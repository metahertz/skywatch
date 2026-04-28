"""State engine — the central message dispatcher.

Responsibilities:
- Maintain the ICAO roster (aircraft seen via squitter) for AP recovery validation
- Route each frame to the correct decoder
- Manage CPR even/odd pair decoding
- Correlate TCAS events across DF16 and TC=28 sources
- Apply position plausibility filters
- Emit deltas (changed-fields-only events) for downstream subscribers
"""
from __future__ import annotations

import logging
import math
import os
import sys
import time
from collections import defaultdict, deque
from dataclasses import asdict
from typing import Callable

from skywatch.decoder import adsb, common
from skywatch.decoder import modes as ms
from skywatch.decoder.beast import BeastFrame
from skywatch.state.aircraft import Aircraft, TcasEvent

# Diagnostic: when SKYWATCH_TRACE_ICAO is set (comma-separated tokens), log
# every TC=29 and inferred BDS-4,0 frame matching those aircraft to stderr,
# including the raw 28-byte hex and the bit values at message positions
# 80-88 (where both message types carry their autopilot-mode flags, just
# at different offsets per spec).  Use this to prove whether a flapping
# autopilot-mode event is real wire data, transponder firmware
# inconsistency, or a BDS-4,0 false-positive on a misidentified frame.
#
# Tokens may be either ICAO 24-bit addresses (e.g. 407FC7) OR callsigns
# (e.g. RRR1230) — both are matched, case-insensitive, so you can paste
# whatever the event ticker shows.
_TRACE_TOKENS = {
    s.strip().upper()
    for s in os.environ.get("SKYWATCH_TRACE_ICAO", "").split(",")
    if s.strip()
}

log = logging.getLogger("skywatch.engine")


# A position decoded for an aircraft must be no further than this from the
# receiver, i.e. line-of-sight to ~FL420.  Tunable per receiver altitude.
DEFAULT_MAX_RANGE_NM = 280

# An aircraft is removed from the active table after this many seconds
# without any reception.
STALE_AGE_SECS = 600

# How long we keep an even/odd CPR frame waiting for its mate.
CPR_PAIR_WINDOW = 10.0


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3440.065  # nautical miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


class StateEngine:
    """The central state machine."""

    def __init__(
        self,
        receiver_lat: float | None = None,
        receiver_lon: float | None = None,
        max_range_nm: float = DEFAULT_MAX_RANGE_NM,
        info_lookup=None,
    ) -> None:
        self.aircraft: dict[str, Aircraft] = {}
        # ICAOs we've seen via squitter (DF11/17/18) recently. Used to
        # validate address-parity recoveries from short replies.
        self._squitter_roster: dict[str, float] = {}
        self.receiver_lat = receiver_lat
        self.receiver_lon = receiver_lon
        self.max_range_nm = max_range_nm
        # Optional InfoLookup for static aircraft metadata (registration,
        # type, country, operator).  See skywatch.db.lookup.
        self.info_lookup = info_lookup

        # Stats
        self.total_frames = 0
        self.frames_by_df: dict[int, int] = defaultdict(int)
        self.frames_dropped: int = 0
        self.start_time = time.time()

        # Listeners for state-change events
        self._listeners: list[Callable[[dict], None]] = []

        # Recent global event log (for the UI's event ticker)
        self.events: deque = deque(maxlen=200)

        # Recent TCAS events across all aircraft (for the RA timeline)
        self.tcas_event_log: deque = deque(maxlen=200)

        # CPR-pair fallback: pending unpaired DF17 position frames
        # not yet attached to an Aircraft (because no roster entry yet).
        self._pending_cpr: dict[str, dict] = {}

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    def subscribe(self, callback: Callable[[dict], None]) -> None:
        self._listeners.append(callback)

    def feed(self, frame: BeastFrame) -> None:
        """Feed one BEAST frame into the engine."""
        self.total_frames += 1
        df_val = common.df(frame.raw_hex)
        self.frames_by_df[df_val] += 1
        try:
            self._dispatch(frame, df_val)
        except Exception as e:
            log.exception("Failed to handle frame %s", frame.raw_hex)
            self.frames_dropped += 1

    def prune_stale(self, now: float | None = None) -> int:
        now = now or time.time()
        cutoff = now - STALE_AGE_SECS
        gone = [k for k, ac in self.aircraft.items() if ac.last_seen < cutoff]
        for k in gone:
            del self.aircraft[k]
        # Same for the roster
        self._squitter_roster = {
            k: t for k, t in self._squitter_roster.items() if t > now - 60
        }
        return len(gone)

    def snapshot(self) -> dict:
        """Full state snapshot for new WebSocket clients."""
        return {
            "type": "snapshot",
            "aircraft": [a.to_dict() for a in self.aircraft.values()],
            "stats": {
                "total_frames": self.total_frames,
                "frames_by_df": dict(self.frames_by_df),
                "frames_dropped": self.frames_dropped,
                "uptime_s": time.time() - self.start_time,
                "active_aircraft": len(self.aircraft),
                "roster_size": len(self._squitter_roster),
            },
            "tcas_events": [self._serialise_tcas(e) for e in self.tcas_event_log],
            "events": list(self.events),
            "receiver": {
                "lat": self.receiver_lat,
                "lon": self.receiver_lon,
                "max_range_nm": self.max_range_nm,
            },
        }

    # -----------------------------------------------------------------
    # Internal: dispatcher
    # -----------------------------------------------------------------

    def _dispatch(self, frame: BeastFrame, df_val: int) -> None:
        msg = frame.raw_hex
        ts = time.time()  # use wall-clock for the state; frame.timestamp is monotonic counter

        if df_val in (17, 18):
            if not common.crc_check(msg):
                self.frames_dropped += 1
                return
            icao = common.icao_from_squitter(msg)
            self._squitter_roster[icao] = ts
            ac = self._get_or_create_aircraft(icao, ts)
            ac.update_seen(ts, df_val, frame.rssi_dbfs)
            self._handle_adsb(ac, msg, ts)

        elif df_val == 11:
            if not common.crc_check(msg):
                # DF11 with non-zero II will fail this check; skip silently.
                return
            icao = common.icao_from_squitter(msg)
            self._squitter_roster[icao] = ts
            ac = self._get_or_create_aircraft(icao, ts)
            ac.update_seen(ts, df_val, frame.rssi_dbfs)

        elif df_val in (0, 4, 5, 16, 20, 21):
            # Address-parity. Recover ICAO and validate against roster.
            icao = common.recover_icao(msg)
            if icao not in self._squitter_roster:
                # Reject — likely a CRC corruption rather than a real frame.
                self.frames_dropped += 1
                return
            ac = self._get_or_create_aircraft(icao, ts)
            ac.update_seen(ts, df_val, frame.rssi_dbfs)

            if df_val in (0, 4, 16, 20):
                alt = ms.altitude_code(msg)
                if alt is not None and 0 <= alt <= 60000:
                    if df_val in (4, 20):
                        ac.alt_baro_ft = alt
                    elif df_val in (0,):
                        # DF0 also carries altitude; keep it for non-ADS-B targets.
                        ac.alt_baro_ft = alt
            if df_val in (5, 21):
                sq = ms.identity_code(msg)
                if sq:
                    ac.squawk = sq
            if df_val in (4, 5, 20, 21):
                fs = ms.flight_status(msg)
                if fs:
                    ac.flight_status = fs["label"]
                    ac.alert = fs["alert"]
                    ac.spi = fs["spi"]
                    if fs["on_ground"] is not None:
                        ac.on_ground = fs["on_ground"]

            if df_val in (20, 21):
                self._handle_commb(ac, msg)
            if df_val == 16:
                self._handle_tcas_coordination(ac, msg, ts)

            self._emit_update(ac)
        else:
            # DF24 (Comm-D) and others — count but don't decode for v0.1.
            pass

    def _get_or_create_aircraft(self, icao: str, ts: float) -> Aircraft:
        ac = self.aircraft.get(icao)
        if ac is None:
            ac = Aircraft(icao=icao, first_seen=ts)
            self.aircraft[icao] = ac
            # Resolve static info immediately so the very first event has it.
            self._refresh_info(ac)
            self._log_event({
                "t": ts,
                "type": "new_aircraft",
                "icao": icao,
            })
        return ac

    def _refresh_info(self, ac: Aircraft) -> None:
        """(Re)resolve static aircraft info; cheap because InfoLookup caches."""
        if self.info_lookup is None:
            return
        if ac._db_info_callsign == ac.callsign and ac.db_info is not None:
            return  # nothing changed since last resolution
        ac.db_info = self.info_lookup.lookup(ac.icao, callsign=ac.callsign)
        ac._db_info_callsign = ac.callsign

    # -----------------------------------------------------------------
    # ADS-B handler
    # -----------------------------------------------------------------

    def _handle_adsb(self, ac: Aircraft, msg: str, ts: float) -> None:
        tc = adsb.typecode(msg)

        if 1 <= tc <= 4:
            cs = adsb.callsign(msg)
            if cs:
                ac.callsign = cs
                self._refresh_info(ac)
            cat = adsb.wake_category(msg)
            if cat:
                ac.category = cat

        elif 5 <= tc <= 8:
            # Surface position - we'd need a reference; not handled in v0.1.
            pass

        elif 9 <= tc <= 18 or 20 <= tc <= 22:
            # Airborne position
            alt = adsb.altitude(msg)
            if alt is not None:
                if 9 <= tc <= 18:
                    ac.alt_baro_ft = alt
                else:
                    ac.alt_gnss_ft = alt

            # CPR handling
            f = adsb.cpr_format(msg)
            lat_cpr, lon_cpr = adsb.cpr_lat_lon(msg)
            entry = (ts, lat_cpr, lon_cpr, msg)

            position = None
            if ac.lat is not None and ac.lon is not None:
                # We already have a position — try local decode first.
                try:
                    cand = adsb.position_local(msg, ac.lat, ac.lon)
                    if self._is_plausible(ac, cand[0], cand[1], ts):
                        position = cand
                except Exception:
                    position = None

            if position is None:
                # Try global decode with stored complementary frame.
                if f == 0:
                    other = ac._cpr_odd
                    ac._cpr_even = entry
                    if other and (ts - other[0]) < CPR_PAIR_WINDOW:
                        try:
                            cand = adsb.position_global(msg, other[3], ts, other[0])
                            if cand and self._is_plausible(ac, cand[0], cand[1], ts):
                                position = cand
                        except Exception:
                            pass
                else:
                    other = ac._cpr_even
                    ac._cpr_odd = entry
                    if other and (ts - other[0]) < CPR_PAIR_WINDOW:
                        try:
                            cand = adsb.position_global(other[3], msg, other[0], ts)
                            if cand and self._is_plausible(ac, cand[0], cand[1], ts):
                                position = cand
                        except Exception:
                            pass

            if position is not None:
                ac.record_position(position[0], position[1], ts)

        elif tc == 19:
            v = adsb.velocity(msg)
            if v:
                if v.speed is not None:
                    if v.speed_type == "GS":
                        ac.gs_kt = v.speed
                    elif v.speed_type == "TAS":
                        ac.tas_kt = int(v.speed)
                    elif v.speed_type == "IAS":
                        ac.ias_kt = int(v.speed)
                if v.track is not None:
                    ac.track_deg = v.track
                if v.heading is not None:
                    ac.heading_deg = v.heading
                if v.vrate is not None:
                    ac.vrate_fpm = v.vrate
                    if v.vrate_source == "baro":
                        ac.vrate_baro_fpm = v.vrate
                    else:
                        ac.vrate_ins_fpm = v.vrate

        elif tc == 28:
            # Sub-type 1 = emergency, sub-type 2 = TCAS RA broadcast
            emerg = adsb.emergency_state(msg)
            if emerg is not None:
                ac.emergency = emerg
                self._log_event({
                    "t": ts, "type": "emergency", "icao": ac.icao, "state": emerg,
                })
            ra = adsb.tcas_ra_broadcast(msg)
            if ra:
                self._handle_tcas_ra(ac, ra, ts, source="TC28")

        elif tc == 29:
            t = adsb.target_state(msg)
            if t:
                # Snapshot intent state before applying changes
                pre = self._snapshot_intent(ac)
                if t.selected_alt_ft is not None:
                    if t.alt_source == "FMS":
                        ac.sel_alt_fms_ft = t.selected_alt_ft
                    else:
                        ac.sel_alt_mcp_ft = t.selected_alt_ft
                if t.qnh_mb is not None:
                    ac.qnh_mb = t.qnh_mb
                if t.selected_heading_deg is not None:
                    ac.selected_heading_deg = t.selected_heading_deg
                ac.autopilot_modes = {
                    "autopilot": t.autopilot,
                    "vnav": t.vnav,
                    "alt_hold": t.alt_hold,
                    "approach": t.approach,
                    "tcas": t.tcas_operational,
                }
                self._trace_modes(ac, msg, "TC29", {
                    "autopilot": t.autopilot, "vnav": t.vnav,
                    "alt_hold": t.alt_hold, "approach": t.approach,
                    "tcas": t.tcas_operational,
                })
                self._emit_intent_changes(ac, pre, ts, source="TC29")

        elif tc == 31:
            os = adsb.operational_status(msg)
            if os:
                ac.adsb_version = os.version
                ac.nac_p = os.nac_p
                ac.nic = os.nic_supplement_a
                ac.sil = os.sil
                if os.on_ground is not None:
                    ac.on_ground = os.on_ground

        self._emit_update(ac)

    # -----------------------------------------------------------------
    # Comm-B handler
    # -----------------------------------------------------------------

    def _handle_commb(self, ac: Aircraft, msg: str) -> None:
        candidates = ms.infer_bds(msg, ac_state=ac.to_dict())
        if not candidates:
            return
        # If exactly one candidate or the highest-confidence one, accept.
        candidates.sort(key=lambda c: {"high": 0, "medium": 1, "low": 2}[c.confidence])
        winner = candidates[0]
        ac.bds_observed.add(winner.bds_code)
        d = winner.decoded
        if winner.bds_code == "2,0":
            if isinstance(d, str) and d:
                ac.callsign = d
                self._refresh_info(ac)
        elif winner.bds_code == "4,0" and isinstance(d, ms.BDS40):
            # BDS 4,0 carries selected altitudes + QNH; we deliberately do
            # NOT consume its mode-flag bits because real-world transponder
            # firmwares encode them inconsistently with TC=29.  TC=29 is the
            # sole source of truth for autopilot modes.
            pre = self._snapshot_intent(ac)
            if d.mcp_alt_ft is not None:
                ac.sel_alt_mcp_ft = d.mcp_alt_ft
            if d.fms_alt_ft is not None:
                ac.sel_alt_fms_ft = d.fms_alt_ft
            if d.qnh_mb is not None:
                ac.qnh_mb = d.qnh_mb
            self._trace_modes(ac, msg, "BDS40", {
                "modes": "ignored (TC=29 is source of truth)",
            }, extra={
                "candidates": [(c.bds_code, c.confidence) for c in candidates],
                "mcp": d.mcp_alt_ft, "fms": d.fms_alt_ft, "qnh": d.qnh_mb,
            })
            self._emit_intent_changes(ac, pre, time.time(), source="BDS40")
        elif winner.bds_code == "5,0" and isinstance(d, ms.BDS50):
            if d.roll_deg is not None:
                ac.roll_deg = d.roll_deg
            if d.track_deg is not None:
                ac.track_deg = d.track_deg
            if d.gs_kt is not None:
                ac.gs_kt = float(d.gs_kt)
            if d.track_rate_dps is not None:
                ac.track_rate_dps = d.track_rate_dps
            if d.tas_kt is not None:
                ac.tas_kt = d.tas_kt
        elif winner.bds_code == "6,0" and isinstance(d, ms.BDS60):
            if d.heading_deg is not None:
                ac.heading_deg = d.heading_deg
            if d.ias_kt is not None:
                ac.ias_kt = d.ias_kt
            if d.mach is not None:
                ac.mach = d.mach
            if d.vrate_baro_fpm is not None:
                ac.vrate_baro_fpm = d.vrate_baro_fpm
                ac.vrate_fpm = d.vrate_baro_fpm
            if d.vrate_ins_fpm is not None:
                ac.vrate_ins_fpm = d.vrate_ins_fpm
        elif winner.bds_code == "4,4" and isinstance(d, ms.BDS44):
            if d.wind_speed_kt is not None:
                ac.wind_speed_kt = d.wind_speed_kt
            if d.wind_direction_deg is not None:
                ac.wind_direction_deg = d.wind_direction_deg
            if d.static_air_temp_c is not None:
                ac.static_air_temp_c = d.static_air_temp_c

    # -----------------------------------------------------------------
    # TCAS handlers
    # -----------------------------------------------------------------

    def _handle_tcas_coordination(self, ac: Aircraft, msg: str, ts: float) -> None:
        coord = ms.decode_tcas_coordination(msg)
        if coord is None:
            return
        # No threat ICAO available in DF16 alone.
        # Build a synthetic broadcast-shaped record.
        from skywatch.decoder.adsb import TcasRaBroadcast
        ra = TcasRaBroadcast(
            active_ra=coord.active_ra,
            rac_record=coord.rac_record,
            ra_terminated=coord.ra_terminated,
            multiple_threat=coord.multiple_threat,
            threat_id_type=0,
            threat_icao=None,
            summary=coord.summary,
        )
        self._handle_tcas_ra(ac, ra, ts, source="DF16")

    def _handle_tcas_ra(self, ac: Aircraft, ra, ts: float, source: str) -> None:
        if ra.ra_terminated:
            # Latch the end time but keep the summary visible for ~18s.
            if ac.tcas_ra_active:
                ac.tcas_ra_ended_at = ts
                ac.tcas_ra_active = False
                # Close the most recent open event.
                for ev in reversed(ac.tcas_ra_history):
                    if ev.ended_at is None:
                        ev.ended_at = ts
                        break
                self._log_event({
                    "t": ts, "type": "tcas_ra_ended",
                    "icao": ac.icao, "summary": ac.tcas_ra_summary,
                })
            return

        # Active RA
        was_active = ac.tcas_ra_active
        ac.tcas_ra_active = True
        ac.tcas_ra_summary = ra.summary
        if ra.threat_icao:
            ac.tcas_threat_icao = ra.threat_icao
        if not was_active:
            ac.tcas_ra_started_at = ts
            ev = TcasEvent(
                started_at=ts,
                ended_at=None,
                ra_summary=ra.summary,
                threat_icao=ra.threat_icao,
                rac_record=ra.rac_record,
                multiple_threat=ra.multiple_threat,
                source=source,
            )
            ac.tcas_ra_history.append(ev)
            self.tcas_event_log.append((ac.icao, ev))
            self._log_event({
                "t": ts,
                "type": "tcas_ra_started",
                "icao": ac.icao,
                "callsign": ac.callsign,
                "summary": ra.summary,
                "threat_icao": ra.threat_icao,
                "source": source,
            })
            # Mark the threat aircraft too if we know it.
            if ra.threat_icao and ra.threat_icao in self.aircraft:
                tac = self.aircraft[ra.threat_icao]
                if not tac.tcas_ra_active:
                    tac.tcas_threat_icao = ac.icao
                    self._emit_update(tac)

    # -----------------------------------------------------------------
    # Autopilot intent change detection
    # -----------------------------------------------------------------

    # Threshold below which a selected-heading change is considered routine
    # bug-following noise rather than a deliberate new heading select.
    _HEADING_CHANGE_DEG = 5.0
    # QNH is set on a 0.1 mb LSB, but real-world QNH only changes at
    # transition altitude or when ATC issues a new setting; require >= 0.5 mb.
    _QNH_CHANGE_MB = 0.5
    # Per-source LSB floor for selected-altitude changes.  BDS 4,0 ("Mode S
    # EHS — selected vertical intention") encodes MCP/FCU and FMS selected
    # altitude with a 16 ft LSB.  ADS-B TC=29 ("Target state and status")
    # commonly lands on a 32 ft grid.  Sub-LSB deltas are just rounding —
    # often from cross-source quantization of the same physical selection —
    # not a deliberate new altitude select, so we suppress the event.
    _SEL_ALT_LSB_FT = {"BDS40": 16, "TC29": 32}

    @staticmethod
    def _trace_modes(ac: Aircraft, msg: str, source: str, decoded: dict,
                     extra: dict | None = None) -> None:
        """Diagnostic dump of an autopilot-mode frame, when the aircraft
        matches SKYWATCH_TRACE_ICAO (ICAO hex or callsign).  Off by default."""
        if not _TRACE_TOKENS:
            return
        cs = (ac.callsign or "").upper()
        if ac.icao.upper() not in _TRACE_TOKENS and cs not in _TRACE_TOKENS:
            return
        # Bit positions 80-88 cover both decoders' mode-flag regions:
        #   TC29:  80=mode_valid 81=AP 82=VNAV 83=ALT_HOLD 84=IMF
        #          85=APPROACH 86=TCAS 87=LNAV 88=-
        #   BDS40: 80=mode_status 81=VNAV 82=ALT_HOLD 83=APPROACH
        #          84-85=reserved 86=src_status 87-88=tgt_alt_src
        bits = "".join(str(common.get_bit(msg, b)) for b in range(80, 89))
        extra_s = f" {extra}" if extra else ""
        print(
            f"[TRACE {source} {ac.icao}/{cs or '-'}] hex={msg} "
            f"msg[80-88]={bits} modes={decoded}{extra_s}",
            file=sys.stderr, flush=True,
        )

    @staticmethod
    def _snapshot_intent(ac: Aircraft) -> dict:
        """Capture the autopilot/intent state for later diffing."""
        return {
            "sel_alt_mcp_ft": ac.sel_alt_mcp_ft,
            "sel_alt_fms_ft": ac.sel_alt_fms_ft,
            "qnh_mb": ac.qnh_mb,
            "selected_heading_deg": ac.selected_heading_deg,
            "autopilot_modes": dict(ac.autopilot_modes or {}),
        }

    def _emit_intent_changes(
        self, ac: Aircraft, pre: dict, ts: float, source: str,
    ) -> None:
        """Compare the aircraft's intent state to `pre` and emit events
        for each change.  Applies a one-frame hysteresis to defend against
        BDS 4,0 false-positive single frames.

        `source` is "TC29" (ADS-B v2 broadcast) or "BDS40" (Comm-B reply).
        """
        post = self._snapshot_intent(ac)
        cs = ac.callsign or ac.icao

        # ---- Selected altitude changes (MCP and FMS tracked separately) ---
        for field, label in (
            ("sel_alt_mcp_ft", "MCP"),
            ("sel_alt_fms_ft", "FMS"),
        ):
            old, new = pre[field], post[field]
            if new is None or new == old:
                continue
            # First-ever observation: emit immediately, no hysteresis.
            if old is None:
                ac._sel_alt_source[label] = source
                self._log_event({
                    "t": ts,
                    "type": "intent_change",
                    "subtype": "selected_altitude",
                    "icao": ac.icao,
                    "callsign": cs,
                    "field": label,
                    "old": None,
                    "new": new,
                    "summary": f"SEL ALT ({label}) {new:,} ft",
                    "source": source,
                })
                continue
            # Sub-LSB floor.  BDS40 has a 16 ft LSB; TC29 lands on a 32 ft
            # grid.  When a value crosses sources, the same physical
            # selection can re-grid by up to the coarser of the two LSBs
            # (e.g. TC29's 37024 ft → BDS40's 37000 ft = 24 ft "change"
            # that is purely regridding).  Pick the coarser threshold.
            old_src = ac._sel_alt_source.get(label, source)
            min_step = max(
                self._SEL_ALT_LSB_FT.get(source, 0),
                self._SEL_ALT_LSB_FT.get(old_src, 0),
            )
            if min_step and abs(new - old) < min_step:
                # Quiet update: refresh source (so the next compare uses
                # the correct LSB) but don't emit and don't run hysteresis.
                ac._sel_alt_source[label] = source
                continue
            # Subsequent change: hysteresis.  Require the new value to
            # appear twice in a row before accepting it.  This defends
            # against single-frame BDS 4,0 false positives.
            pending_key = f"{field}"
            if ac._pending_intent.get(pending_key) != new:
                ac._pending_intent[pending_key] = new
                # Roll back so the UI doesn't briefly display a noisy value
                setattr(ac, field, old)
                continue
            ac._pending_intent.pop(pending_key, None)
            ac._sel_alt_source[label] = source
            old_str = f"{old:,} ft"
            new_str = f"{new:,} ft"
            self._log_event({
                "t": ts,
                "type": "intent_change",
                "subtype": "selected_altitude",
                "icao": ac.icao,
                "callsign": cs,
                "field": label,
                "old": old,
                "new": new,
                "summary": f"SEL ALT ({label}) {old_str} → {new_str}",
                "source": source,
            })

        # ---- QNH changes ----
        old, new = pre["qnh_mb"], post["qnh_mb"]
        if new is not None and old is not None:
            if abs(new - old) >= self._QNH_CHANGE_MB:
                pending_key = "qnh_mb"
                # Confirm the new QNH twice before announcing
                pending = ac._pending_intent.get(pending_key)
                if pending is None or abs(pending - new) >= self._QNH_CHANGE_MB:
                    ac._pending_intent[pending_key] = new
                    ac.qnh_mb = old   # roll back until confirmed
                else:
                    ac._pending_intent.pop(pending_key, None)
                    self._log_event({
                        "t": ts,
                        "type": "intent_change",
                        "subtype": "qnh",
                        "icao": ac.icao,
                        "callsign": cs,
                        "old": old,
                        "new": new,
                        "summary": f"QNH {old:.1f} → {new:.1f} mb",
                        "source": source,
                    })
        elif new is not None and old is None:
            # First-ever QNH report — emit as a single event
            self._log_event({
                "t": ts,
                "type": "intent_change",
                "subtype": "qnh",
                "icao": ac.icao,
                "callsign": cs,
                "old": None,
                "new": new,
                "summary": f"QNH {new:.1f} mb",
                "source": source,
            })

        # ---- Selected heading (only emit on substantial changes) ----
        old, new = pre["selected_heading_deg"], post["selected_heading_deg"]
        if new is not None:
            if old is None:
                self._log_event({
                    "t": ts, "type": "intent_change", "subtype": "selected_heading",
                    "icao": ac.icao, "callsign": cs,
                    "old": None, "new": new,
                    "summary": f"SEL HDG {new:03.0f}°",
                    "source": source,
                })
            else:
                # Smallest absolute angular distance
                d = abs(((new - old + 540) % 360) - 180)
                if d >= self._HEADING_CHANGE_DEG:
                    self._log_event({
                        "t": ts, "type": "intent_change",
                        "subtype": "selected_heading",
                        "icao": ac.icao, "callsign": cs,
                        "old": old, "new": new,
                        "summary": f"SEL HDG {old:03.0f}° → {new:03.0f}°",
                        "source": source,
                    })

        # ---- Autopilot mode flag changes ----
        # Each mode flip emits one event, but to defend against rapid
        # source-alternation (TC29 vs BDS40 disagreement) we suppress
        # repeats of the same flip within MODE_DWELL_S of the previous one.
        old_modes = pre["autopilot_modes"] or {}
        new_modes = post["autopilot_modes"] or {}
        for mode in set(old_modes) | set(new_modes):
            o = old_modes.get(mode)
            n = new_modes.get(mode)
            if o == n or n is None:
                continue
            # Only report flips where both old and new were definitive bools.
            if o is None:
                continue
            # Anti-spam: suppress a repeated flip of the same mode within 3s.
            dwell_key = f"_mode_emit_{mode}"
            last_emit = ac._pending_intent.get(dwell_key, 0.0)
            if ts - last_emit < 3.0:
                continue
            ac._pending_intent[dwell_key] = ts
            verb = "ENGAGED" if n else "DISENGAGED"
            self._log_event({
                "t": ts,
                "type": "intent_change",
                "subtype": "ap_mode",
                "icao": ac.icao,
                "callsign": cs,
                "mode": mode,
                "active": bool(n),
                "summary": f"{mode.upper().replace('_', ' ')} {verb}",
                "source": source,
            })

    # -----------------------------------------------------------------
    # Plausibility
    # -----------------------------------------------------------------

    def _is_plausible(self, ac: Aircraft, lat: float, lon: float, t: float) -> bool:
        """Receiver-range check + previous-fix sanity check."""
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return False

        if self.receiver_lat is not None and self.receiver_lon is not None:
            d = _haversine_nm(self.receiver_lat, self.receiver_lon, lat, lon)
            if d > self.max_range_nm:
                return False

        if (
            ac._prev_lat is not None
            and ac._prev_lon is not None
            and ac._prev_position_at is not None
        ):
            dt = t - ac._prev_position_at
            if dt <= 0:
                return False
            d = _haversine_nm(ac._prev_lat, ac._prev_lon, lat, lon)
            # Max possible: Concorde was 1350 kt. Allow 1500 kt.
            max_d = (1500.0 / 3600.0) * dt
            if d > max(max_d, 0.5):  # 0.5 NM minimum slack for first-fix noise
                return False
        return True

    # -----------------------------------------------------------------
    # Eventing
    # -----------------------------------------------------------------

    def _emit_update(self, ac: Aircraft) -> None:
        for cb in self._listeners:
            try:
                cb({"type": "update", "icao": ac.icao, "data": ac.to_dict()})
            except Exception:
                log.exception("listener failed")

    def _log_event(self, ev: dict) -> None:
        self.events.append(ev)
        for cb in self._listeners:
            try:
                cb({"type": "event", "event": ev})
            except Exception:
                log.exception("listener failed")

    def _serialise_tcas(self, item):
        icao, ev = item
        return {
            "icao": icao,
            "started_at": ev.started_at,
            "ended_at": ev.ended_at,
            "summary": ev.ra_summary,
            "threat_icao": ev.threat_icao,
            "multiple_threat": ev.multiple_threat,
            "source": ev.source,
        }
