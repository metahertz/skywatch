"""Common Mode S primitives: CRC, bit access, two's complement, ICAO recovery.

References: ICAO Annex 10 Vol IV; Sun (2021) "The 1090 MHz Riddle" ch. 11.
"""
from __future__ import annotations


# Mode S CRC generator polynomial: x^24 + x^23 + x^22 + x^21 + x^20 + x^19
# + x^18 + x^17 + x^16 + x^15 + x^14 + x^13 + x^12 + x^10 + x^3 + 1
# Bit pattern with leading 1 implicit: 0x1FFF409 -> 25 bits.
# Standard form for 24-bit CRC division: 0xFFF409.
CRC_GENERATOR = 0xFFF409


def hex_to_bin(msg: str) -> str:
    """Convert hex Mode S message to binary string, preserving leading zeros."""
    n = len(msg) * 4
    return bin(int(msg, 16))[2:].zfill(n)


def bin_to_int(bits: str) -> int:
    return int(bits, 2)


def hex_to_bytes(msg: str) -> bytes:
    return bytes.fromhex(msg)


def df(msg: str) -> int:
    """Downlink Format (bits 1-5). For DF24, the high two bits are 11."""
    first_byte = int(msg[:2], 16)
    df_val = first_byte >> 3
    # DF >= 24 are all encoded with the top two bits as 11; collapse to 24.
    if df_val >= 24:
        return 24
    return df_val


def msg_length_bits(df_val: int) -> int:
    """Mode S frame length in bits: short = 56, long = 112."""
    return 112 if df_val in (16, 17, 18, 19, 20, 21, 24) else 56


def crc(msg: str) -> int:
    """Compute Mode S CRC remainder over the full message.

    For ADS-B (DF17/18) and DF11 the result should be 0 if the message is
    intact (since the parity bits at the tail are the actual CRC remainder).
    For address-parity messages, the result equals the ICAO XOR'd in.
    """
    nbits = len(msg) * 4
    data = int(msg, 16)
    # Generator with leading 1, shifted to align with the MSB of the data.
    # G = 0xFFF409 with implicit x^24 -> full 25-bit polynomial 0x1FFF409.
    gen = 0x1FFF409
    for i in range(nbits - 24):
        # Test the top remaining data bit; if 1, XOR generator aligned to it.
        top_bit_pos = nbits - 1 - i
        if (data >> top_bit_pos) & 1:
            data ^= gen << (top_bit_pos - 24)
    return data & 0xFFFFFF


def crc_check(msg: str) -> bool:
    """For DF11 (with II=0) and DF17/18, the CRC residual should be 0."""
    return crc(msg) == 0


def icao_from_squitter(msg: str) -> str | None:
    """Extract ICAO from messages where it sits in plaintext (DF11/17/18)."""
    df_val = df(msg)
    if df_val in (11, 17, 18):
        # ICAO is bits 9-32 of the message = hex chars 2:8.
        return msg[2:8].upper()
    return None


def recover_icao(msg: str) -> str:
    """Recover ICAO from address-parity overlaid messages (DF0/4/5/16/20/21).

    The message parity = real_CRC XOR ICAO. So:
        ICAO = received_parity XOR real_CRC
             = crc(full_message) for AP-overlaid messages.

    The result will only equal the actual aircraft ICAO if no bit errors
    occurred. The caller must validate against a roster of known ICAOs.
    """
    return f"{crc(msg):06X}"


def twos_complement(value: int, nbits: int) -> int:
    """Decode an n-bit signed two's-complement integer."""
    if value & (1 << (nbits - 1)):
        return value - (1 << nbits)
    return value


def get_bits(msg: str, start: int, length: int) -> int:
    """Extract a bitfield from a hex Mode S message.

    Bits are numbered from 1 (MSB of the first byte), per ICAO convention.
    """
    bits = hex_to_bin(msg)
    return int(bits[start - 1 : start - 1 + length], 2)


def get_bit(msg: str, pos: int) -> int:
    """Get a single bit from a hex Mode S message (1-indexed)."""
    return get_bits(msg, pos, 1)
