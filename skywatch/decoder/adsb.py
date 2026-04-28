"""ADS-B Extended Squitter (DF17/18) decoder.

Reference: ICAO Doc 9871; Sun (2021) ch. 3-9.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .common import get_bit, get_bits, hex_to_bin, twos_complement


# ---------------------------------------------------------------------------
# Type code dispatch
# ---------------------------------------------------------------------------

def typecode(msg: str) -> int:
    """ADS-B Type Code (first 5 bits of ME field = bits 33-37)."""
    return get_bits(msg, 33, 5)


def adsb_category(msg: str) -> tuple[int, int]:
    """Return (TC, CA) for identification messages.

    The combination of TC and CA gives the wake-turbulence category
    per ICAO Doc 9871 Table A-2-8.
    """
    tc = typecode(msg)
    ca = get_bits(msg, 38, 3)
    return tc, ca


WAKE_CATEGORIES = {
    # (TC, CA) -> human-readable category
    (1, 0): "No category info",
    (2, 1): "Surface emergency vehicle",
    (2, 3): "Surface service vehicle",
    (2, 4): "Ground obstruction",
    (2, 5): "Ground obstruction",
    (2, 6): "Ground obstruction",
    (2, 7): "Ground obstruction",
    (3, 1): "Glider / sailplane",
    (3, 2): "Lighter-than-air",
    (3, 3): "Parachutist / skydiver",
    (3, 4): "Ultralight / hang-glider",
    (3, 6): "UAV",
    (3, 7): "Space vehicle",
    (4, 1): "Light (< 7000 kg)",
    (4, 2): "Medium 1 (7000-34000 kg)",
    (4, 3): "Medium 2 (34000-136000 kg)",
    (4, 4): "High vortex (e.g. B757)",
    (4, 5): "Heavy (> 136000 kg)",
    (4, 6): "High performance (>5g, >400kt)",
    (4, 7): "Rotorcraft",
}


# ---------------------------------------------------------------------------
# TC 1-4: Aircraft identification (callsign + category)
# ---------------------------------------------------------------------------

# 6-bit Mode S character set (ICAO Annex 10 Vol IV §3.1.2.9.1.2).
_CALLSIGN_CHARS = (
    "#ABCDEFGHIJKLMNOPQRSTUVWXYZ#####"  # 0-31 (0 reserved, 27-31 reserved)
    " ###############0123456789######"  # 32-63 (space + digits)
)


def callsign(msg: str) -> str | None:
    """Decode the 8-char callsign from a TC=1-4 ADS-B message."""
    tc = typecode(msg)
    if tc < 1 or tc > 4:
        return None
    bits = hex_to_bin(msg)
    # ME field is bits 33-88; the 8 callsign chars start at ME bit 9 (msg bit 41).
    chars = []
    for i in range(8):
        c = int(bits[40 + i * 6 : 40 + (i + 1) * 6], 2)
        chars.append(_CALLSIGN_CHARS[c])
    cs = "".join(chars).rstrip("_ ").rstrip()
    # Filter out '#' (reserved/invalid markers).
    return cs if cs and "#" not in cs else None


def wake_category(msg: str) -> str | None:
    tc, ca = adsb_category(msg)
    return WAKE_CATEGORIES.get((tc, ca))


# ---------------------------------------------------------------------------
# TC 9-22: Position (Compact Position Reporting)
# ---------------------------------------------------------------------------

NZ = 15  # Number of latitude zones between equator and pole


def _nl(lat: float) -> int:
    """NL(lat) - number of longitude zones at this latitude.

    Per ICAO Annex 10 Vol IV §3.1.2.6.5.4.2.
    """
    if lat == 0:
        return 59
    if abs(lat) >= 87:
        return 1 if abs(lat) > 87 else 2
    try:
        nz_term = 1 - math.cos(math.pi / (2 * NZ))
        denom = math.cos(math.pi * abs(lat) / 180.0) ** 2
        return int(math.floor(2 * math.pi / math.acos(1 - nz_term / denom)))
    except (ValueError, ZeroDivisionError):
        return 1


def cpr_lat_lon(msg: str) -> tuple[int, int]:
    """Extract raw 17-bit CPR latitude and longitude integers from a position msg."""
    lat_cpr = get_bits(msg, 55, 17)
    lon_cpr = get_bits(msg, 72, 17)
    return lat_cpr, lon_cpr


def cpr_format(msg: str) -> int:
    """0 = even frame, 1 = odd frame."""
    return get_bit(msg, 54)


def position_global(
    msg_even: str, msg_odd: str, t_even: float, t_odd: float
) -> tuple[float, float] | None:
    """Globally unambiguous airborne CPR decode.

    Requires a fresh even/odd pair (within ~10s).  Returns None if the
    pair straddles a latitude zone boundary (NL mismatch).
    """
    lat_e_cpr, lon_e_cpr = cpr_lat_lon(msg_even)
    lat_o_cpr, lon_o_cpr = cpr_lat_lon(msg_odd)

    lat_e = lat_e_cpr / 131072.0
    lat_o = lat_o_cpr / 131072.0
    lon_e = lon_e_cpr / 131072.0
    lon_o = lon_o_cpr / 131072.0

    # Latitude zone index
    j = math.floor(59 * lat_e - 60 * lat_o + 0.5)
    d_lat_e = 360.0 / 60
    d_lat_o = 360.0 / 59

    lat_even = d_lat_e * ((j % 60) + lat_e)
    lat_odd = d_lat_o * ((j % 59) + lat_o)
    if lat_even >= 270:
        lat_even -= 360
    if lat_odd >= 270:
        lat_odd -= 360

    if _nl(lat_even) != _nl(lat_odd):
        # Pair straddles a longitude zone — wait for the next pair.
        return None

    if t_even >= t_odd:
        lat = lat_even
        nl = _nl(lat)
        ni = max(nl, 1)
        m = math.floor(lon_e * (nl - 1) - lon_o * nl + 0.5)
        lon = (360.0 / ni) * ((m % ni) + lon_e)
    else:
        lat = lat_odd
        nl = _nl(lat)
        ni = max(nl - 1, 1)
        m = math.floor(lon_e * (nl - 1) - lon_o * nl + 0.5)
        lon = (360.0 / ni) * ((m % ni) + lon_o)

    if lon >= 180:
        lon -= 360

    return lat, lon


def position_local(msg: str, lat_ref: float, lon_ref: float) -> tuple[float, float]:
    """Locally unambiguous decode using a known reference within 180 NM."""
    lat_cpr, lon_cpr = cpr_lat_lon(msg)
    lat_cpr_n = lat_cpr / 131072.0
    lon_cpr_n = lon_cpr / 131072.0
    i = cpr_format(msg)

    d_lat = 360.0 / (60 - i)
    j = math.floor(lat_ref / d_lat) + math.floor(
        ((lat_ref % d_lat) / d_lat) - lat_cpr_n + 0.5
    )
    lat = d_lat * (j + lat_cpr_n)

    nl = _nl(lat)
    d_lon = 360.0 / max(nl - i, 1)
    m = math.floor(lon_ref / d_lon) + math.floor(
        ((lon_ref % d_lon) / d_lon) - lon_cpr_n + 0.5
    )
    lon = d_lon * (m + lon_cpr_n)
    if lon >= 180:
        lon -= 360
    return lat, lon


# ---------------------------------------------------------------------------
# Altitude
# ---------------------------------------------------------------------------

def altitude(msg: str) -> int | None:
    """Decode altitude from an airborne position message (TC 9-18 or 20-22).

    TC 9-18: barometric altitude in feet (Q-bit + 11-bit value, or Gillham).
    TC 20-22: GNSS height in metres -> we convert to feet.
    Returns None if altitude unavailable.
    """
    tc = typecode(msg)
    if not (9 <= tc <= 22) or tc in (19,):
        return None

    alt_bits = get_bits(msg, 41, 12)
    if alt_bits == 0:
        return None

    if 9 <= tc <= 18:
        # Barometric altitude
        q = (alt_bits >> 4) & 1
        if q:
            # 25 ft increments. Strip Q-bit (bit position 4 of the 12-bit field).
            n = ((alt_bits >> 5) << 4) | (alt_bits & 0x0F)
            return 25 * n - 1000
        else:
            # 100 ft Gillham — see _gillham_to_alt below.
            return _gillham_to_alt(alt_bits)
    else:
        # TC 20-22: GNSS height in metres
        return int(alt_bits * 3.28084)  # m -> ft


def _gillham_to_alt(code: int) -> int | None:
    """Decode 11-bit Gillham (modified Gray) altitude code to feet.

    Bit layout in the 12-bit ALT field with Q=0:
        C1 A1 C2 A2 C4 A4 (M=0) B1 D1 B2 D2 B4 D4
    where the M-bit is at position 6 (= the Q-bit slot).
    Standard Gillham decoding per Honeywell/ARINC 575.
    """
    # Map the 12-bit ALT field to the named pulses.
    bits = format(code, "012b")
    C1, A1, C2, A2, C4, A4, _M, B1, D1, B2, D2, B4 = (int(b) for b in bits[:12])
    # The 100ft 'C' digit (centi-feet group) — ranges 1-5
    n100 = _gray_to_int((C1 << 2) | (C2 << 1) | C4)
    if n100 in (0, 6) or n100 == 7:
        # 0 and 6 are invalid; 7 means "test" - treat as unavailable.
        return None
    if n100 == 5:
        n100 = 0
    # The 500ft component — 8-bit reflected Gray: D1 D2 D4 A1 A2 A4 B1 B2 B4
    n500_gray = (
        (D1 << 7) | (D2 << 6) | (B4 << 5) | (B2 << 4) | (B1 << 3)
        | (A4 << 2) | (A2 << 1) | A1
    )
    # The above ordering follows the standard Gillham table; verify with test.
    n500 = _gray_to_int(n500_gray)
    # Odd 500ft increments invert the 100ft direction.
    if n500 % 2:
        n100 = 6 - n100
    return (n500 * 500) + (n100 * 100) - 1200


def _gray_to_int(g: int) -> int:
    """Convert a reflected binary Gray code to an integer."""
    n = g
    while g:
        g >>= 1
        n ^= g
    return n


def surveillance_status(msg: str) -> int:
    """SS field of position messages: 0=none, 1=permanent alert,
    2=temporary alert, 3=SPI."""
    return get_bits(msg, 38, 2)


# ---------------------------------------------------------------------------
# TC 19: Velocity
# ---------------------------------------------------------------------------

@dataclass
class Velocity:
    speed: float | None       # GS or AS in knots
    track: float | None       # ground track angle (deg true)
    heading: float | None     # magnetic heading (deg) for sub-types 3/4
    vrate: int | None         # vertical rate (ft/min, +ve = climb)
    vrate_source: str | None  # "baro" or "gnss"
    speed_type: str           # "GS", "TAS", "IAS"
    gnss_baro_diff: int | None  # ft, +ve = GNSS above baro


def velocity(msg: str) -> Velocity | None:
    """Decode TC=19 airborne velocity message."""
    if typecode(msg) != 19:
        return None
    subtype = get_bits(msg, 38, 3)

    # Vertical rate (common to all subtypes)
    vr_src = "baro" if get_bit(msg, 36) else "gnss"
    s_vr = get_bit(msg, 37)
    vr_raw = get_bits(msg, 38, 9)  # bits 38-46 of ME, but our offsets shift
    # Re-extract correctly: ME bits 36-46 = msg bits 68-78
    vr_src = "baro" if get_bit(msg, 68) else "gnss"
    s_vr = get_bit(msg, 69)
    vr_raw = get_bits(msg, 70, 9)
    if vr_raw == 0:
        vrate = None
    else:
        vrate = 64 * (vr_raw - 1) * (-1 if s_vr else 1)

    # GNSS-baro altitude difference
    s_dif = get_bit(msg, 81)
    d_alt = get_bits(msg, 82, 7)
    gnss_baro_diff = None if d_alt == 0 else (-1 if s_dif else 1) * 25 * (d_alt - 1)

    if subtype in (1, 2):
        s_ew = get_bit(msg, 46)
        v_ew = get_bits(msg, 47, 10)
        s_ns = get_bit(msg, 57)
        v_ns = get_bits(msg, 58, 10)
        if v_ew == 0 or v_ns == 0:
            return Velocity(None, None, None, vrate, vr_src, "GS", gnss_baro_diff)
        scale = 4 if subtype == 2 else 1
        vx = scale * (v_ew - 1) * (-1 if s_ew else 1)
        vy = scale * (v_ns - 1) * (-1 if s_ns else 1)
        speed = math.sqrt(vx * vx + vy * vy)
        track = math.degrees(math.atan2(vx, vy)) % 360
        return Velocity(speed, track, None, vrate, vr_src, "GS", gnss_baro_diff)

    elif subtype in (3, 4):
        sh = get_bit(msg, 46)
        hdg_raw = get_bits(msg, 47, 10)
        heading = hdg_raw * 360.0 / 1024 if sh else None
        t_bit = get_bit(msg, 57)
        as_raw = get_bits(msg, 58, 10)
        if as_raw == 0:
            return Velocity(None, None, heading, vrate, vr_src,
                            "TAS" if t_bit else "IAS", gnss_baro_diff)
        scale = 4 if subtype == 4 else 1
        airspeed = scale * (as_raw - 1)
        return Velocity(airspeed, None, heading, vrate, vr_src,
                        "TAS" if t_bit else "IAS", gnss_baro_diff)
    return None


# ---------------------------------------------------------------------------
# TC 28: Aircraft status (emergency / TCAS RA broadcast)
# ---------------------------------------------------------------------------

EMERGENCY_STATES = {
    0: None,
    1: "GENERAL EMERGENCY",
    2: "MEDICAL EMERGENCY",
    3: "MINIMUM FUEL",
    4: "NO COMMUNICATIONS",
    5: "UNLAWFUL INTERFERENCE",
    6: "DOWNED AIRCRAFT",
}


@dataclass
class TcasRaBroadcast:
    """TC=28 sub-type 2 carries a snapshot of the aircraft's active RA."""
    active_ra: int           # 14-bit ARA field
    rac_record: int          # 4-bit RAC
    ra_terminated: bool
    multiple_threat: bool
    threat_id_type: int      # 0 none, 1 ICAO, 2 altitude/range/bearing
    threat_icao: str | None
    summary: str             # human-readable RA description


def tcas_ra_summary(ara: int, mte: bool) -> str:
    """Translate the 14-bit ARA into a pilot-readable phrase."""
    # Bit numbering in ARA (MSB first within the 14-bit field):
    # b0=msg41, b1=msg42 ... b13=msg54.  msg41 here is the LSB-leftmost.
    # Following Sun ch.14 §1.4.2: when ARA bit 41 = 1 and MTE = 0/1.
    bit41 = (ara >> 13) & 1  # MSB of the 14-bit ARA
    if bit41 == 1:
        # Single-threat RA encoding.
        corrective = (ara >> 12) & 1     # bit 42
        downward = (ara >> 11) & 1       # bit 43
        increased = (ara >> 10) & 1      # bit 44
        reversal = (ara >> 9) & 1        # bit 45
        crossing = (ara >> 8) & 1        # bit 46
        positive = (ara >> 7) & 1        # bit 47
        sense = "DESCEND" if downward else "CLIMB"
        parts = []
        if positive:
            parts.append(sense)
            if increased:
                parts.append("INCREASE")
            if crossing:
                parts.append("CROSSING")
            if reversal:
                parts.append("REVERSAL")
        else:
            # Vertical Speed Limit (preventive)
            parts.append(f"LIMIT {sense.lower()} VS")
        if not corrective:
            parts.append("(preventive)")
        return " ".join(parts)
    elif mte:
        # Multi-threat encoding (bit 41 = 0, MTE = 1).
        upward = (ara >> 12) & 1
        climb = (ara >> 11) & 1
        downward = (ara >> 10) & 1
        descend = (ara >> 9) & 1
        crossing = (ara >> 8) & 1
        reversal = (ara >> 7) & 1
        verbs = []
        if climb:
            verbs.append("CLIMB")
        if descend:
            verbs.append("DESCEND")
        if upward and not climb:
            verbs.append("don't descend")
        if downward and not descend:
            verbs.append("don't climb")
        s = ", ".join(verbs) if verbs else "MULTI-THREAT MAINTAIN"
        if crossing:
            s += " (crossing)"
        if reversal:
            s += " (reversal)"
        return s
    return "no vertical RA"


def tcas_ra_broadcast(msg: str) -> TcasRaBroadcast | None:
    """Decode TC=28 sub-type 2 ADS-B aircraft status RA broadcast."""
    if typecode(msg) != 28:
        return None
    subtype = get_bits(msg, 38, 3)
    if subtype != 2:
        return None  # subtype 1 is emergency/priority, handled separately
    # Layout (ME bits 9-56, msg bits 41-88):
    #   ARA: 14 bits  (msg 41-54)
    #   RAC: 4 bits   (msg 55-58)
    #   RAT: 1 bit    (msg 59)
    #   MTE: 1 bit    (msg 60)
    #   TTI: 2 bits   (msg 61-62)  - threat type indicator
    #   THC: 26 bits  (msg 63-88)  - threat identity (ICAO if TTI=1)
    ara = get_bits(msg, 41, 14)
    rac = get_bits(msg, 55, 4)
    rat = bool(get_bit(msg, 59))
    mte = bool(get_bit(msg, 60))
    tti = get_bits(msg, 61, 2)
    threat_icao = None
    if tti == 1:
        threat_icao = f"{get_bits(msg, 63, 24):06X}"
    return TcasRaBroadcast(
        active_ra=ara,
        rac_record=rac,
        ra_terminated=rat,
        multiple_threat=mte,
        threat_id_type=tti,
        threat_icao=threat_icao,
        summary=tcas_ra_summary(ara, mte),
    )


def emergency_state(msg: str) -> str | None:
    """TC=28 sub-type 1: emergency / priority status."""
    if typecode(msg) != 28 or get_bits(msg, 38, 3) != 1:
        return None
    state = get_bits(msg, 41, 3)
    return EMERGENCY_STATES.get(state)


# ---------------------------------------------------------------------------
# TC 29: Target state and status (autopilot intent, ADS-B v2)
# ---------------------------------------------------------------------------

@dataclass
class TargetState:
    selected_alt_ft: int | None
    alt_source: str | None       # "MCP/FCU" or "FMS"
    qnh_mb: float | None
    selected_heading_deg: float | None
    autopilot: bool
    vnav: bool
    alt_hold: bool
    approach: bool
    tcas_operational: bool


def target_state(msg: str) -> TargetState | None:
    """Decode TC=29 target state and status (ADS-B v2 only)."""
    if typecode(msg) != 29:
        return None
    subtype = get_bits(msg, 38, 2)
    if subtype != 1:
        return None  # ADS-B v1 used a different layout
    # ME bits 9-10 = subtype, then sil_supp(1), alt_type(1),
    # selected_alt(11) [25 ft LSB, range 0-100k], baro(9), hdg_status(1),
    # selected_hdg(9), nacp(4), nicb(1), sil(2), mode_valid(1),
    # autopilot(1), vnav(1), alt_hold(1), approach(1), tcas(1), lnav(1)
    alt_type = get_bit(msg, 41)
    sel_alt_raw = get_bits(msg, 42, 11)
    sel_alt = (sel_alt_raw - 1) * 32 if sel_alt_raw else None
    baro_raw = get_bits(msg, 53, 9)
    qnh = 800.0 + (baro_raw - 1) * 0.8 if baro_raw else None
    hdg_status = get_bit(msg, 62)
    sel_hdg_sign = get_bit(msg, 63)
    sel_hdg_raw = get_bits(msg, 64, 8)
    if hdg_status:
        sel_hdg = sel_hdg_raw * 180.0 / 256
        if sel_hdg_sign:
            sel_hdg += 180
    else:
        sel_hdg = None
    mode_valid = get_bit(msg, 80)
    autopilot = bool(get_bit(msg, 81)) if mode_valid else False
    vnav = bool(get_bit(msg, 82)) if mode_valid else False
    alt_hold = bool(get_bit(msg, 83)) if mode_valid else False
    approach = bool(get_bit(msg, 85)) if mode_valid else False
    tcas_op = bool(get_bit(msg, 86)) if mode_valid else False
    return TargetState(
        selected_alt_ft=sel_alt,
        alt_source="FMS" if alt_type else "MCP/FCU",
        qnh_mb=qnh,
        selected_heading_deg=sel_hdg,
        autopilot=autopilot,
        vnav=vnav,
        alt_hold=alt_hold,
        approach=approach,
        tcas_operational=tcas_op,
    )


# ---------------------------------------------------------------------------
# TC 31: Operational status (ADS-B version, NIC/NAC/SIL)
# ---------------------------------------------------------------------------

@dataclass
class OperationalStatus:
    version: int
    nic_supplement_a: int
    nac_p: int
    nac_v: int
    sil: int
    on_ground: bool | None


def operational_status(msg: str) -> OperationalStatus | None:
    if typecode(msg) != 31:
        return None
    subtype = get_bits(msg, 38, 3)
    on_ground = subtype == 1
    # Version bits 41-43 of ME = msg bits 73-75
    version = get_bits(msg, 73, 3)
    nic_a = get_bit(msg, 76)
    nac_p = get_bits(msg, 77, 4)
    sil = get_bits(msg, 82, 2)
    nac_v = get_bits(msg, 80, 3) if subtype == 0 else 0
    return OperationalStatus(
        version=version,
        nic_supplement_a=nic_a,
        nac_p=nac_p,
        nac_v=nac_v,
        sil=sil,
        on_ground=on_ground if subtype in (0, 1) else None,
    )
