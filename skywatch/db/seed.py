"""Generate a tiny seed aircraft database in Mictronics CSV format.

This is the fallback dataset the system uses when the real Mictronics DB
hasn't been downloaded yet.  It contains a few entries matching our
synthetic scenario plus some well-known real-world aircraft for demos.

Run as: python -m skywatch.db.seed
"""
from __future__ import annotations

import csv
import gzip
from pathlib import Path

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
SEED_PATH = DEFAULT_DATA_DIR / "aircraft.seed.csv.gz"


# (icao, registration, type_code, flags_hex, description)
SEED_ROWS = [
    # --- Synthetic scenario aircraft (matched to default_scenario()) ---
    ("406B90", "G-EUYG", "A320", "0", "Airbus A320-232"),
    ("4CA9B5", "EI-DLN", "B738", "0", "Boeing 737-8AS"),
    ("A1B2C3", "N358DA", "B752", "0", "Boeing 757-232"),
    ("3C6750", "D-AIPT", "A320", "0", "Airbus A320-211"),
    ("4844F1", "PH-BXA", "B738", "0", "Boeing 737-8K2"),
    # --- Famous textbook ICAOs from Sun's "1090 MHz Riddle" ---
    ("4840D6", "PH-KZD", "F70", "0", "Fokker 70"),
    ("40621D", "G-GATM", "A320", "0", "Airbus A320-232"),
    ("485020", "PH-EZA", "E190", "0", "Embraer 190"),
    ("A05F21", "N101AN", "A321", "0", "Airbus A321-231"),
    ("3C6DD0", "D-AIQB", "A320", "0", "Airbus A320-211"),
    # --- A few well-known special aircraft ---
    ("ADFEF8", "N28000", "B748", "1", "Boeing 747-8I (VC-25B Air Force One)"),
    ("AE2EAB", "N612US", "B752", "1", "Boeing C-32A (USAF executive transport)"),
    # --- Some military examples to demonstrate flag handling ---
    ("AE0001", "70-1234", "C5M", "1", "Lockheed C-5M Super Galaxy"),
    ("AE0002", "00-1234", "C17", "1", "Boeing C-17 Globemaster III"),
    # --- A PIA-range example so the UI can display the flag ---
    ("ADF800", "PIA-ROTATING", "GLF6", "5", "Gulfstream G650 (PIA enrolled)"),
]


def generate(path: Path = SEED_PATH) -> int:
    """Write the seed database to `path`. Returns row count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Mictronics format is semicolon-separated, gzipped CSV
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        for row in SEED_ROWS:
            writer.writerow(row)
    return len(SEED_ROWS)


if __name__ == "__main__":
    import sys
    n = generate()
    print(f"Wrote {n} seed rows to {SEED_PATH}")
    sys.exit(0)
