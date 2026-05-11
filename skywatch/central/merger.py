"""Central merger: consumes per-receiver deltas, builds a merged
view, and re-emits engine events that AppServer broadcasts to the UI.

Why not reuse `StateEngine.feed()` directly?
  Because edges already did the BEAST → decode work; what we receive
  here is *output*, not input.  We need the engine's data structures
  (Aircraft, ReceiverRegistry, ReceiverAttribution) and the listener
  fan-out, but we skip the dispatch / decode / CPR pathway entirely.

The merger is a thin shim:
  * `apply_delta(delta)` updates engine state for one delta.
  * For aircraft updates from receiver R about ICAO X:
      - locate-or-create the Aircraft
      - merge that receiver's slice into `Aircraft.by_receiver[R]`
      - update top-level "best view" fields (lat/lon/alt/etc.) using
        a freshest-by-receipt rule
      - track the per-(icao, R) gen counter and warn on gaps
      - emit the standard engine `update` event so the existing
        AppServer broadcasts it to UI clients
  * For event deltas: forward to the engine's event log + listeners.
  * For receiver deltas: upsert the engine's receiver registry.

Cross-receiver TCAS pair-link reconciliation lives here too — when
two aircraft both report TCAS-active and reference each other's ICAOs,
the existing engine logic can run because both Aircraft objects now
exist in this single merged engine.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from skywatch.state import StateEngine
from skywatch.state.aircraft import Aircraft, ReceiverAttribution
from skywatch.transport import (
    DELTA_TYPE_AIRCRAFT, DELTA_TYPE_COMMS, DELTA_TYPE_EVENT,
    DELTA_TYPE_METRICS, DELTA_TYPE_RECEIVER, Delta,
)

log = logging.getLogger("skywatch.central.merger")


# How many gen units may be skipped before we warn loudly.  In normal
# steady-state, gaps are 0 (we see every delta).  Small gaps on
# reconnect after a transport blip are expected (spool drain catches
# up).  Large gaps suggest spool overflow → real data loss.
GAP_WARN_THRESHOLD = 100


class CentralMerger:
    """Owns the merged StateEngine.  Subscribe a transport into
    `apply_delta` and the engine's listeners do the rest."""

    def __init__(self, engine: StateEngine):
        self.engine = engine
        # Per-(receiver_id) last gen we observed.  None means we don't
        # know yet.  A reset (incoming gen < last) is treated as a
        # legitimate edge restart and silently re-anchored.
        self._last_gen: dict[str, int] = {}
        self.deltas_applied = 0
        self.deltas_dropped_invalid = 0
        self.gen_gaps = 0
        self.gen_resets = 0

    # -- entry point --------------------------------------------------

    def apply_delta(self, delta: Delta) -> None:
        try:
            self._track_gen(delta)
            if delta.type == DELTA_TYPE_AIRCRAFT:
                self._apply_aircraft(delta)
            elif delta.type == DELTA_TYPE_EVENT:
                self._apply_event(delta)
            elif delta.type == DELTA_TYPE_COMMS:
                self._apply_comms(delta)
            elif delta.type == DELTA_TYPE_RECEIVER:
                self._apply_receiver(delta)
            elif delta.type == DELTA_TYPE_METRICS:
                # Metrics flow straight to the store via engine.store
                # if persistence is enabled; ignore otherwise.
                if self.engine.store is not None:
                    self.engine.store.record_receiver_metrics(
                        delta.receiver_id, delta.payload or {})
            else:
                self.deltas_dropped_invalid += 1
                return
            self.deltas_applied += 1
        except Exception:
            log.exception("merger failed on delta gen=%s rid=%s type=%s",
                          delta.gen, delta.receiver_id, delta.type)
            self.deltas_dropped_invalid += 1

    # -- gen tracking -------------------------------------------------

    def _track_gen(self, delta: Delta) -> None:
        prev = self._last_gen.get(delta.receiver_id)
        if prev is None:
            self._last_gen[delta.receiver_id] = delta.gen
            return
        if delta.gen <= prev:
            # Edge restart (gen reset to 0) or duplicate / out-of-order.
            if delta.gen < prev - GAP_WARN_THRESHOLD:
                self.gen_resets += 1
                log.info("[%s] gen reset detected (%d → %d) — edge restart",
                         delta.receiver_id, prev, delta.gen)
            self._last_gen[delta.receiver_id] = delta.gen
            return
        gap = delta.gen - prev - 1
        if gap > 0:
            self.gen_gaps += gap
            if gap >= GAP_WARN_THRESHOLD:
                log.warning("[%s] %d-delta gen gap detected (last=%d, now=%d)",
                            delta.receiver_id, gap, prev, delta.gen)
        self._last_gen[delta.receiver_id] = delta.gen

    # -- per-type appliers --------------------------------------------

    def _apply_aircraft(self, delta: Delta) -> None:
        payload = delta.payload or {}
        icao = payload.get("icao")
        if not icao:
            self.deltas_dropped_invalid += 1
            return
        ac = self.engine.aircraft.get(icao)
        if ac is None:
            ac = Aircraft(icao=icao)
            self.engine.aircraft[icao] = ac
        # Top-level "best view" fields: take freshest non-None.  We
        # use the delta's own `ts` as the "freshness" stamp; a slightly
        # delayed but newer-on-the-wire delta wins over an older one
        # already merged.
        ts = delta.ts
        # Update top-level scalar fields if they're newer than what
        # we have OR we don't have them yet.  We don't track per-field
        # last-update times in v1 — the merger is naive on this axis,
        # which is acceptable because each receiver's edge engine has
        # already filtered out obvious garbage.
        for fld in (
            "callsign", "category", "squawk",
            "lat", "lon", "alt_baro_ft", "alt_gnss_ft",
            "gs_kt", "tas_kt", "ias_kt", "mach",
            "track_deg", "heading_deg",
            "vrate_fpm", "vrate_baro_fpm", "vrate_ins_fpm",
            "roll_deg", "track_rate_dps",
            "sel_alt_mcp_ft", "sel_alt_fms_ft", "qnh_mb",
            "selected_heading_deg",
            "on_ground", "flight_status", "alert", "spi",
            "nic", "nac_p", "sil", "adsb_version", "emergency",
            "wind_speed_kt", "wind_direction_deg", "static_air_temp_c",
            "tcas_ra_active", "tcas_ra_summary", "tcas_threat_icao",
            "tcas_ra_started_at", "tcas_ra_ended_at",
        ):
            v = payload.get(fld)
            if v is not None:
                setattr(ac, fld, v)
        # Update last_position_at, first/last_seen across all receivers.
        if payload.get("last_position_at") is not None:
            if (ac.last_position_at is None or
                    payload["last_position_at"] >= ac.last_position_at):
                ac.last_position_at = payload["last_position_at"]
        ac.last_seen = max(ac.last_seen, ts)
        if payload.get("first_seen") is not None:
            ac.first_seen = min(ac.first_seen, payload["first_seen"])
        # Per-receiver attribution: the edge already supplied a slice
        # for ITS receiver_id; we adopt that slice as-is for that key.
        # If the edge bundled multiple receivers (it shouldn't, but
        # tolerate), we adopt all.
        edge_by_rx = payload.get("by_receiver") or {}
        for rid, slice_ in edge_by_rx.items():
            self._update_attribution(ac, rid, slice_)

        # Merge bds_observed and msg_counts (top-level).
        for b in payload.get("bds_observed") or []:
            ac.bds_observed.add(b)
        for df, n in (payload.get("msg_counts") or {}).items():
            try:
                df_int = int(df)
            except (ValueError, TypeError):
                continue
            # Counts are per-edge cumulative; we keep max-across-edges
            # as a coarse merged view (good enough for UI).
            if n > ac.msg_counts.get(df_int, 0):
                ac.msg_counts[df_int] = n

        # Trail: extend with any new points beyond what we already have.
        new_trail = payload.get("trail") or []
        if new_trail:
            existing_max_t = ac.trail[-1][0] if ac.trail else -1
            for pt in new_trail:
                if not isinstance(pt, (list, tuple)) or len(pt) < 4:
                    continue
                if pt[0] > existing_max_t:
                    ac.trail.append(tuple(pt))

        # Comms: append any new entries the edge embedded in the
        # aircraft payload, with dedup against entries already added
        # via DELTA_TYPE_COMMS.  Either ordering of arrivals (embedded
        # first OR dedicated comms delta first) converges on the same
        # deque content.  The deque's maxlen=50 caps growth.
        for c in (payload.get("comms") or []):
            if isinstance(c, dict) and not _comms_dup(ac.comms, c):
                ac.comms.append(c)

        # Info / route from edge — adopt as-is when supplied.
        if payload.get("info"):
            ac.db_info = _DictAsAttr(payload["info"])
        if payload.get("route"):
            ac.route = payload["route"]

        # Re-emit through the merged engine's listeners and persistence.
        self.engine._emit_update(ac)

    @staticmethod
    def _update_attribution(ac: Aircraft, rid: str, slice_: dict) -> None:
        bucket = ac.by_receiver.get(rid)
        if bucket is None:
            bucket = ReceiverAttribution(
                first_seen=slice_.get("first_seen", time.time()),
                last_seen=slice_.get("last_seen", time.time()),
            )
            ac.by_receiver[rid] = bucket
        bucket.last_seen = max(bucket.last_seen, slice_.get("last_seen", 0))
        if slice_.get("first_seen") is not None:
            bucket.first_seen = min(bucket.first_seen, slice_["first_seen"])
        for df, n in (slice_.get("msg_counts") or {}).items():
            try:
                df_int = int(df)
            except (ValueError, TypeError):
                continue
            if n > bucket.msg_counts.get(df_int, 0):
                bucket.msg_counts[df_int] = n
        if "rssi" in slice_ and slice_["rssi"] is not None:
            bucket.rssi_avg = slice_["rssi"]
        if "rssi_samples" in slice_ and slice_["rssi_samples"] is not None:
            bucket.rssi_samples = max(bucket.rssi_samples,
                                      int(slice_["rssi_samples"]))
        if "gen" in slice_ and slice_["gen"] is not None:
            bucket.gen = max(bucket.gen, int(slice_["gen"]))

    def _apply_event(self, delta: Delta) -> None:
        ev = delta.payload or {}
        # Tag the event with which edge it came from, for UI grouping.
        ev.setdefault("source_receiver_id", delta.receiver_id)
        # Engine._log_event handles persistence + listener fanout.
        self.engine._log_event(ev)

    def _apply_comms(self, delta: Delta) -> None:
        """One DELTA_TYPE_COMMS envelope from an edge.

        Two responsibilities:
          1. Persist to the dedicated `comms` time-series collection
             (only when the central has Mongo configured).  The
             aircraft delta carries an embedded `comms` list too, but
             that lives inside the aircraft_state collection — the
             dedicated `comms` archive (with TTL, time-series indexes,
             per-kind queries) only gets populated by this path.
          2. Append the entry to the matching Aircraft.comms deque
             with dedup against any embedded entry the aircraft delta
             may have already added.  This makes the central's
             behaviour identical to monolithic mode: the detail-pane
             COMMS section is populated whether the embedded list or
             the dedicated delta arrives first.
        """
        payload = delta.payload or {}
        # Persist to the dedicated `comms` collection.  MongoStore
        # accesses attributes (not dict keys), so wrap in a tiny shim.
        if self.engine.store is not None:
            self.engine.store.enqueue_comms(_CommsShim(payload, delta.receiver_id))
        # Attach to the aircraft's comms deque, when there is one.
        icao = payload.get("aircraft_icao")
        if not icao:
            return
        ac = self.engine.aircraft.get(icao)
        if ac is None:
            # Comms can arrive before the first aircraft delta for a
            # newly-tracked ICAO.  Create an Aircraft on sight so the
            # COMMS row isn't lost; subsequent aircraft deltas merge in.
            ac = Aircraft(icao=icao)
            self.engine.aircraft[icao] = ac
        entry = {
            "ts": payload.get("ts"),
            "kind": payload.get("kind"),
            "direction": payload.get("direction"),
            "label": payload.get("label"),
            "text": payload.get("text"),
            "peer": (payload.get("dst_icao")
                     if payload.get("direction") == "downlink"
                     else payload.get("src_icao")),
            "receiver_id": delta.receiver_id,
            "flight": payload.get("flight"),
            "blocks": 1,
            "complete": True,
        }
        if not _comms_dup(ac.comms, entry):
            ac.comms.append(entry)

    def _apply_receiver(self, delta: Delta) -> None:
        rx_doc = delta.payload or {}
        rid = rx_doc.get("id") or delta.receiver_id
        if not rid:
            return
        self.engine.receivers.upsert(
            rid,
            name=rx_doc.get("name"),
            lat=rx_doc.get("lat"),
            lon=rx_doc.get("lon"),
            max_range_nm=rx_doc.get("max_range_nm"),
        )
        rx = self.engine.receivers.get(rid)
        if rx is not None:
            for k in ("connected", "first_seen", "last_frame_at",
                      "frames_total", "frames_dropped"):
                if k in rx_doc and rx_doc[k] is not None:
                    setattr(rx, k, rx_doc[k])
        if self.engine.store is not None and rx is not None:
            self.engine.store.upsert_receiver(rx.to_dict())


def _comms_dup(deque_, entry: dict) -> bool:
    """True if `entry` is already in the deque (matched on the
    operationally-distinguishing fields).  ts is float-compared
    exactly because both delta paths originate from the same edge
    with the same `frame.ts` value, so equality is reliable."""
    key = (entry.get("ts"), entry.get("kind"),
           entry.get("label"), entry.get("text"))
    for existing in deque_:
        ek = (existing.get("ts"), existing.get("kind"),
              existing.get("label"), existing.get("text"))
        if ek == key:
            return True
    return False


class _CommsShim:
    """Adapter that lets `MongoStore.enqueue_comms()` (which expects
    attribute access on a VdlFrame) consume a comms-delta payload dict
    directly.  Avoids re-importing the VdlFrame dataclass into the
    central, which lives a few packages away from the decoder."""
    __slots__ = ("ts", "receiver_id", "aircraft_icao", "src_icao",
                 "dst_icao", "direction", "kind", "label", "text",
                 "flight", "reg", "raw")

    def __init__(self, payload: dict, receiver_id: str):
        self.ts = payload.get("ts")
        self.receiver_id = receiver_id
        self.aircraft_icao = payload.get("aircraft_icao")
        self.src_icao = payload.get("src_icao")
        self.dst_icao = payload.get("dst_icao")
        self.direction = payload.get("direction")
        self.kind = payload.get("kind")
        self.label = payload.get("label")
        self.text = payload.get("text")
        self.flight = payload.get("flight")
        self.reg = payload.get("reg")
        self.raw = payload.get("raw")


class _DictAsAttr:
    """Tiny helper: wraps a dict so getattr(d, 'foo') works.  Used to
    re-hydrate the `info` block on a merged Aircraft so the existing
    `Aircraft.to_dict()` serialiser keeps working."""
    def __init__(self, d: dict):
        self._d = d or {}

    def __getattr__(self, name: str):
        if name == "_d":
            raise AttributeError(name)
        v = self._d.get(name)
        if isinstance(v, dict):
            return _DictAsAttr(v)
        return v
