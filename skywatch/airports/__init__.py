"""Airport + runway dataset (OurAirports-derived).

Public-domain CSVs from https://ourairports.com/data/.  The fetcher
downloads the canonical files; the seed module produces a tiny bundled
fallback covering ~50 of the world's busiest airports so the UI works
out of the box without an internet round-trip.
"""

from pathlib import Path

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
AIRPORTS_PATH = DEFAULT_DATA_DIR / "airports.csv.gz"
RUNWAYS_PATH = DEFAULT_DATA_DIR / "runways.csv.gz"
AIRPORTS_SEED_PATH = DEFAULT_DATA_DIR / "airports.seed.csv.gz"
RUNWAYS_SEED_PATH = DEFAULT_DATA_DIR / "runways.seed.csv.gz"
