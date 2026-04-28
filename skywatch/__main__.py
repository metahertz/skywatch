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


def _resolve_receivers(args) -> list[dict]:
    """Pair --beast endpoints with their --name / --rx-lat / --rx-lon /
    --rx-range-nm flags by occurrence index.

    Indexing rule: the i-th --beast pairs with the i-th --name, i-th
    --rx-lat, etc.  argparse `action="append"` does not preserve the
    positional relationship between flags, so users running multiple
    receivers MUST pass each --rx-* flag in the same per-receiver order
    as their --beast flags.  E.g.:

        --beast home --beast office \\
        --name home --name office \\
        --rx-lat 51.47 --rx-lat 53.50 \\
        --rx-lon -0.46 --rx-lon -2.45

    For the single-receiver case, the paired flags are optional and the
    legacy global --lat / --lon / --max-range-nm are used as fallback.
    receiver_id is --name when supplied; otherwise host:port.
    """
    beasts = args.beast or []
    names = args.name or []
    lats = args.rx_lat or []
    lons = args.rx_lon or []
    ranges = args.rx_range_nm or []

    # Sanity warning when paired-flag counts don't match --beast count
    # for a multi-receiver setup.  Single-receiver setups intentionally
    # tolerate missing paired flags via the back-compat fallback.
    n = len(beasts)
    if n > 1:
        for label, lst in [("--name", names), ("--rx-lat", lats),
                           ("--rx-lon", lons), ("--rx-range-nm", ranges)]:
            if 0 < len(lst) < n:
                logging.getLogger("skywatch").warning(
                    "%s given %d times but --beast given %d times; "
                    "trailing receivers will use defaults",
                    label, len(lst), n,
                )

    out: list[dict] = []
    for i, endpoint in enumerate(beasts):
        host, port = _parse_endpoint(endpoint, 30005)
        name = names[i] if i < len(names) else None
        lat = lats[i] if i < len(lats) else None
        lon = lons[i] if i < len(lons) else None
        rng = ranges[i] if i < len(ranges) else None
        # Backward-compat: with a single --beast, fall back to the
        # legacy global --lat / --lon / --max-range-nm.
        if n == 1:
            if lat is None: lat = args.lat
            if lon is None: lon = args.lon
            if rng is None: rng = args.max_range_nm
        receiver_id = name or f"{host}:{port}"
        out.append({
            "receiver_id": receiver_id,
            "name": name or receiver_id,
            "host": host, "port": port,
            "lat": lat, "lon": lon,
            "max_range_nm": rng if rng is not None else 280.0,
        })
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="skywatch",
        description="ADS-B / Mode S / TCAS surveillance visualiser",
    )
    parser.add_argument(
        "--beast", metavar="HOST:PORT", action="append", default=None,
        help="Connect to a dump1090 BEAST source (default port 30005). "
             "Repeatable: pass --beast multiple times to ingest from "
             "several receivers simultaneously.  Each --beast may be "
             "followed by a matching --name / --rx-lat / --rx-lon / "
             "--rx-range-nm to label and place that receiver. "
             "If not supplied, uses the synthetic message generator.",
    )
    parser.add_argument(
        "--name", action="append", default=None,
        help="Receiver display name, paired positionally with --beast. "
             "Repeatable.",
    )
    parser.add_argument(
        "--rx-lat", type=float, action="append", default=None,
        help="Receiver latitude, paired positionally with --beast. "
             "Repeatable.",
    )
    parser.add_argument(
        "--rx-lon", type=float, action="append", default=None,
        help="Receiver longitude, paired positionally with --beast. "
             "Repeatable.",
    )
    parser.add_argument(
        "--rx-range-nm", type=float, action="append", default=None,
        help="Receiver range in NM, paired positionally with --beast. "
             "Repeatable.",
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
        "--mongo", metavar="URI", default=None,
        help="MongoDB URI to enable persistence.  When set, the receiver "
             "registry, live aircraft state, event log, frame archive "
             "(time-series, TTL'd), and per-receiver health metrics "
             "(time-series, TTL'd) are written.  Requires `pymongo`.",
    )
    parser.add_argument(
        "--mongo-db", metavar="NAME", default="skywatch",
        help="MongoDB database name (default: skywatch).",
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

    # ---- Persistence (optional) ----
    mongo_store = None
    if args.mongo:
        from skywatch.store import HAS_MONGO, MongoStore
        if not HAS_MONGO:
            log.error("--mongo specified but pymongo is not installed; "
                      "install with `pip install skywatch[mongo]` or "
                      "`pip install pymongo`")
            return 2
        mongo_store = MongoStore(args.mongo, db_name=args.mongo_db)
        try:
            mongo_store.start()
        except Exception as e:
            log.error("Failed to connect to MongoDB at %s: %s", args.mongo, e)
            return 2

    # ---- Resolve receivers ----
    # Resolve the per-receiver list now so we can seed the engine with
    # the *primary* (first) receiver's lat/lon — that's the one used by
    # the legacy single-RX snapshot block and the existing UI's range
    # ring.  The remaining receivers are registered after engine
    # construction.
    beast_specs = _resolve_receivers(args)

    primary = beast_specs[0] if beast_specs else None
    primary_lat = primary["lat"] if primary else args.lat
    primary_lon = primary["lon"] if primary else args.lon
    primary_range = primary["max_range_nm"] if primary else args.max_range_nm

    # ---- Engine ----
    engine = StateEngine(
        receiver_lat=primary_lat,
        receiver_lon=primary_lon,
        max_range_nm=primary_range,
        info_lookup=info_lookup,
        store=mongo_store,
    )

    # Seed every receiver in the registry up-front (lat/lon may be None
    # if the user didn't supply them) so plausibility / UI have correct
    # IDs from the first frame.  CLI specs win over any persisted entry
    # — it's the source of truth for this run.
    for spec in beast_specs:
        engine.receivers.upsert(
            spec["receiver_id"],
            name=spec["name"],
            lat=spec["lat"],
            lon=spec["lon"],
            max_range_nm=spec["max_range_nm"],
        )

    # If persistence is enabled, hydrate any persisted state that the
    # CLI didn't override and persist the merged registry back.
    if mongo_store is not None:
        for doc in mongo_store.load_receivers():
            rid = doc.get("_id") or doc.get("id")
            if rid and not engine.receivers.get(rid):
                engine.receivers.upsert(
                    rid,
                    name=doc.get("name"),
                    lat=doc.get("lat"),
                    lon=doc.get("lon"),
                    max_range_nm=doc.get("max_range_nm"),
                )
        for rx in engine.receivers:
            mongo_store.upsert_receiver(rx.to_dict())
        # Hydrate the active aircraft set (≤10min-old entries).
        for doc in mongo_store.load_aircraft_state():
            doc.pop("_id", None)
            # Best-effort hydration — Aircraft is a dataclass with many
            # complex fields (deque trail, set bds_observed, etc.).  We
            # rebuild a minimal Aircraft and let live frames refresh it.
            from skywatch.state.aircraft import Aircraft
            try:
                ac = Aircraft(icao=doc["icao"])
                ac.callsign = doc.get("callsign")
                ac.lat = doc.get("lat"); ac.lon = doc.get("lon")
                ac.last_seen = doc.get("last_seen", ac.last_seen)
                ac.first_seen = doc.get("first_seen", ac.first_seen)
                engine.aircraft[ac.icao] = ac
            except Exception:
                log.debug("skipping unhydratable aircraft doc")

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
    if beast_specs:
        for spec in beast_specs:
            log.info("Starting BEAST client %r → %s:%d (%s)",
                     spec["receiver_id"], spec["host"], spec["port"],
                     spec["name"])
            app.start_beast_client(
                spec["host"], spec["port"],
                receiver_id=spec["receiver_id"],
            )
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
        if mongo_store is not None:
            mongo_store.stop()
        ws.stop()
        http.stop()
        time.sleep(0.3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
