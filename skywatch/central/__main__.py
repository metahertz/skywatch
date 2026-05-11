"""skywatch.central — UI server + cross-receiver merger.

Run one per deployment.  Subscribes to the chosen transport, merges
incoming deltas across receivers via the existing engine data
structures, and serves the same HTTP + WebSocket UI as monolithic
mode.

Examples:

    # Mongo transport.  Same Mongo URI is used for persistence and
    # the change-stream feed.
    python -m skywatch.central \\
        --transport mongo --mongo mongodb://localhost:27017 \\
        --http 0.0.0.0:8080 --ws 0.0.0.0:8765

    # WebSocket transport.  Mongo is still allowed as a separate
    # persistence layer.
    SKYWATCH_INGEST_TOKEN=... python -m skywatch.central \\
        --transport ws --ingest-bind 0.0.0.0:8767 \\
        --token-env SKYWATCH_INGEST_TOKEN \\
        --mongo mongodb://localhost:27017 \\
        --http 0.0.0.0:8080 --ws 0.0.0.0:8765
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from skywatch.central.merger import CentralMerger
from skywatch.db import InfoLookup, MictronicsDB
from skywatch.db.mictronics import DEFAULT_DB_PATH
from skywatch.db.seed import SEED_PATH
from skywatch.enrich import RouteResolver
from skywatch.server import AppServer, StaticServer, WebSocketServer
from skywatch.state import StateEngine
from skywatch.transport import make_transport


def _parse_endpoint(endpoint: str, default_port: int) -> tuple[str, int]:
    if ":" in endpoint:
        host, _, port = endpoint.rpartition(":")
        return host, int(port)
    return endpoint, default_port


def main(argv=None) -> int:
    # Tiny subcommand dispatch: keep the flat-flag CLI for the run
    # path, peel off operator helpers as positional commands.  argv
    # defaults to sys.argv[1:] so the same sniff works whether
    # invoked from the CLI or programmatically (tests).
    effective_argv = argv if argv is not None else sys.argv[1:]
    if effective_argv and effective_argv[0] == "gen-token":
        import secrets
        # 32 bytes → ~43 url-safe chars, 256 bits of entropy.  Paste
        # the output into SKYWATCH_INGEST_TOKEN on the central host
        # and every edge.
        print(secrets.token_urlsafe(32))
        return 0

    parser = argparse.ArgumentParser(
        prog="skywatch.central",
        description="Central renderer: consume deltas, merge, serve UI.",
    )
    parser.add_argument(
        "--transport", choices=("mongo", "ws"), required=True,
    )
    # Mongo
    parser.add_argument("--mongo", metavar="URI", default=None,
                        help="MongoDB URI.  Required for --transport mongo. "
                             "Optional but recommended for --transport ws.")
    parser.add_argument("--mongo-db", default="skywatch")
    # WS-side ingest server
    parser.add_argument("--ingest-bind", default="0.0.0.0:8767",
                        help="host:port to listen for edge connections "
                             "(--transport ws only).")
    parser.add_argument("--ingest-path", default="/ingest")
    parser.add_argument("--token-env", default=None,
                        help="Env-var holding bearer token (--transport ws).")
    parser.add_argument("--token", default=None)
    # UI server
    parser.add_argument("--http", default="127.0.0.1:8080")
    parser.add_argument("--ws", default="127.0.0.1:8765")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--route-enrichment", action="store_true",
        help="Enable callsign → origin/destination lookups via adsbdb.com.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)-22s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("skywatch.central")

    # Resolve aircraft DB
    db_path = args.db
    if db_path is None and DEFAULT_DB_PATH.exists():
        db_path = DEFAULT_DB_PATH
    elif db_path is None and SEED_PATH.exists():
        db_path = SEED_PATH
    info_lookup = None
    if db_path:
        db = MictronicsDB(db_path)
        db.load()
        info_lookup = InfoLookup(mictronics_db=db)
        log.info("Loaded aircraft DB: %d records from %s", len(db), db_path)

    # Persistence (optional in WS mode, required for mongo transport)
    mongo_store = None
    if args.mongo:
        from skywatch.store import HAS_MONGO, MongoStore
        if not HAS_MONGO:
            log.error("pymongo not installed; pip install pymongo")
            return 2
        mongo_store = MongoStore(args.mongo, db_name=args.mongo_db)
        try:
            mongo_store.start()
        except Exception as e:
            log.error("MongoStore start failed: %s", e)
            return 2

    # Build the transport.
    if args.transport == "mongo":
        if not args.mongo:
            log.error("--transport mongo requires --mongo")
            return 2
        transport = make_transport("mongo", uri=args.mongo,
                                   db_name=args.mongo_db)
    else:
        transport = make_transport(
            "ws", bind=args.ingest_bind, path=args.ingest_path,
            token=args.token, token_env=args.token_env,
        )
    try:
        transport.start()
    except Exception as e:
        log.error("transport start failed: %s", e)
        return 2

    # Engine — empty, no input source on central.
    engine = StateEngine(info_lookup=info_lookup, store=mongo_store)
    # Route enrichment lives at central too.
    route_resolver = RouteResolver(
        on_route=engine.apply_route,
        enabled=args.route_enrichment,
    )
    engine.route_resolver = route_resolver
    route_resolver.start()

    # UI servers (same as monolithic).
    ws_host, ws_port = _parse_endpoint(args.ws, 8765)
    http_host, http_port = _parse_endpoint(args.http, 8080)
    ws = WebSocketServer(host=ws_host, port=ws_port)
    web_dir = Path(__file__).resolve().parent.parent.parent / "web"
    http = StaticServer(directory=web_dir, host=http_host, port=http_port)
    app = AppServer(engine=engine, ws_server=ws)
    app.attach()
    ws.start()
    http.start()
    app.start()

    # Wire transport → merger → engine listeners.
    merger = CentralMerger(engine)
    transport.subscribe(merger.apply_delta)

    print()
    print(f"  Web UI:    http://{http_host}:{http_port}/")
    print(f"  WebSocket: ws://{ws_host}:{ws_port}/")
    print(f"  Transport: {args.transport}")
    print()
    print("  Press Ctrl-C to stop.")
    print()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("shutting down")
        app.stop()
        route_resolver.stop()
        transport.stop()
        if mongo_store is not None:
            mongo_store.stop()
        ws.stop()
        http.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
