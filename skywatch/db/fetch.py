"""One-off offline data fetcher.

Run this *once* on a machine with internet to populate `data/` with the
external aircraft databases.  After that, the rest of skywatch runs
fully offline.

Usage:
    python -m skywatch.db.fetch [--data-dir DIR] [--quiet]
    python -m skywatch.db.fetch --list-sources       # for offline mirroring

What gets downloaded:

  data/aircraft.csv.gz         (~5 MB) — ICAO 24-bit -> registration / type
                               from github.com/wiedehopf/tar1090-db (csv branch),
                               originally maintained by Mictronics.
                               Updated approximately monthly.

  data/operators.json          (~150 KB) — extended airline operator codes
                               from Mictronics/readsb-protobuf (dev branch),
                               at webapp/src/db/operators.json.
                               This is the upstream source that tar1090-db
                               and readsb both pull from.

  data/types.json              (~600 KB) — full ICAO Doc 8643 type catalog
                               from Mictronics/readsb-protobuf (dev branch),
                               at webapp/src/db/types.json.

The tool falls back through several mirror URLs for each file in case the
primary one moves; use `--list-sources` to print all URLs (one filename per
line, indented URLs underneath) for offline mirroring.

If you can't run this, the tool still works using only the embedded
country-range table and the algorithmic registration recovery — you just
won't have type/operator data for non-N-numbered aircraft outside the
embedded core list of ~110 operators and ~150 types.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import urllib.request
from pathlib import Path

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

# Each entry maps an output filename to a list of fallback URLs.  The fetcher
# tries them in order and keeps the first that succeeds.
#
# Sources verified against the canonical update flows used by readsb,
# tar1090, and the docker-readsb-protobuf container (Apr 2026):
#   - aircraft.csv.gz: tar1090-db's csv branch (Mictronics-derived)
#   - operators.json:  Mictronics readsb-protobuf at webapp/src/db/operators.json
#   - types.json:      Mictronics readsb-protobuf at webapp/src/db/types.json
SOURCES: dict[str, list[str]] = {
    "aircraft.csv.gz": [
        "https://github.com/wiedehopf/tar1090-db/raw/csv/aircraft.csv.gz",
    ],
    "operators.json": [
        "https://raw.githubusercontent.com/Mictronics/readsb-protobuf/dev/webapp/src/db/operators.json",
        "https://github.com/Mictronics/readsb-protobuf/raw/dev/webapp/src/db/operators.json",
        "https://raw.githubusercontent.com/Mictronics/readsb/master/webapp/src/db/operators.json",
    ],
    "types.json": [
        "https://raw.githubusercontent.com/Mictronics/readsb-protobuf/dev/webapp/src/db/types.json",
        "https://github.com/Mictronics/readsb-protobuf/raw/dev/webapp/src/db/types.json",
        "https://raw.githubusercontent.com/Mictronics/readsb/master/webapp/src/db/types.json",
    ],
}


log = logging.getLogger("skywatch.db.fetch")


def fetch_one(url: str, dest: Path, quiet: bool = False) -> tuple[bool, str | None]:
    """Download URL to dest atomically.

    Returns (success, error_message). On success, error_message is None.
    """
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "skywatch/0.1 (+https://example/skywatch)"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            tmp.write_bytes(r.read())
        # Atomic rename
        tmp.replace(dest)
        return True, None
    except Exception as e:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        return False, str(e)


def fetch_with_fallbacks(
    filename: str, urls: list[str], dest_dir: Path, quiet: bool = False,
) -> bool:
    """Try each URL in turn; first one that works wins.

    Prints progress for each attempt and reports the source that succeeded.
    """
    dest = dest_dir / filename
    last_err = None
    for i, url in enumerate(urls):
        if not quiet:
            label = "  -> " if i == 0 else "     fallback: "
            print(f"{label}{url}")
            print(f"     downloading to {dest} ...", end="", flush=True)
        ok, err = fetch_one(url, dest, quiet=quiet)
        if ok:
            if not quiet:
                size_kb = dest.stat().st_size // 1024
                print(f" {size_kb} KB OK")
            return True
        last_err = err
        if not quiet:
            print(f" FAILED ({err})")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip())
    parser.add_argument(
        "--data-dir", type=Path, default=DEFAULT_DATA_DIR,
        help=f"Directory for downloaded data files (default: {DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress progress output",
    )
    parser.add_argument(
        "--list-sources", action="store_true",
        help="Print the source URLs and exit (for offline mirroring)",
    )
    args = parser.parse_args()

    if args.list_sources:
        # One filename per line, then each URL indented; first URL is preferred,
        # subsequent are fallbacks.
        for fn, urls in SOURCES.items():
            print(fn)
            for u in urls:
                print(f"\t{u}")
        return 0

    args.data_dir.mkdir(parents=True, exist_ok=True)
    if not args.quiet:
        print(f"Fetching aircraft databases into {args.data_dir}")
        print("This is a one-off operation; the data is then used offline.\n")

    failures = 0
    for filename, urls in SOURCES.items():
        ok = fetch_with_fallbacks(filename, urls, args.data_dir, quiet=args.quiet)
        if not ok:
            failures += 1

    if not args.quiet:
        print()
        if failures == 0:
            print(f"All {len(SOURCES)} files fetched successfully.")
            print("Skywatch is ready to run fully offline.")
        else:
            print(f"WARNING: {failures} of {len(SOURCES)} files failed.")
            print("Skywatch will still run with reduced lookup coverage.")
    return 1 if failures else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
