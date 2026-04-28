"""Mode S surveillance and Comm-B decoders.

Covers:
- DF0/4/16/20: altitude code (AC field)
- DF5/21: identity code (squawk, ID field)
- DF20/21 MB field: BDS 1,0 / 2,0 / 4,0 / 4,4 / 5,0 / 6,0 register decoders
- BDS inference: try each candidate, return all that pass sanity checks

Reference: Sun (2021) ch. 13, 15-18; ICAO Doc 9871.
"""
from __future__ import annotations

from dataclasses import dataclass

from .adsb import _gillham_to_alt
from .common import get_bit, get_bits, hex_to_bin, twos_complement


# ---------------------------------------------------------------------------
# Mode S altitude code (AC field, 13 bits, in DF 0/4/16/20 at bits 20-32)
# ---------------------------------------------------------------------------

def altitude_code(msg: str) -> int | None:
    """Decode the 13-bit AC field (Mode S altitude reply) to feet.

    The AC field has a different layout from the 12-bit ADS-B altitude:
        bits 20-32 = C1 A1 C2 A2 C4 A4 M B1 Q B2 D2 B4 D4
    where M=0 (metric flag) and Q=1 means 25-ft increment.
    """
    ac = get_bits(msg, 20, 13)
    if ac == 0:
        return None
    # M-bit at position 6 of the 13-bit field (1-indexed from left = bit 26 of msg)
    m_bit = (ac >> 6) & 1
    if m_bit:
        return None  # metric encoding, rare and not currently used by transponders
    # Q-bit at position 8 of the field
    q_bit = (ac >> 4) & 1
    if q_bit:
        # Strip M (bit 6) and Q (bit 4), concatenate the remaining 11 bits.
        # AC bits (MSB first): C1 A1 C2 A2 C4 A4 M B1 Q B2 D2 B4 D4
        # Remove M (bit index 6 from left) and Q (bit index 8 from left).
        bits = format(ac, "013b")
        n_bits = bits[:6] + bits[7:8] + bits[9:]   # drop M then Q
        n = int(n_bits, 2)
        return 25 * n - 1000
    else:
        # 100-ft Gillham. Drop the M bit only and decode the 12 remaining bits.
        bits = format(ac, "013b")
        gill = int(bits[:6] + bits[7:], 2)
        return _gillham_to_alt(gill)


# ---------------------------------------------------------------------------
# Mode S identity code (squawk) — DF5/21 ID field, 13 bits at msg bits 20-32
# ---------------------------------------------------------------------------

def identity_code(msg: str) -> str | None:
    """Decode the 13-bit ID field to a 4-octal-digit Mode A squawk."""
    id_bits = get_bits(msg, 20, 13)
    if id_bits == 0:
        return None
    # Standard pulse mapping: C1 A1 C2 A2 C4 A4 X B1 D1 B2 D2 B4 D4
    bits = format(id_bits, "013b")
    C1, A1, C2, A2, C4, A4, _X, B1, D1, B2, D2, B4, D4 = (int(b) for b in bits)
    a = (A4 << 2) | (A2 << 1) | A1
    b = (B4 << 2) | (B2 << 1) | B1
    c = (C4 << 2) | (C2 << 1) | C1
    d = (D4 << 2) | (D2 << 1) | D1
    return f"{a}{b}{c}{d}"


# ---------------------------------------------------------------------------
# Flight Status (FS field, 3 bits, in DF4/5/20/21)
# ---------------------------------------------------------------------------

FLIGHT_STATUS = {
    0: "airborne",
    1: "on-ground",
    2: "alert + airborne",
    3: "alert + on-ground",
    4: "alert + SPI",
    5: "SPI",
}


def flight_status(msg: str) -> dict | None:
    """Decode FS field (bits 6-8 of DF4/5/20/21)."""
    fs = get_bits(msg, 6, 3)
    label = FLIGHT_STATUS.get(fs)
    if label is None:
        return None
    return {
        "raw": fs,
        "label": label,
        "alert": fs in (2, 3, 4),
        "spi": fs in (4, 5),
        "on_ground": fs in (1, 3),
    }


# ---------------------------------------------------------------------------
# Comm-B MB field decoders (56 bits, msg bits 33-88)
# ---------------------------------------------------------------------------

@dataclass
class BDS40:
    """Selected vertical intention.

    Note: the spec also defines VNAV / ALT_HOLD / APPROACH mode flags at
    MB bits 49-51, but in practice many transponder firmwares encode
    those bits inconsistently with TC=29 (ADS-B v2's "target state and
    status").  ADS-B v2 introduced TC=29 specifically to replace the
    BDS 4,0 mode flags, so we ignore them here and treat TC=29 as the
    sole source of truth for autopilot mode state.
    """
    mcp_alt_ft: int | None
    fms_alt_ft: int | None
    qnh_mb: float | None
    target_alt_source: int | None  # 0/1/2/3 per spec


@dataclass
class BDS50:
    """Track and turn report."""
    roll_deg: float | None
    track_deg: float | None
    gs_kt: int | None
    track_rate_dps: float | None
    tas_kt: int | None


@dataclass
class BDS60:
    """Heading and speed report."""
    heading_deg: float | None
    ias_kt: int | None
    mach: float | None
    vrate_baro_fpm: int | None
    vrate_ins_fpm: int | None


@dataclass
class BDS44:
    """Meteorological routine air report (MRAR)."""
    wind_speed_kt: int | None
    wind_direction_deg: float | None
    static_air_temp_c: float | None
    static_pressure_mb: int | None
    turbulence: int | None
    humidity_pct: float | None


def decode_bds_10(msg: str) -> dict | None:
    """BDS 1,0 — Datalink capability report.

    The first 8 bits of MB are the BDS code (= 0x10).  Self-identifying.
    """
    if get_bits(msg, 33, 8) != 0x10:
        return None
    # We don't decode every cap bit; just confirm and report.
    return {"bds": "1,0", "datalink_capability": True}


def decode_bds_20(msg: str) -> str | None:
    """BDS 2,0 — Aircraft identification (callsign).

    First 8 bits of MB = 0x20, then 8 6-bit chars.
    """
    if get_bits(msg, 33, 8) != 0x20:
        return None
    from .adsb import _CALLSIGN_CHARS
    bits = hex_to_bin(msg)
    chars = []
    # MB bits 9-56 = msg bits 41-88
    for i in range(8):
        c = int(bits[40 + i * 6 : 40 + (i + 1) * 6], 2)
        chars.append(_CALLSIGN_CHARS[c])
    cs = "".join(chars).rstrip("_ ").rstrip()
    return cs if cs and "#" not in cs else None


def decode_bds_40(msg: str) -> BDS40 | None:
    """BDS 4,0 — Selected vertical intention."""
    # Status bits at MB:1, MB:14, MB:27, and MB:48. msg bit = MB bit + 32.
    s_mcp = get_bit(msg, 33)
    mcp_raw = get_bits(msg, 34, 12) * 16 if s_mcp else None
    s_fms = get_bit(msg, 46)
    fms_raw = get_bits(msg, 47, 12) * 16 if s_fms else None
    s_baro = get_bit(msg, 59)
    qnh = (get_bits(msg, 60, 12) * 0.1) + 800 if s_baro else None
    # Mode flag region (msg bits 80-83) is parsed only for the reserved-bits
    # validator below; the values themselves are intentionally not exposed.
    # See class docstring for why.
    s_src = get_bit(msg, 86)
    src = get_bits(msg, 87, 2) if s_src else None

    # Real-world MCP/FMS altitudes are always set in 100 ft increments on
    # the FCU, but the BDS 4,0 wire format uses a 16 ft LSB.  So a real
    # selected altitude of 3000 ft transmits as floor(3000/16)*16 = 2992 ft.
    # Snap each value to the nearest 100 ft, rejecting only values that
    # are too far from any 100-ft boundary (which would indicate noise,
    # not quantisation).
    def _snap_to_hundred(raw: int | None) -> int | None:
        if raw is None:
            return None
        rounded = round(raw / 100) * 100
        # 16-ft LSB => max quantisation error is ±8 ft from the rounded
        # value.  Allow 12 ft to be safe; reject anything further as noise.
        if abs(raw - rounded) > 12:
            return -1   # sentinel: invalid
        return rounded

    mcp = _snap_to_hundred(mcp_raw)
    fms = _snap_to_hundred(fms_raw)
    if mcp == -1 or fms == -1:
        return None  # not a real selected altitude

    # Sanity: at least one real field; tighter ranges to avoid BDS-5,0/6,0
    # false positives.  Real ATC selected altitudes are 0-50000 ft and
    # QNH 950-1050 mb in 99% of operations.
    if mcp is None and fms is None and qnh is None:
        return None
    if mcp is not None and not (0 <= mcp <= 50000):
        return None
    if fms is not None and not (0 <= fms <= 50000):
        return None
    if qnh is not None and not (950 <= qnh <= 1050):
        return None

    # Reserved bits between QNH (MB:39 ends at msg 71) and mode-status
    # (MB:48 at msg 80) should all be zero per ICAO Annex 10 Vol IV.
    # Genuine BDS 4,0 messages put zeros here; BDS 5,0 / 6,0 messages
    # carry data in this range, so checking it is a sharp false-positive
    # filter.  Same for the 2 reserved bits before s_src (msg 84-85).
    reserved_72_79 = get_bits(msg, 72, 8)
    reserved_84_85 = get_bits(msg, 84, 2)
    if reserved_72_79 != 0 or reserved_84_85 != 0:
        return None

    return BDS40(mcp, fms, qnh, src)


def decode_bds_50(msg: str) -> BDS50 | None:
    """BDS 5,0 — Track and turn report."""
    s_roll = get_bit(msg, 33)
    if s_roll:
        sign = get_bit(msg, 34)
        val = get_bits(msg, 35, 9)
        roll = twos_complement((sign << 9) | val, 10) * (45.0 / 256)
    else:
        roll = None

    s_trk = get_bit(msg, 44)
    if s_trk:
        sign = get_bit(msg, 45)
        val = get_bits(msg, 46, 10)
        trk = twos_complement((sign << 10) | val, 11) * (90.0 / 512)
        if trk < 0:
            trk += 360
    else:
        trk = None

    s_gs = get_bit(msg, 56)
    gs = get_bits(msg, 57, 10) * 2 if s_gs else None

    s_rtrk = get_bit(msg, 67)
    if s_rtrk:
        sign = get_bit(msg, 68)
        val = get_bits(msg, 69, 9)
        rtrk = twos_complement((sign << 9) | val, 10) * (8.0 / 256)
    else:
        rtrk = None

    s_tas = get_bit(msg, 78)
    tas = get_bits(msg, 79, 10) * 2 if s_tas else None

    # Sanity check: implausible values disqualify.
    if roll is not None and not (-50 <= roll <= 50):
        return None
    if trk is not None and not (0 <= trk < 360):
        return None
    if gs is not None and not (0 <= gs <= 700):
        return None
    if tas is not None and not (0 <= tas <= 700):
        return None
    if rtrk is not None and not (-16 <= rtrk <= 16):
        return None
    # Must have at least one valid field.
    if all(v is None for v in (roll, trk, gs, rtrk, tas)):
        return None
    return BDS50(roll, trk, gs, rtrk, tas)


def decode_bds_60(msg: str) -> BDS60 | None:
    """BDS 6,0 — Heading and speed report."""
    s_hdg = get_bit(msg, 33)
    if s_hdg:
        sign = get_bit(msg, 34)
        val = get_bits(msg, 35, 10)
        hdg = twos_complement((sign << 10) | val, 11) * (90.0 / 512)
        if hdg < 0:
            hdg += 360
    else:
        hdg = None

    s_ias = get_bit(msg, 45)
    ias = get_bits(msg, 46, 10) if s_ias else None

    s_mach = get_bit(msg, 56)
    mach = get_bits(msg, 57, 10) * 0.004 if s_mach else None

    s_vbaro = get_bit(msg, 67)
    if s_vbaro:
        sign = get_bit(msg, 68)
        val = get_bits(msg, 69, 9)
        vbaro = twos_complement((sign << 9) | val, 10) * 32
    else:
        vbaro = None

    s_vins = get_bit(msg, 78)
    if s_vins:
        sign = get_bit(msg, 79)
        val = get_bits(msg, 80, 9)
        vins = twos_complement((sign << 9) | val, 10) * 32
    else:
        vins = None

    # Sanity
    if hdg is not None and not (0 <= hdg < 360):
        return None
    if ias is not None and not (0 <= ias <= 600):
        return None
    if mach is not None and not (0 <= mach <= 0.95):
        return None
    if vbaro is not None and not (-8000 <= vbaro <= 8000):
        return None
    if vins is not None and not (-8000 <= vins <= 8000):
        return None
    if all(v is None for v in (hdg, ias, mach, vbaro, vins)):
        return None
    # Cross-field consistency: if both Mach and IAS available at high alt
    # they should be roughly coherent (Mach * 600 ≈ IAS at FL300+).
    return BDS60(hdg, ias, mach, vbaro, vins)


def decode_bds_44(msg: str) -> BDS44 | None:
    """BDS 4,4 — Meteorological routine air report (MRAR)."""
    fom = get_bits(msg, 33, 4)  # figure of merit / source
    if fom not in (0, 1, 2, 3, 4):
        return None
    s_wind = get_bit(msg, 37)
    if s_wind:
        wind_speed = get_bits(msg, 38, 9)
        wind_dir = get_bits(msg, 47, 9) * (180.0 / 256)
    else:
        wind_speed = wind_dir = None
    s_sat = get_bit(msg, 56)
    if s_sat:
        sign = get_bit(msg, 57)
        val = get_bits(msg, 58, 10)
        sat = twos_complement((sign << 10) | val, 11) * 0.25
    else:
        sat = None
    s_pres = get_bit(msg, 68)
    pres = get_bits(msg, 69, 11) if s_pres else None
    s_turb = get_bit(msg, 80)
    turb = get_bits(msg, 81, 2) if s_turb else None
    s_hum = get_bit(msg, 83)
    hum = get_bits(msg, 84, 6) * (100.0 / 64) if s_hum else None

    if all(v is None for v in (wind_speed, sat, pres, turb, hum)):
        return None
    # Sanity
    if wind_speed is not None and wind_speed > 250:
        return None
    if sat is not None and not (-80 <= sat <= 60):
        return None
    if pres is not None and not (0 <= pres <= 2048):
        return None
    return BDS44(wind_speed, wind_dir, sat, pres, turb, hum)


# ---------------------------------------------------------------------------
# BDS inference: try each candidate; return all that pass sanity
# ---------------------------------------------------------------------------

@dataclass
class BdsInferenceResult:
    """A candidate BDS decode plus its confidence level."""
    bds_code: str
    decoded: object
    confidence: str  # "high" | "medium" | "low"


_BDS_DECODERS = [
    ("1,0", decode_bds_10),
    ("2,0", decode_bds_20),
    ("4,0", decode_bds_40),
    ("4,4", decode_bds_44),
    ("5,0", decode_bds_50),
    ("6,0", decode_bds_60),
]


def infer_bds(msg: str, ac_state: dict | None = None) -> list[BdsInferenceResult]:
    """Try every BDS decoder; return the candidates that pass sanity.

    `ac_state` is the current Aircraft snapshot; if provided, candidates that
    cross-check against existing ADS-B-derived state get higher confidence.
    """
    candidates: list[BdsInferenceResult] = []
    for code, fn in _BDS_DECODERS:
        result = fn(msg)
        if result is None:
            continue
        confidence = "medium"
        # Cross-check tightening:
        if ac_state and code == "5,0" and isinstance(result, BDS50):
            adsb_track = ac_state.get("track_deg")
            adsb_gs = ac_state.get("gs_kt")
            if adsb_track is not None and result.track_deg is not None:
                if abs((result.track_deg - adsb_track + 540) % 360 - 180) < 15:
                    confidence = "high"
            if adsb_gs is not None and result.gs_kt is not None:
                if abs(result.gs_kt - adsb_gs) > 50:
                    confidence = "low"
        if ac_state and code == "6,0" and isinstance(result, BDS60):
            # If we have a Mach and an altitude > FL250, sanity is tighter.
            alt = ac_state.get("alt_baro_ft")
            if alt and alt > 25000 and result.mach and 0.6 < result.mach < 0.9:
                confidence = "high"
        if code in ("1,0", "2,0"):
            confidence = "high"  # self-identifying via 0x10 / 0x20 header
        candidates.append(BdsInferenceResult(code, result, confidence))
    return candidates


# ---------------------------------------------------------------------------
# ACAS / TCAS coordination reply (DF16)
# ---------------------------------------------------------------------------

@dataclass
class TcasCoordination:
    """DF16 with VDS=3,0: TCAS coordination reply."""
    active_ra: int
    rac_record: int
    ra_terminated: bool
    multiple_threat: bool
    summary: str
    sensitivity_level: int


def decode_tcas_coordination(msg: str) -> TcasCoordination | None:
    """Decode DF16 ACAS coordination reply (MV field, VDS=3,0)."""
    from .common import df
    if df(msg) != 16:
        return None
    # MV bits 1-8 = VDS (msg bits 33-40)
    vds = get_bits(msg, 33, 8)
    if vds != 0x30:
        return None  # Not an RA coordination reply (likely Comm-B fallback)
    sl = get_bits(msg, 9, 3)
    ara = get_bits(msg, 41, 14)
    rac = get_bits(msg, 55, 4)
    rat = bool(get_bit(msg, 59))
    mte = bool(get_bit(msg, 60))
    from .adsb import tcas_ra_summary
    return TcasCoordination(
        active_ra=ara,
        rac_record=rac,
        ra_terminated=rat,
        multiple_threat=mte,
        summary=tcas_ra_summary(ara, mte),
        sensitivity_level=sl,
    )
