"""Loader for the Mictronics aircraft database.

This is the same database used by readsb, tar1090, dump1090-fa, and most
of the open ADS-B ecosystem.  Source: https://www.mictronics.de/aircraft-database/

The database is distributed in two main formats:
  1. tar1090-db CSV: `aircraft.csv.gz`, semicolon-separated, ~5 MB compressed
  2. Original JSON: split into 256 sub-files by hex prefix, ~50 MB total

We use format (1) — single file, simpler to ship offline, easy to parse.

Schema (from readsb/README-json.md, --db-file option):
    hex;reg;icaotype;flags;description

Fields:
  hex        24-bit ICAO address (lowercase hex)
  reg        Registration string, e.g. "N12345" or "G-VBOW"
  icaotype   ICAO Doc 8643 type code, e.g. "B738", "A320"
  flags      Bitfield: 1=military, 2=interesting, 4=PIA, 8=LADD
  description  Manufacturer + model, e.g. "BOEING 737-800"

The DB is loaded into a single dict keyed by uppercase hex.

To populate the database, run:
    python -m skywatch.db.fetch
This downloads ~5 MB from github.com/wiedehopf/tar1090-db (csv branch) and
saves it to data/aircraft.csv.gz.  Requires internet for the one-time fetch.
"""
from __future__ import annotations

import csv
import gzip
import io
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

log = logging.getLogger("skywatch.db.mictronics")


# Path conventions: data lives in <repo>/data/aircraft.csv.gz
DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "aircraft.csv.gz"


@dataclass(frozen=True)
class AircraftRecord:
    """One row of the Mictronics database."""
    icao: str            # uppercase hex ICAO address
    registration: str    # tail number / N-number / etc.
    type_code: str       # ICAO Doc 8643 type code (e.g. B738)
    description: str     # human-readable type
    military: bool
    interesting: bool
    pia: bool
    ladd: bool


def _parse_flags(flag_str: str) -> tuple[bool, bool, bool, bool]:
    """Parse the 4-bit flags field into (military, interesting, pia, ladd)."""
    if not flag_str:
        return False, False, False, False
    try:
        # Mictronics encodes flags as a hex digit (e.g. "1" = military)
        f = int(flag_str, 16)
    except ValueError:
        return False, False, False, False
    return bool(f & 1), bool(f & 2), bool(f & 4), bool(f & 8)


def iter_db(path: Path = DEFAULT_DB_PATH) -> Iterator[AircraftRecord]:
    """Stream records from the gzipped Mictronics CSV.

    The file is small enough (~5 MB) that streaming isn't necessary for
    memory, but it's faster than parsing the whole thing if we only need
    a partial scan (e.g. for tests).
    """
    if not path.exists():
        log.warning("Aircraft DB not found at %s — only algorithmic lookups will work", path)
        return

    open_fn = gzip.open if path.suffix == ".gz" else open
    with open_fn(path, "rt", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f, delimiter=";")
        for row in reader:
            if not row or len(row) < 2:
                continue
            # Pad to 5 cols
            row = (row + ["", "", "", ""])[:5]
            hex_, reg, icao_type, flags, descr = row
            hex_ = hex_.strip().upper()
            if len(hex_) != 6:
                continue
            mil, interesting, pia, ladd = _parse_flags(flags)
            yield AircraftRecord(
                icao=hex_,
                registration=reg.strip(),
                type_code=icao_type.strip(),
                description=descr.strip(),
                military=mil,
                interesting=interesting,
                pia=pia,
                ladd=ladd,
            )


class MictronicsDB:
    """In-memory aircraft database with a dict lookup."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else DEFAULT_DB_PATH
        self._records: dict[str, AircraftRecord] = {}
        self._loaded = False

    def load(self) -> int:
        """Load the entire DB into memory. Returns number of records.

        Also looks for sibling `operators.json` and `types.json` in the same
        directory and merges them into the global operator/type tables.
        """
        from .operators import load_operators_json
        from .types import load_types_json

        self._records.clear()
        for rec in iter_db(self.path):
            self._records[rec.icao] = rec
        self._loaded = True
        log.info("Loaded %d aircraft records from %s", len(self._records), self.path)

        # Sibling JSON files (downloaded alongside aircraft.csv.gz by
        # `python -m skywatch.db.fetch`).
        data_dir = self.path.parent
        ops_path = data_dir / "operators.json"
        if ops_path.exists():
            load_operators_json(ops_path)
        types_path = data_dir / "types.json"
        if types_path.exists():
            load_types_json(types_path)

        return len(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, icao: str) -> bool:
        return icao.upper() in self._records

    def get(self, icao: str) -> AircraftRecord | None:
        return self._records.get(icao.upper())

    @property
    def is_loaded(self) -> bool:
        return self._loaded
