"""Aircraft state object — accumulates everything we know about one aircraft."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TcasEvent:
    """One observed TCAS RA event."""
    started_at: float
    ended_at: float | None
    ra_summary: str
    threat_icao: str | None
    rac_record: int
    multiple_threat: bool
    source: str  # "TC28" (ADS-B broadcast) or "DF16" (coordination reply)


@dataclass
class ReceiverAttribution:
    """Per-receiver counters for one aircraft.  Lets the UI show 'this
    aircraft was heard by RX home with -42 dBFS, by RX office with -28
    dBFS' and detect coverage gaps when one feed drops.

    `gen` is a monotonic counter incremented every time this attribution
    is updated.  In edge-mode deployments, the central merger compares
    incoming `gen` values against the last one it saw for the same
    (icao, receiver_id) pair to detect dropped/reordered deltas.  In
    monolithic mode it's harmless (just bumps locally and is never
    inspected).
    """
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    msg_counts: dict = field(default_factory=lambda: defaultdict(int))
    rssi_avg: float = -100.0
    rssi_samples: int = 0
    gen: int = 0

    def update(self, timestamp: float, df: int, rssi: float) -> None:
        self.last_seen = timestamp
        self.msg_counts[df] += 1
        if self.rssi_samples == 0:
            self.rssi_avg = rssi
        else:
            self.rssi_avg = 0.9 * self.rssi_avg + 0.1 * rssi
        self.rssi_samples += 1
        self.gen += 1

    def to_dict(self) -> dict:
        return {
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "msg_counts": dict(self.msg_counts),
            "rssi": self.rssi_avg,
            "rssi_samples": self.rssi_samples,
            "gen": self.gen,
        }


@dataclass
class Aircraft:
    """Everything we know about one aircraft, keyed by ICAO."""

    icao: str
    callsign: str | None = None
    category: str | None = None
    squawk: str | None = None

    # Geometry
    lat: float | None = None
    lon: float | None = None
    last_position_at: float | None = None
    alt_baro_ft: int | None = None
    alt_gnss_ft: int | None = None

    # Velocity
    gs_kt: float | None = None
    tas_kt: int | None = None
    ias_kt: int | None = None
    mach: float | None = None
    track_deg: float | None = None
    heading_deg: float | None = None
    vrate_baro_fpm: int | None = None
    vrate_ins_fpm: int | None = None
    vrate_fpm: int | None = None  # whichever we have most recent
    roll_deg: float | None = None
    track_rate_dps: float | None = None

    # Autopilot intent (BDS 4,0 or TC=29)
    sel_alt_mcp_ft: int | None = None
    sel_alt_fms_ft: int | None = None
    qnh_mb: float | None = None
    # `autopilot_modes` is the authoritative TC=29-sourced view used by
    # intent-change events and the primary "Modes:" UI line.
    autopilot_modes: dict = field(default_factory=dict)
    # `autopilot_modes_bds` is the (often divergent) BDS 4,0 view —
    # display-only, never feeds intent-change events.  Surfaced so the
    # operator can compare both readings side-by-side.
    autopilot_modes_bds: dict = field(default_factory=dict)
    selected_heading_deg: float | None = None

    # Surveillance state
    on_ground: bool | None = None
    flight_status: str | None = None
    alert: bool = False
    spi: bool = False
    nic: int | None = None
    nac_p: int | None = None
    sil: int | None = None
    adsb_version: int | None = None
    emergency: str | None = None

    # Atmosphere (BDS 4,4)
    wind_speed_kt: int | None = None
    wind_direction_deg: float | None = None
    static_air_temp_c: float | None = None

    # TCAS
    tcas_ra_active: bool = False
    tcas_ra_summary: str | None = None
    tcas_threat_icao: str | None = None
    tcas_ra_started_at: float | None = None
    tcas_ra_ended_at: float | None = None
    tcas_ra_history: list[TcasEvent] = field(default_factory=list)

    # Quality / metadata.  These are the *merged* counters across every
    # receiver that has heard this aircraft (kept for back-compat with
    # callers expecting a single-receiver view).  Per-receiver
    # breakdowns live in `by_receiver`.
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    msg_counts: dict = field(default_factory=lambda: defaultdict(int))
    rssi_avg: float = -100.0
    rssi_samples: int = 0
    bds_observed: set = field(default_factory=set)

    # Per-receiver attribution: which receivers have heard this aircraft,
    # with their own RSSI / msg counts.  Empty before the first frame.
    by_receiver: dict = field(default_factory=dict)

    # Static info from DB lookup (registration, type, country, operator).
    # This is an `AircraftInfo` instance; we store it as `Any` to avoid
    # a circular import.  Computed once on first sight, refreshed when
    # callsign changes (so operator can be re-resolved).
    db_info: object = None
    _db_info_callsign: str | None = None  # callsign at last lookup

    # Route enrichment (origin/destination/airline) from the external
    # adsbdb.com lookup.  Populated asynchronously by RouteResolver when
    # route enrichment is enabled; otherwise None.  Keyed by callsign,
    # so it persists across re-resolutions of the same callsign.
    route: dict | None = None
    _route_callsign: str | None = None  # callsign at last route fetch

    # Pending autopilot/intent changes awaiting confirmation.
    # BDS 4,0 inference can occasionally false-positive on a single message,
    # so we require a new value to appear in two consecutive observations
    # before emitting a "changed" event to the UI.  Maps field name to the
    # candidate value last seen but not yet confirmed.
    _pending_intent: dict = field(default_factory=dict)

    # Source ("BDS40" / "TC29") of the value currently held in each
    # selected-altitude field.  Used to suppress cross-source regridding
    # noise (BDS 4,0 has a 16 ft LSB; TC=29 lands on a 32 ft grid, so the
    # same physical selection can show as e.g. 37024 ft via TC29 then
    # 37000 ft via BDS40 without anything actually changing).
    _sel_alt_source: dict = field(default_factory=dict)

    # Position trail
    trail: deque = field(default_factory=lambda: deque(maxlen=120))

    # Internal: CPR pair buffer (private; not serialised).
    # Keyed by receiver_id so the engine can pair an even and odd half
    # only when they came from the SAME receiver.  Cross-receiver
    # pairing is unsafe because the wall-clock timestamps used for the
    # 10-second pair window can drift between distributed receivers,
    # which can produce spurious global decodes.
    _cpr_even: dict = field(default_factory=dict)  # receiver_id -> (ts, lat_cpr, lon_cpr, msg)
    _cpr_odd: dict = field(default_factory=dict)
    _cpr_even_surface: dict = field(default_factory=dict)
    _cpr_odd_surface: dict = field(default_factory=dict)

    # Internal: previous fix for plausibility check
    _prev_lat: float | None = None
    _prev_lon: float | None = None
    _prev_position_at: float | None = None

    def update_seen(
        self,
        timestamp: float,
        df: int,
        rssi: float,
        receiver_id: str = "default",
    ) -> None:
        """Record one received frame.  Updates the merged top-level
        counters AND the per-receiver bucket so UI clients can see both
        a single "best" RSSI and the per-RX breakdown."""
        # Merged view (existing behaviour).
        self.last_seen = timestamp
        self.msg_counts[df] += 1
        if self.rssi_samples == 0:
            self.rssi_avg = rssi
        else:
            self.rssi_avg = 0.9 * self.rssi_avg + 0.1 * rssi
        self.rssi_samples += 1
        # Per-receiver attribution.
        bucket = self.by_receiver.get(receiver_id)
        if bucket is None:
            bucket = ReceiverAttribution(first_seen=timestamp,
                                         last_seen=timestamp)
            self.by_receiver[receiver_id] = bucket
        bucket.update(timestamp, df, rssi)

    def record_position(self, lat: float, lon: float, t: float) -> None:
        self._prev_lat = self.lat
        self._prev_lon = self.lon
        self._prev_position_at = self.last_position_at
        self.lat = lat
        self.lon = lon
        self.last_position_at = t
        self.trail.append((t, lat, lon, self.alt_baro_ft))

    def to_dict(self) -> dict[str, Any]:
        """Serialise the public state for the WebSocket."""
        info_dict = None
        if self.db_info is not None:
            i = self.db_info
            info_dict = {
                "registration": getattr(i, "registration", None),
                "registration_source": getattr(i, "registration_source", None),
                "type_code": getattr(i, "type_code", None),
                "description": getattr(i, "description", None),
                "country_code": getattr(i, "country_code", None),
                "country_name": getattr(i, "country_name", None),
                "is_military": getattr(i, "is_military", False),
                "is_pia": getattr(i, "is_pia", False),
                "is_interesting": getattr(i, "is_interesting", False),
                "is_ladd": getattr(i, "is_ladd", False),
            }
            ti = getattr(i, "type_info", None)
            if ti is not None:
                info_dict["type"] = {
                    "manufacturer": ti.manufacturer,
                    "model": ti.model,
                    "wtc": ti.wtc,
                    "engine_type": ti.engine_type,
                    "engine_count": ti.engine_count,
                }
            op = getattr(i, "operator", None)
            if op is not None:
                info_dict["operator"] = {
                    "designator": op.designator,
                    "name": op.name,
                    "country": op.country,
                    "callsign": op.callsign,
                }
        return {
            "icao": self.icao,
            "callsign": self.callsign,
            "category": self.category,
            "squawk": self.squawk,
            "lat": self.lat,
            "lon": self.lon,
            "last_position_at": self.last_position_at,
            "alt_baro_ft": self.alt_baro_ft,
            "alt_gnss_ft": self.alt_gnss_ft,
            "gs_kt": self.gs_kt,
            "tas_kt": self.tas_kt,
            "ias_kt": self.ias_kt,
            "mach": self.mach,
            "track_deg": self.track_deg,
            "heading_deg": self.heading_deg,
            "vrate_fpm": self.vrate_fpm,
            "vrate_baro_fpm": self.vrate_baro_fpm,
            "vrate_ins_fpm": self.vrate_ins_fpm,
            "roll_deg": self.roll_deg,
            "track_rate_dps": self.track_rate_dps,
            "sel_alt_mcp_ft": self.sel_alt_mcp_ft,
            "sel_alt_fms_ft": self.sel_alt_fms_ft,
            "qnh_mb": self.qnh_mb,
            "autopilot_modes": self.autopilot_modes,
            "autopilot_modes_bds": self.autopilot_modes_bds,
            "selected_heading_deg": self.selected_heading_deg,
            "on_ground": self.on_ground,
            "flight_status": self.flight_status,
            "alert": self.alert,
            "spi": self.spi,
            "nic": self.nic,
            "nac_p": self.nac_p,
            "sil": self.sil,
            "adsb_version": self.adsb_version,
            "emergency": self.emergency,
            "wind_speed_kt": self.wind_speed_kt,
            "wind_direction_deg": self.wind_direction_deg,
            "static_air_temp_c": self.static_air_temp_c,
            "tcas_ra_active": self.tcas_ra_active,
            "tcas_ra_summary": self.tcas_ra_summary,
            "tcas_threat_icao": self.tcas_threat_icao,
            "tcas_ra_started_at": self.tcas_ra_started_at,
            "tcas_ra_ended_at": self.tcas_ra_ended_at,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "msg_counts": dict(self.msg_counts),
            "rssi": self.rssi_avg,
            "bds_observed": sorted(self.bds_observed),
            "trail": list(self.trail),
            "info": info_dict,
            "route": self.route,
            "by_receiver": {
                rid: att.to_dict() for rid, att in self.by_receiver.items()
            },
        }
