"""Unified aircraft information lookup.

Layers (each falls through if the higher one fails):
  1. Mictronics database (if loaded)        -> registration, type, description
  2. Algorithmic recovery (always available) -> registration for US/CA/DE/UK/JP/KR
  3. ICAO range allocation (always available)-> country only

Each result includes a `source` field naming which layer produced it, so
the UI can show e.g. "N12345 (algorithmic)" vs "N12345 (FAA database)".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .algorithmic import algo_registration
from .icao_ranges import country_for_icao, is_pia
from .operators import lookup_callsign_operator, Operator
from .types import lookup_type, AircraftType

if TYPE_CHECKING:
    from .mictronics import MictronicsDB


@dataclass
class AircraftInfo:
    """All static (non-flight) information we know about an aircraft."""
    icao: str
    registration: str | None = None
    registration_source: str | None = None    # "database" or "algorithmic"
    type_code: str | None = None
    type_info: AircraftType | None = None
    description: str | None = None
    country_code: str | None = None             # ISO 3166-1 alpha-2
    country_name: str | None = None
    is_military: bool = False
    is_pia: bool = False
    is_interesting: bool = False
    is_ladd: bool = False
    operator: Operator | None = None            # only set when callsign known


class InfoLookup:
    """Combines all data sources for fast lookups."""

    def __init__(self, mictronics_db: "MictronicsDB | None" = None):
        self.db = mictronics_db
        # Cache: ICAO -> AircraftInfo (callsign-independent half)
        self._cache: dict[str, AircraftInfo] = {}

    def lookup(self, icao: str, callsign: str | None = None) -> AircraftInfo:
        """Look up everything we know about an aircraft.

        The cache is keyed by ICAO only; the operator field is computed
        per-call from the (changing) callsign.
        """
        icao_up = icao.upper()
        info = self._cache.get(icao_up)
        if info is None:
            info = self._build(icao_up)
            self._cache[icao_up] = info
        # Augment with operator from callsign (always recomputed)
        if callsign:
            op = lookup_callsign_operator(callsign)
            if op:
                # Return a copy so we don't mutate the cached one
                return AircraftInfo(
                    icao=info.icao,
                    registration=info.registration,
                    registration_source=info.registration_source,
                    type_code=info.type_code,
                    type_info=info.type_info,
                    description=info.description,
                    country_code=info.country_code,
                    country_name=info.country_name,
                    is_military=info.is_military,
                    is_pia=info.is_pia,
                    is_interesting=info.is_interesting,
                    is_ladd=info.is_ladd,
                    operator=op,
                )
        return info

    def _build(self, icao: str) -> AircraftInfo:
        info = AircraftInfo(icao=icao)
        # Country always works for any valid ICAO
        c = country_for_icao(icao)
        if c:
            info.country_code, info.country_name = c

        # PIA flag from the address itself
        if is_pia(icao):
            info.is_pia = True

        # Layer 1: Mictronics DB (if available)
        if self.db is not None:
            rec = self.db.get(icao)
            if rec is not None:
                if rec.registration:
                    info.registration = rec.registration
                    info.registration_source = "database"
                if rec.type_code:
                    info.type_code = rec.type_code
                    info.type_info = lookup_type(rec.type_code)
                if rec.description:
                    info.description = rec.description
                info.is_military = rec.military
                info.is_pia = info.is_pia or rec.pia
                info.is_interesting = rec.interesting
                info.is_ladd = rec.ladd

        # Layer 2: Algorithmic recovery (only if DB didn't supply a reg)
        if info.registration is None:
            algo = algo_registration(icao)
            if algo:
                info.registration = algo
                info.registration_source = "algorithmic"

        # If we have a type code but no type_info, look it up
        if info.type_code and info.type_info is None:
            info.type_info = lookup_type(info.type_code)

        return info

    def invalidate(self, icao: str | None = None) -> None:
        """Clear the cache (whole, or just one ICAO)."""
        if icao is None:
            self._cache.clear()
        else:
            self._cache.pop(icao.upper(), None)

    def stats(self) -> dict:
        return {
            "cached_lookups": len(self._cache),
            "database_loaded": self.db.is_loaded if self.db else False,
            "database_size": len(self.db) if self.db else 0,
        }
