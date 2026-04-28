"""BEAST binary protocol parser.

dump1090 emits messages on TCP port 30005 in BEAST format:

    <esc> <type> <ts:6> <signal:1> <data:N>

  - esc = 0x1A
  - type 0x31 = Mode A/C (8 byte message, 2-byte data)
  - type 0x32 = Mode S short (56-bit, 7-byte data)
  - type 0x33 = Mode S long  (112-bit, 14-byte data)
  - ts: big-endian 48-bit 12 MHz counter (we treat as wall-clock-relative)
  - signal: 1 byte 0-255, mapped to dBFS via 10*log10(s^2/65025)
  - data: raw Mode S frame bytes

Any 0x1A that appears INSIDE the timestamp/signal/data is doubled (escaped
to 0x1A 0x1A) to disambiguate from a frame start.

References:
    https://wiki.jetvision.de/wiki/Mode-S_Beast:Data_Output_Formats
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass


ESC = 0x1A


@dataclass
class BeastFrame:
    raw_hex: str
    df: int
    timestamp: float       # seconds, monotonic-ish
    rssi_dbfs: float       # negative number, 0 = full scale
    msg_type: int          # 0x31/0x32/0x33
    # Receiver this frame arrived from.  Stable string ID assigned by the
    # caller (parser is constructed with one).  When skywatch is ingesting
    # from a single source this is just a constant; with multiple BEAST
    # feeds, the engine uses it to attribute every per-aircraft signal
    # (RSSI / msg_counts / first/last_seen) and to constrain CPR pairing
    # to same-receiver halves only.
    receiver_id: str = "default"


# Default tag when nothing else is known about the source.  Callers that
# care about receiver attribution (multi-receiver mode) should always
# pass an explicit receiver_id.
DEFAULT_RECEIVER_ID = "default"


class BeastParser:
    """Stateful parser that consumes bytes and yields BeastFrame objects."""

    def __init__(self, receiver_id: str = DEFAULT_RECEIVER_ID) -> None:
        self._buf = bytearray()
        self._receiver_id = receiver_id

    def feed(self, data: bytes) -> list[BeastFrame]:
        """Append raw bytes, return any complete frames found."""
        self._buf.extend(data)
        return list(self._extract())

    def _extract(self):
        # Walk the buffer looking for ESC followed by a known type byte.
        i = 0
        while i < len(self._buf):
            if self._buf[i] != ESC:
                i += 1
                continue
            # Need at least 2 bytes for ESC+type.
            if i + 1 >= len(self._buf):
                break
            t = self._buf[i + 1]
            if t == ESC:
                # Escaped ESC inside payload; not a frame start. Skip both.
                i += 2
                continue
            if t not in (0x31, 0x32, 0x33):
                # Unknown type — treat the lone ESC as garbage and advance.
                i += 1
                continue
            data_len = {0x31: 2, 0x32: 7, 0x33: 14}[t]
            # We need to read 6 (ts) + 1 (sig) + data_len bytes, but each
            # 0x1A inside is doubled, so read with un-escaping.
            payload, consumed = self._read_payload(i + 2, 6 + 1 + data_len)
            if payload is None:
                # Incomplete frame; wait for more data.
                break
            ts_raw = int.from_bytes(payload[0:6], "big")
            sig = payload[6]
            data = bytes(payload[7 : 7 + data_len])
            # 12 MHz counter -> seconds. Caller can offset to wall clock.
            timestamp = ts_raw / 12_000_000.0
            # Signal byte to dBFS; dump1090-fa convention.
            if sig > 0:
                rssi = 10 * math.log10((sig * sig) / 65025) + 0  # 0 = 0 dBFS
            else:
                rssi = -100.0
            raw_hex = data.hex().upper()
            yield BeastFrame(
                raw_hex=raw_hex,
                df=(data[0] >> 3),
                timestamp=timestamp,
                rssi_dbfs=rssi,
                msg_type=t,
                receiver_id=self._receiver_id,
            )
            del self._buf[: i + 2 + consumed]
            i = 0
        else:
            return
        # Trim consumed bytes from front.
        if i > 0:
            del self._buf[:i]

    def _read_payload(self, start: int, n: int):
        """Read `n` logical bytes from buffer starting at `start`, un-escaping
        any ESC ESC pairs. Returns (bytes, consumed_from_buffer) or (None, 0)
        if incomplete."""
        out = bytearray()
        pos = start
        consumed = 0
        while len(out) < n:
            if pos >= len(self._buf):
                return None, 0
            b = self._buf[pos]
            if b == ESC:
                # Need a following byte to know if it's escape or framestart.
                if pos + 1 >= len(self._buf):
                    return None, 0
                if self._buf[pos + 1] == ESC:
                    out.append(ESC)
                    pos += 2
                    consumed += 2
                else:
                    # An ESC followed by a non-ESC mid-payload means the
                    # current "frame" was actually garbage — abort.
                    return None, -1
            else:
                out.append(b)
                pos += 1
                consumed += 1
        return out, consumed


def encode_beast(raw_hex: str, ts_seconds: float = 0.0,
                 signal: int = 200) -> bytes:
    """Encode a single Mode S frame as BEAST bytes (for tests / replay)."""
    data = bytes.fromhex(raw_hex)
    if len(data) == 7:
        t = 0x32
    elif len(data) == 14:
        t = 0x33
    elif len(data) == 2:
        t = 0x31
    else:
        raise ValueError(f"Unsupported frame length: {len(data)}")
    ts_counter = int(ts_seconds * 12_000_000) & 0xFFFFFFFFFFFF
    ts_bytes = ts_counter.to_bytes(6, "big")
    payload = bytes([t]) + ts_bytes + bytes([signal]) + data
    # Escape any ESC bytes inside payload (but NOT the leading ESC).
    escaped = bytearray()
    for b in payload:
        escaped.append(b)
        if b == ESC:
            escaped.append(ESC)
    return bytes([ESC]) + bytes(escaped)


# ---------------------------------------------------------------------------
# AVR (raw text) format — dump1090's `*HEX;` lines on port 30002.
# Useful as a fallback for hardware/streams that don't emit BEAST.
# ---------------------------------------------------------------------------

def parse_avr_line(line: str) -> str | None:
    """Parse a single AVR line `*HEX;` and return the hex payload."""
    line = line.strip()
    if not line.startswith("*") or not line.endswith(";"):
        return None
    hex_part = line[1:-1]
    if not all(c in "0123456789ABCDEFabcdef" for c in hex_part):
        return None
    if len(hex_part) not in (14, 28):
        return None  # short or long Mode S only
    return hex_part.upper()
