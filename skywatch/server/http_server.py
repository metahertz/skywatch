"""Simple static file server for the skywatch web UI.

Serves the contents of `web/` over HTTP, plus a small allow-listed set
of files from `data/` under the `/data/` path so the frontend can
fetch the airport / runway dataset at runtime without exposing the
rest of the data directory.

Pure stdlib.
"""
from __future__ import annotations

import http.server
import logging
import os
import socketserver
import threading
from pathlib import Path

log = logging.getLogger("skywatch.http")


# Files under `data/` that the static server is allowed to serve under
# `/data/`.  Keeps the surface narrow — Mictronics CSVs and operator
# JSON stay private (and aren't useful to the UI anyway).
_DATA_ALLOWLIST = {
    "airports.csv.gz",
    "airports.seed.csv.gz",
    "runways.csv.gz",
    "runways.seed.csv.gz",
}


class _SkywatchHandler(http.server.SimpleHTTPRequestHandler):
    """Serves `web/` for normal paths plus an allow-listed subset of
    `data/` under `/data/<file>`."""

    # Set per-instance from the StaticServer.start() factory.
    data_dir: Path = Path()

    def log_message(self, fmt: str, *args) -> None:
        log.debug(fmt, *args)

    def do_GET(self):
        if self.path.startswith("/data/"):
            return self._serve_data()
        return super().do_GET()

    def do_HEAD(self):
        if self.path.startswith("/data/"):
            return self._serve_data(head_only=True)
        return super().do_HEAD()

    def _serve_data(self, head_only: bool = False):
        # Strip query string, then take the path component.  basename()
        # neutralises any "../" attempts.
        rel = self.path.split("?", 1)[0][len("/data/"):]
        name = os.path.basename(rel)
        if name not in _DATA_ALLOWLIST:
            self.send_error(404, "Not allowed")
            return
        full = self.data_dir / name
        if not full.is_file():
            self.send_error(404, f"{name} not generated yet")
            return
        try:
            data = full.read_bytes()
        except OSError as e:
            self.send_error(500, f"read failed: {e}")
            return
        ctype = "application/gzip" if name.endswith(".gz") else "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # Aggressive cache: dataset rotates only when the operator runs
        # `python -m skywatch.airports.fetch` again, which they'll
        # follow with a refresh anyway.
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        if not head_only:
            self.wfile.write(data)


class StaticServer:
    """Threaded HTTP server serving a directory."""

    def __init__(
        self,
        directory: Path,
        host: str = "127.0.0.1",
        port: int = 8080,
        data_dir: Path | None = None,
    ):
        self.directory = Path(directory).resolve()
        self.data_dir = (
            Path(data_dir).resolve() if data_dir is not None
            else (self.directory.parent / "data").resolve()
        )
        self.host = host
        self.port = port
        self._server: socketserver.TCPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        directory = str(self.directory)
        data_dir = self.data_dir

        # Build a handler class that serves from `directory` and knows
        # where the `/data/` allow-list lives.
        class _Handler(_SkywatchHandler):
            pass
        _Handler.data_dir = data_dir

        def _handler_factory(*args, **kwargs):
            return _Handler(*args, directory=directory, **kwargs)

        socketserver.TCPServer.allow_reuse_address = True
        self._server = socketserver.ThreadingTCPServer(
            (self.host, self.port), _handler_factory,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="skywatch-http",
        )
        self._thread.start()
        log.info("HTTP server serving %s on http://%s:%d (data: %s)",
                 self.directory, self.host, self.port, self.data_dir)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
