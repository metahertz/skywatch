"""skywatch.edge — receiver-site edge process.

One instance per dump1090 BEAST source.  Decodes locally and pushes
per-aircraft state deltas to a central node via the configured
transport.

Examples:

    # Mongo transport (durable, ~100-500 ms latency)
    python -m skywatch.edge \\
        --beast localhost:30005 --name home \\
        --rx-lat 51.47 --rx-lon -0.46 \\
        --transport mongo --mongo mongodb://central.lan:27017

    # WebSocket transport (~10-50 ms latency, no durability beyond spool)
    SKYWATCH_INGEST_TOKEN=... python -m skywatch.edge \\
        --beast localhost:30005 --name home \\
        --rx-lat 51.47 --rx-lon -0.46 \\
        --transport ws --central ws://central.lan:8767/ingest \\
        --token-env SKYWATCH_INGEST_TOKEN
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from skywatch.edge.runner import EdgeRunner
from skywatch.transport import make_transport


def _parse_endpoint(endpoint: str, default_port: int) -> tuple[str, int]:
    if ":" in endpoint:
        host, _, port = endpoint.rpartition(":")
        return host, int(port)
    return endpoint, default_port


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="skywatch.edge",
        description="Edge receiver: BEAST → decode → push deltas to central.",
    )
    parser.add_argument(
        "--beast", metavar="HOST:PORT", required=True,
        help="dump1090 BEAST source (default port 30005).",
    )
    parser.add_argument(
        "--vdl2", metavar="HOST:PORT", default=None,
        help="Optional dumpvdl2 JSON source for this site (default port "
             "5555).  When set, VDL2 / CPDLC / ACARS frames decoded "
             "locally are shipped to the central as DELTA_TYPE_COMMS "
             "envelopes alongside the BEAST-derived aircraft updates.",
    )
    parser.add_argument(
        "--name", required=True,
        help="Stable receiver_id; appears in central's by_receiver attribution.",
    )
    parser.add_argument("--rx-lat", type=float, default=None)
    parser.add_argument("--rx-lon", type=float, default=None)
    parser.add_argument("--rx-range-nm", type=float, default=280.0)
    parser.add_argument(
        "--transport", choices=("mongo", "ws"), required=True,
        help="Edge → central transport.  Mutually exclusive with the "
             "other choice; central must be configured the same way.",
    )
    # Mongo transport args
    parser.add_argument("--mongo", metavar="URI", default=None,
                        help="MongoDB URI (transport=mongo).")
    parser.add_argument("--mongo-db", default="skywatch")
    # WS transport args
    parser.add_argument("--central", metavar="URL", default=None,
                        help="ws://host:port/ingest (transport=ws).")
    parser.add_argument("--token-env", metavar="VAR", default=None,
                        help="Env-var holding the bearer token.")
    parser.add_argument("--token", default=None,
                        help="Bearer token literal (avoid; prefer --token-env).")
    # Spool
    parser.add_argument(
        "--spool", type=Path, default=None,
        help="On-disk overflow spool path (sqlite).  Without this, "
             "deltas are dropped when the in-memory queue overflows.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)-22s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("skywatch.edge")

    # Build the transport.
    if args.transport == "mongo":
        if not args.mongo:
            log.error("--transport mongo requires --mongo URI")
            return 2
        try:
            transport = make_transport("mongo", uri=args.mongo,
                                       db_name=args.mongo_db)
        except ImportError:
            log.error("pymongo not installed; pip install pymongo")
            return 2
    else:
        if not args.central:
            log.error("--transport ws requires --central URL")
            return 2
        transport = make_transport(
            "ws", central_url=args.central,
            token=args.token, token_env=args.token_env,
        )
    try:
        transport.start()
    except Exception as e:
        log.error("transport start failed: %s", e)
        return 2

    # Build and start the runner.
    beast_host, beast_port = _parse_endpoint(args.beast, 30005)
    vdl2_host = vdl2_port = None
    if args.vdl2:
        vdl2_host, vdl2_port = _parse_endpoint(args.vdl2, 5555)
    runner = EdgeRunner(
        receiver_id=args.name,
        beast_host=beast_host, beast_port=beast_port,
        vdl2_host=vdl2_host, vdl2_port=vdl2_port,
        transport=transport,
        receiver_lat=args.rx_lat,
        receiver_lon=args.rx_lon,
        max_range_nm=args.rx_range_nm,
        spool_path=args.spool,
    )
    runner.start()

    log.info("edge %s online; transport=%s", args.name, args.transport)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("shutting down")
        runner.stop()
        transport.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
