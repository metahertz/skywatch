"""Synthetic Mode S / ADS-B message generator.

Lets us demo and test the entire pipeline without needing an RTL-SDR or
network feed. Generates a configurable scenario of flights including
a scheduled TCAS RA event.

Builds binary messages from scratch — every CRC, CPR, BDS encoding
is the inverse of what our decoders parse, so this also serves as a
round-trip integration test.
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field

from skywatch.decoder.beast import encode_beast
from skywatch.decoder.common import crc, hex_to_bin


# ---------------------------------------------------------------------------
# Bit-builder helpers
# ---------------------------------------------------------------------------

def _bits_to_hex(bits: str) -> str:
    """Pad bits to a multiple of 4 and return uppercase hex."""
    if len(bits) % 4:
        bits += "0" * (4 - len(bits) % 4)
    return f"{int(bits, 2):0{len(bits)//4}X}"


def _add_parity(payload_bits: str, icao: str) -> str:
    """Compute the address-overlay parity tail and return the full hex msg."""
    payload_hex = _bits_to_hex(payload_bits)
    full = payload_hex + "000000"  # placeholder parity
    real_crc = crc(full)
    icao_int = int(icao, 16) & 0xFFFFFF
    parity = real_crc ^ icao_int
    return payload_hex + f"{parity:06X}"


def _add_pure_parity(payload_bits: str) -> str:
    """For DF11/17/18: parity tail = pure CRC remainder (giving residual 0)."""
    payload_hex = _bits_to_hex(payload_bits)
    full = payload_hex + "000000"
    real_crc = crc(full)
    return payload_hex + f"{real_crc:06X}"


# ---------------------------------------------------------------------------
# CPR encoder (inverse of decoder)
# ---------------------------------------------------------------------------

NZ = 15


def _nl(lat: float) -> int:
    if lat == 0:
        return 59
    if abs(lat) >= 87:
        return 1 if abs(lat) > 87 else 2
    nz_term = 1 - math.cos(math.pi / (2 * NZ))
    denom = math.cos(math.pi * abs(lat) / 180.0) ** 2
    try:
        return int(math.floor(2 * math.pi / math.acos(1 - nz_term / denom)))
    except (ValueError, ZeroDivisionError):
        return 1


def encode_cpr(lat: float, lon: float, even: bool) -> tuple[int, int]:
    """Encode (lat, lon) into 17-bit CPR ints for an even or odd frame."""
    i = 0 if even else 1
    d_lat = 360.0 / (60 - i)
    yz = math.floor(131072 * ((lat % d_lat) / d_lat) + 0.5)
    rlat = d_lat * (yz / 131072.0 + math.floor(lat / d_lat))
    nl = _nl(rlat)
    d_lon = 360.0 / max(nl - i, 1)
    xz = math.floor(131072 * ((lon % d_lon) / d_lon) + 0.5)
    return int(yz) % (1 << 17), int(xz) % (1 << 17)


# ---------------------------------------------------------------------------
# Message constructors
# ---------------------------------------------------------------------------

# Reverse of _CALLSIGN_CHARS in decoder
_CALLSIGN_LOOKUP = {}
_charset = (
    "#ABCDEFGHIJKLMNOPQRSTUVWXYZ#####"
    " ###############0123456789######"
)
for idx, ch in enumerate(_charset):
    if ch != "#" and ch not in _CALLSIGN_LOOKUP:
        _CALLSIGN_LOOKUP[ch] = idx


def make_identification(icao: str, callsign: str, category: int = 4) -> str:
    """ADS-B TC=4 (heavy aircraft category) identification message.

    category: TC value 1-4; 4 means a regular jet.
    """
    df = format(17, "05b")
    ca = format(5, "03b")  # level 2+ airborne
    icao_b = format(int(icao, 16), "024b")
    tc = format(category, "05b")
    cat = format(0, "03b")  # generic; we handle fine cat via wake table
    cs_padded = (callsign + "        ")[:8]
    cs_bits = "".join(format(_CALLSIGN_LOOKUP.get(c, 32), "06b") for c in cs_padded)
    me = tc + cat + cs_bits  # 5 + 3 + 48 = 56 bits
    payload = df + ca + icao_b + me
    return _add_pure_parity(payload)


def make_airborne_position(
    icao: str, lat: float, lon: float, alt_ft: int, even: bool = True,
) -> str:
    """ADS-B TC=11 airborne position message with barometric altitude."""
    df = format(17, "05b")
    ca = format(5, "03b")
    icao_b = format(int(icao, 16), "024b")
    tc = format(11, "05b")  # arbitrary in 9-18 range
    ss = "00"  # no surveillance condition
    saf = "0"
    # Altitude with Q=1 encoding
    n = (alt_ft + 1000) // 25
    n_bits = format(n, "011b")
    # Insert Q-bit at position 4 from right (bit 8 of the 12-bit field)
    alt_bits = n_bits[:7] + "1" + n_bits[7:]
    t_bit = "0"
    f_bit = "0" if even else "1"
    lat_cpr, lon_cpr = encode_cpr(lat, lon, even=even)
    me = (
        tc + ss + saf + alt_bits + t_bit + f_bit
        + format(lat_cpr, "017b") + format(lon_cpr, "017b")
    )
    payload = df + ca + icao_b + me
    return _add_pure_parity(payload)


def make_velocity(
    icao: str, gs_kt: float, track_deg: float, vrate_fpm: int,
) -> str:
    """ADS-B TC=19 sub-type 1 ground velocity."""
    df = format(17, "05b")
    ca = format(5, "03b")
    icao_b = format(int(icao, 16), "024b")
    tc = format(19, "05b")
    st = format(1, "03b")  # subsonic ground velocity
    ic = "0"
    ifr = "1"
    nuc = format(0, "03b")

    # Decompose ground vector
    rad = math.radians(track_deg)
    vx = gs_kt * math.sin(rad)
    vy = gs_kt * math.cos(rad)
    s_ew = "1" if vx < 0 else "0"
    v_ew = format(min(int(round(abs(vx))) + 1, 1023), "010b")
    s_ns = "1" if vy < 0 else "0"
    v_ns = format(min(int(round(abs(vy))) + 1, 1023), "010b")

    vr_src = "1"  # baro
    s_vr = "1" if vrate_fpm < 0 else "0"
    vr_n = min(abs(vrate_fpm) // 64 + 1, 511)
    vr_bits = format(vr_n, "09b")
    reserved = "00"
    s_dif = "0"
    d_alt = format(0, "07b")  # not available

    me = (
        tc + st + ic + ifr + nuc
        + s_ew + v_ew + s_ns + v_ns
        + vr_src + s_vr + vr_bits + reserved + s_dif + d_alt
    )
    payload = df + ca + icao_b + me
    return _add_pure_parity(payload)


def make_tcas_ra_broadcast(
    icao: str, threat_icao: str, *,
    sense_descend: bool = False,
    increased: bool = False,
    crossing: bool = False,
) -> str:
    """ADS-B TC=28 sub-type 2 TCAS RA broadcast message."""
    df = format(17, "05b")
    ca = format(5, "03b")
    icao_b = format(int(icao, 16), "024b")
    tc = format(28, "05b")
    st = format(2, "03b")
    # ARA: bit41=1 (single threat), corrective=1, sense, increased, no reversal
    ara_bits = (
        "1"        # bit 41: single-threat encoding
        "1"        # bit 42: corrective
        + ("1" if sense_descend else "0")  # bit 43
        + ("1" if increased else "0")      # bit 44
        + "0"      # bit 45: no reversal
        + ("1" if crossing else "0")       # bit 46
        + "1"      # bit 47: positive RA
        + "0000000"   # bits 48-54 reserved
    )
    rac = "0000"
    rat = "0"        # not terminated
    mte = "0"
    tti = "01"       # threat ICAO
    threat_bits = format(int(threat_icao, 16), "024b")
    # threat field is 26 bits; pad with zeros for the 'data available' bits
    threat_field = threat_bits + "00"
    me = tc + st + ara_bits + rac + rat + mte + tti + threat_field
    payload = df + ca + icao_b + me
    return _add_pure_parity(payload)


def make_tcas_ra_terminated(icao: str) -> str:
    """ADS-B TC=28 sub-type 2 with RAT=1 to signal RA cleared."""
    df = format(17, "05b")
    ca = format(5, "03b")
    icao_b = format(int(icao, 16), "024b")
    tc = format(28, "05b")
    st = format(2, "03b")
    ara_bits = "0" * 14  # no longer active
    rac = "0000"
    rat = "1"
    mte = "0"
    tti = "00"
    threat_field = "0" * 26
    me = tc + st + ara_bits + rac + rat + mte + tti + threat_field
    payload = df + ca + icao_b + me
    return _add_pure_parity(payload)


def make_bds60(
    icao: str, heading_deg: float, ias_kt: int, mach: float,
    vrate_baro_fpm: int, vrate_ins_fpm: int, alt_ft: int = 30000,
) -> str:
    """DF20 reply carrying BDS 6,0 (heading and speed report).

    The DF20 carrier wraps the BDS payload around an altitude code (AC),
    so the same altitude as the aircraft's true altitude should be passed
    in to keep state coherent with ADS-B messages.
    """
    df = format(20, "05b")
    fs = format(0, "03b")  # airborne, no alert
    dr = "00000"
    um = "000000"
    # Altitude code (13 bits) — Q=1, 25 ft increments
    n = (alt_ft + 1000) // 25
    n_bits = format(n, "011b")
    # AC field layout: C1 A1 C2 A2 C4 A4 M B1 Q B2 D2 B4 D4
    # Insert Q-bit at position 8, M-bit at position 6 of the 13-bit field.
    ac_bits = n_bits[:6] + "0" + n_bits[6:7] + "1" + n_bits[7:]

    # MB field for BDS 6,0
    s_hdg = "1"
    sign_h = "1" if heading_deg > 180 else "0"
    h_val = (heading_deg if heading_deg <= 180 else heading_deg - 360)
    h_int = int(round(h_val * 512 / 90)) & 0x3FF
    h_bits = format(h_int, "010b")

    s_ias = "1"
    ias_bits = format(min(ias_kt, 1023), "010b")

    s_mach = "1"
    mach_bits = format(min(int(round(mach / 0.004)), 1023), "010b")

    s_vbaro = "1"
    sign_vb = "1" if vrate_baro_fpm < 0 else "0"
    vb_int = min(abs(vrate_baro_fpm) // 32, 511)
    vb_bits = format(vb_int, "09b")

    s_vins = "1"
    sign_vi = "1" if vrate_ins_fpm < 0 else "0"
    vi_int = min(abs(vrate_ins_fpm) // 32, 511)
    vi_bits = format(vi_int, "09b")

    mb = (
        s_hdg + sign_h + h_bits
        + s_ias + ias_bits
        + s_mach + mach_bits
        + s_vbaro + sign_vb + vb_bits
        + s_vins + sign_vi + vi_bits
    )
    assert len(mb) == 56, f"BDS60 MB length {len(mb)}"

    payload = df + fs + dr + um + ac_bits + mb
    return _add_parity(payload, icao)


def make_bds40(
    icao: str, mcp_alt_ft: int, fms_alt_ft: int, qnh_mb: float,
    alt_ft: int = 30000,
) -> str:
    """DF20 reply carrying BDS 4,0 (selected vertical intention)."""
    df = format(20, "05b")
    fs = format(0, "03b")
    dr = "00000"
    um = "000000"
    n = (alt_ft + 1000) // 25
    n_bits = format(n, "011b")
    ac_bits = n_bits[:6] + "0" + n_bits[6:7] + "1" + n_bits[7:]

    s_mcp = "1"
    mcp_bits = format(mcp_alt_ft // 16, "012b")
    s_fms = "1"
    fms_bits = format(fms_alt_ft // 16, "012b")
    s_qnh = "1"
    qnh_bits = format(int(round((qnh_mb - 800) / 0.1)), "012b")
    reserved = "00000000"
    s_mode = "0"
    vnav = alt_hold = approach = "0"
    reserved2 = "00"
    s_src = "0"
    src = "00"
    mb = (
        s_mcp + mcp_bits + s_fms + fms_bits + s_qnh + qnh_bits
        + reserved + s_mode + vnav + alt_hold + approach
        + reserved2 + s_src + src
    )
    assert len(mb) == 56, f"BDS40 MB length {len(mb)}"
    payload = df + fs + dr + um + ac_bits + mb
    return _add_parity(payload, icao)


def make_df11_squitter(icao: str) -> str:
    """DF11 acquisition squitter — minimal carrier of the ICAO address."""
    df = format(11, "05b")
    ca = format(5, "03b")
    icao_b = format(int(icao, 16), "024b")
    payload = df + ca + icao_b  # 32 bits
    return _add_pure_parity(payload)


# ---------------------------------------------------------------------------
# Synthetic flight scenario
# ---------------------------------------------------------------------------

@dataclass
class SyntheticAircraft:
    icao: str
    callsign: str
    lat: float
    lon: float
    alt_ft: int
    track_deg: float
    gs_kt: float
    vrate_fpm: int = 0
    sel_alt_ft: int = 0
    qnh_mb: float = 1013.2
    heading_offset: float = 0  # heading - track (wind correction)

    def step(self, dt: float) -> None:
        # Move along track
        nm_per_deg_lat = 60.0
        d_nm = self.gs_kt * (dt / 3600.0)
        rad = math.radians(self.track_deg)
        self.lat += (d_nm * math.cos(rad)) / nm_per_deg_lat
        cos_lat = max(0.01, math.cos(math.radians(self.lat)))
        self.lon += (d_nm * math.sin(rad)) / (nm_per_deg_lat * cos_lat)
        self.alt_ft += int(self.vrate_fpm * dt / 60)


@dataclass
class Scenario:
    """A scripted scenario producing BEAST bytes over time."""

    aircraft: list[SyntheticAircraft] = field(default_factory=list)
    receiver_lat: float = 51.4775   # Heathrow, near a major TMA
    receiver_lon: float = -0.4614
    duration_s: float = 600.0
    tcas_event: tuple[str, str, float] | None = None  # (icao_a, icao_b, t_seconds)
    _t: float = 0.0

    def step(self, dt: float = 1.0):
        """Advance the simulated time by `dt`; yield (t, hex_msg) tuples."""
        self._t += dt
        for ac in self.aircraft:
            ac.step(dt)

        # Each aircraft squitters at varying rates
        for ac in self.aircraft:
            # ADS-B position frames: emit both an even and an odd frame each
            # step so the CPR pair-decode can succeed on the next tick.  In
            # reality these alternate at ~2 Hz; here we just emit both.
            yield self._t, make_airborne_position(
                ac.icao, ac.lat, ac.lon, ac.alt_ft, even=True,
            )
            yield self._t + 0.5, make_airborne_position(
                ac.icao, ac.lat, ac.lon, ac.alt_ft, even=False,
            )
            # Velocity at 2 Hz too
            yield self._t, make_velocity(ac.icao, ac.gs_kt, ac.track_deg, ac.vrate_fpm)
            # Identification at 0.2 Hz (every 5 s)
            if int(self._t) % 5 == 0:
                yield self._t, make_identification(ac.icao, ac.callsign)
            # DF11 squitter at 1 Hz
            yield self._t, make_df11_squitter(ac.icao)
            # BDS 6,0 (DF20) every 4 s — needs to be heard so add it
            if int(self._t) % 4 == 0:
                hdg = (ac.track_deg + ac.heading_offset) % 360
                yield self._t, make_bds60(
                    ac.icao, hdg, int(ac.gs_kt * 0.95),
                    min(0.85, ac.gs_kt / 600), ac.vrate_fpm, ac.vrate_fpm,
                    alt_ft=ac.alt_ft,
                )
            # BDS 4,0 (DF20) every 8 s
            if int(self._t) % 8 == 0 and ac.sel_alt_ft:
                yield self._t, make_bds40(
                    ac.icao, ac.sel_alt_ft, ac.sel_alt_ft, ac.qnh_mb,
                    alt_ft=ac.alt_ft,
                )

        # Scripted TCAS RA event
        if self.tcas_event:
            icao_a, icao_b, t_event = self.tcas_event
            if abs(self._t - t_event) < dt:
                # Both aircraft start broadcasting an RA, opposite senses
                yield self._t, make_tcas_ra_broadcast(
                    icao_a, icao_b, sense_descend=False,
                )
                yield self._t, make_tcas_ra_broadcast(
                    icao_b, icao_a, sense_descend=True,
                )
            elif t_event < self._t < t_event + 12:
                # Continue broadcasting at 1.25 Hz during the RA
                if int((self._t - t_event) * 1.25) % 1 == 0:
                    yield self._t, make_tcas_ra_broadcast(
                        icao_a, icao_b, sense_descend=False, increased=True,
                    )
                    yield self._t, make_tcas_ra_broadcast(
                        icao_b, icao_a, sense_descend=True, increased=True,
                    )
            elif abs(self._t - (t_event + 12)) < dt:
                yield self._t, make_tcas_ra_terminated(icao_a)
                yield self._t, make_tcas_ra_terminated(icao_b)


def default_scenario() -> Scenario:
    """A representative scene around London with a planned TCAS event."""
    return Scenario(
        aircraft=[
            SyntheticAircraft(
                icao="406B90", callsign="BAW217", lat=51.50, lon=0.10,
                alt_ft=37000, track_deg=270, gs_kt=470, vrate_fpm=0,
                sel_alt_ft=37000, heading_offset=-7,
            ),
            SyntheticAircraft(
                icao="4CA9B5", callsign="EIN98K", lat=51.30, lon=-0.30,
                alt_ft=24000, track_deg=80, gs_kt=410, vrate_fpm=1500,
                sel_alt_ft=33000, heading_offset=5,
            ),
            SyntheticAircraft(
                icao="A1B2C3", callsign="DAL58", lat=51.60, lon=-0.80,
                alt_ft=12000, track_deg=180, gs_kt=320, vrate_fpm=-1000,
                sel_alt_ft=4000, heading_offset=2,
            ),
            SyntheticAircraft(
                icao="3C6750", callsign="DLH4PT", lat=51.10, lon=0.40,
                alt_ft=33000, track_deg=305, gs_kt=445, vrate_fpm=0,
                sel_alt_ft=33000, heading_offset=-3,
            ),
            SyntheticAircraft(
                icao="4844F1", callsign="KLM43H", lat=51.80, lon=-0.20,
                alt_ft=29000, track_deg=130, gs_kt=440, vrate_fpm=-2000,
                sel_alt_ft=20000, heading_offset=10,
            ),
        ],
        # Schedule a TCAS event between BAW217 and the climbing EIN98K at t=45s
        tcas_event=("406B90", "4CA9B5", 45.0),
    )
