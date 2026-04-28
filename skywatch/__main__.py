"""skywatch CLI entry point.

Run with:
    python -m skywatch                          # synthetic feed (no SDR needed)
    python -m skywatch --beast HOST:PORT        # connect to dump1090 BEAST
    python -m skywatch --beast localhost:30005  # typical local dump1090
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from skywatch.db import InfoLookup, MictronicsDB
from skywatch.db.mictronics import DEFAULT_DB_PATH
from skywatch.db.seed import SEED_PATH
from skywatch.enrich import RouteResolver
from skywatch.server import AppServer, StaticServer, WebSocketServer
from skywatch.state import StateEngine


def _parse_endpoint(endpoint: str, default_port: int) -> tuple[str, int]:
    """Parse 'host:port' or just 'host' string."""
    if ":" in endpoint:
        host, _, port = endpoint.rpartition(":")
        return host, int(port)
    return endpoint, default_port


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="skywatch",
        description="ADS-B / Mode S / TCAS surveillance visualiser",
    )
    parser.add_argument(
        "--beast", metavar="HOST:PORT", default=None,
        help="Connect to a dump1090 BEAST source (default port 30005). "
             "If not supplied, uses the synthetic message generator.",
    )
    parser.add_argument(
        "--http", metavar="HOST:PORT", default="127.0.0.1:8080",
        help="HTTP bind for the web UI (default: 127.0.0.1:8080)",
    )
    parser.add_argument(
        "--ws", metavar="HOST:PORT", default="127.0.0.1:8765",
        help="WebSocket bind for live state (default: 127.0.0.1:8765)",
    )
    parser.add_argument(
        "--lat", type=float, default=None,
        help="Receiver latitude (decimal degrees) for range/plausibility checks",
    )
    parser.add_argument(
        "--lon", type=float, default=None,
        help="Receiver longitude (decimal degrees)",
    )
    parser.add_argument(
        "--max-range-nm", type=float, default=280.0,
        help="Maximum receiver range in nautical miles (default: 280)",
    )
    parser.add_argument(
        "--db", type=Path, default=None,
        help="Path to Mictronics aircraft.csv.gz "
             "(default: data/aircraft.csv.gz, or seed DB)",
    )
    parser.add_argument(
        "--time-scale", type=float, default=1.0,
        help="Synthetic feed time scale (>1 = faster than real time)",
    )
    parser.add_argument(
        "--route-enrichment", action="store_true",
        help="Enable callsign → origin/destination lookups via the public "
             "adsbdb.com API at startup.  Each lookup leaks the callsign "
             "(and your IP) to the third party, so it is off by default. "
             "Can also be toggled at runtime from the web UI.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="DEBUG logging",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)-22s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("skywatch")

    # ---- Resolve aircraft DB ----
    db_path: Path | None
    if args.db:
        db_path = args.db
    elif DEFAULT_DB_PATH.exists():
        db_path = DEFAULT_DB_PATH
    elif SEED_PATH.exists():
        db_path = SEED_PATH
        log.info("Using seed aircraft DB; run `python -m skywatch.db.fetch` "
                 "for the full Mictronics database.")
    else:
        db_path = None
        log.warning("No aircraft DB found; only algorithmic registration "
                    "recovery available. Run `python -m skywatch.db.seed` "
                    "or `python -m skywatch.db.fetch`.")

    info_lookup = None
    if db_path:
        db = MictronicsDB(db_path)
        db.load()
        info_lookup = InfoLookup(mictronics_db=db)
        log.info("Loaded aircraft DB: %d records from %s", len(db), db_path)

    # ---- Engine ----
    engine = StateEngine(
        receiver_lat=args.lat,
        receiver_lon=args.lon,
        max_range_nm=args.max_range_nm,
        info_lookup=info_lookup,
    )

    # ---- Route enrichment (adsbdb.com) ----
    route_resolver = RouteResolver(
        on_route=engine.apply_route,
        enabled=args.route_enrichment,
    )
    engine.route_resolver = route_resolver
    route_resolver.start()

    # ---- Servers ----
    ws_host, ws_port = _parse_endpoint(args.ws, 8765)
    http_host, http_port = _parse_endpoint(args.http, 8080)

    ws = WebSocketServer(host=ws_host, port=ws_port)
    web_dir = Path(__file__).resolve().parent.parent / "web"
    http = StaticServer(directory=web_dir, host=http_host, port=http_port)

    app = AppServer(engine=engine, ws_server=ws)
    app.attach()

    ws.start()
    http.start()
    app.start()

    # ---- Input source ----
    if args.beast:
        beast_host, beast_port = _parse_endpoint(args.beast, 30005)
        app.start_beast_client(beast_host, beast_port)
    else:
        log.info("No --beast specified — using synthetic message generator")
        app.start_synthetic_input(time_scale=args.time_scale)

    print()
    print(f"  Web UI:    http://{http_host}:{http_port}/")
    print(f"  WebSocket: ws://{ws_host}:{ws_port}/")
    print()
    print("  Press Ctrl-C to stop.")
    print()

    # ---- Wait for SIGINT ----
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
        app.stop()
        route_resolver.stop()
        ws.stop()
        http.stop()
        time.sleep(0.3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
