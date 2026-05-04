"""Download the OurAirports airport + runway dataset.

Run once on a machine with internet to populate `data/` with the
public-domain airport/runway CSVs.  After that, skywatch runs fully
offline against the bundled data.

Usage:
    python -m skywatch.airports.fetch [--data-dir DIR] [--quiet]
    python -m skywatch.airports.fetch --list-sources  # for offline mirroring

The dataset is licensed CC0 (public domain) by OurAirports.
"""
from __future__ import annotations

import argparse
import gzip
import logging
import sys
import urllib.request
from pathlib import Path

from . import DEFAULT_DATA_DIR


# Mirrors are checked in order; first success wins.
SOURCES: dict[str, list[str]] = {
    "airports.csv.gz": [
        # Mirrored on davidmegginson's GitHub Pages site (canonical
        # public mirror, refreshed daily from the OurAirports CMS).
        "https://davidmegginson.github.io/ourairports-data/airports.csv",
    ],
    "runways.csv.gz": [
        "https://davidmegginson.github.io/ourairports-data/runways.csv",
    ],
}


log = logging.getLogger("skywatch.airports.fetch")


def fetch_one(url: str, dest: Path, quiet: bool = False) -> tuple[bool, str | None]:
    """Download URL to dest atomically.  CSV bodies are re-gzipped on
    write so the on-disk file ends in .csv.gz like the rest of the
    project's data.  Returns (success, error_message)."""
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "skywatch-airports-fetch/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
        if not body:
            return False, "empty body"
        # Some mirrors serve raw .csv even when the source URL ends in
        # .csv.gz (or vice versa).  Normalise: gzip-encode whatever we
        # got if our destination is .gz.
        if dest.suffix == ".gz" and body[:2] != b"\x1f\x8b":
            body = gzip.compress(body)
        tmp.write_bytes(body)
        tmp.replace(dest)
        if not quiet:
            log.info("fetched %s (%d bytes)", dest.name, dest.stat().st_size)
        return True, None
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False, str(e)


def fetch_all(data_dir: Path, quiet: bool = False) -> int:
    data_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for fname, urls in SOURCES.items():
        dest = data_dir / fname
        ok = False
        last_err = None
        for url in urls:
            ok, err = fetch_one(url, dest, quiet=quiet)
            if ok:
                break
            last_err = err
            if not quiet:
                log.info("  %s failed: %s — trying next mirror", url, err)
        if not ok:
            failures += 1
            log.warning("FAILED %s (last error: %s)", fname, last_err)
    return failures


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="skywatch.airports.fetch")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--list-sources", action="store_true",
        help="Print all candidate URLs and exit (for offline mirroring).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(message)s",
    )

    if args.list_sources:
        for fname, urls in SOURCES.items():
            print(fname)
            for url in urls:
                print(f"  {url}")
        return 0

    failures = fetch_all(args.data_dir, quiet=args.quiet)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
