"""Simple static file server for the skywatch web UI.

Serves the contents of `web/` over HTTP.  Pure stdlib.
"""
from __future__ import annotations

import http.server
import logging
import socketserver
import threading
from pathlib import Path

log = logging.getLogger("skywatch.http")


class _SilentHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler that doesn't spam stderr with every request."""

    def log_message(self, fmt: str, *args) -> None:
        log.debug(fmt, *args)


class StaticServer:
    """Threaded HTTP server serving a directory."""

    def __init__(self, directory: Path, host: str = "127.0.0.1", port: int = 8080):
        self.directory = Path(directory).resolve()
        self.host = host
        self.port = port
        self._server: socketserver.TCPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        directory = str(self.directory)

        # Build a handler class that serves from `directory`. Python 3.7+
        # supports the `directory` constructor arg via partial.
        def _handler_factory(*args, **kwargs):
            return _SilentHandler(*args, directory=directory, **kwargs)

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
        log.info("HTTP server serving %s on http://%s:%d",
                 self.directory, self.host, self.port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
