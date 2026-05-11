"""WebSocket-backed transport: edges open an outbound WS to central;
central runs an inbound WS ingest server on a separate port.

Lower latency than the Mongo change-stream transport (~10-50 ms LAN),
no Mongo dependency on the wire path, but no built-in durability —
when central is unreachable the edge spool is the only safety net.

Auth: a shared bearer token, optional.  Edges send it twice — once in
an `Authorization: Bearer …` header on the WS upgrade (cosmetic; the
stock WebSocketServer doesn't surface request headers to application
code) and once as the *first* WS text frame after the upgrade:
`{"type": "hello", "token": "..."}`.  The application-layer hello is
what the central actually validates.  Mismatch closes the socket from
the server side.  When no token is configured anywhere, every
inbound connection auto-authenticates — fine on a trusted LAN, not
fine on a public path.  Generate a token with
`python -m skywatch.central gen-token`.

Both directions share the existing `skywatch.server.websocket` codebase
where possible — the inbound server *is* a stock `WebSocketServer` with
a delta-aware `on_message`.  Edge-side we ship a minimal blocking
client (no extra deps) that handshakes against any RFC 6455 server.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import queue
import socket
import struct
import threading
import time
from urllib.parse import urlsplit

from . import Delta, DeltaCallback, Transport

log = logging.getLogger("skywatch.transport.ws")

_HANDSHAKE_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Opcodes (RFC 6455 §5.2)
_OP_TEXT = 0x1
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

_SEND_QUEUE_MAX = 5000


class WebSocketPushTransport(Transport):
    """One transport instance acts as either edge sender or central
    receiver, depending on which methods the caller invokes.  In
    edge mode `central_url` must be set; in central mode `bind` must
    be set."""

    def __init__(
        self,
        *,
        # Edge-side: where to push to.
        central_url: str | None = None,
        # Central-side: where to listen.
        bind: str | None = None,           # "host:port"
        path: str = "/ingest",
        # Shared auth.  Either supply the token directly, or a $VAR name to
        # read it from.
        token: str | None = None,
        token_env: str | None = None,
        send_queue_max: int = _SEND_QUEUE_MAX,
    ):
        self.central_url = central_url
        self.bind = bind
        self.path = path
        if token_env and not token:
            token = os.environ.get(token_env)
        self.token = token

        self._send_q: queue.Queue = queue.Queue(maxsize=send_queue_max)
        self._sender_thread: threading.Thread | None = None

        self._subs: list[DeltaCallback] = []
        self._server = None  # WebSocketServer instance, lazily

        self._stop = threading.Event()
        self.sent_total = 0
        self.send_dropped_queue = 0
        self.received_total = 0

    # -- lifecycle ----------------------------------------------------

    def start(self) -> None:
        # Edge sender thread starts on demand (first send call); cheap.
        if self.central_url:
            self._sender_thread = threading.Thread(
                target=self._sender_loop, daemon=True,
                name="skywatch-ws-tx-sender",
            )
            self._sender_thread.start()
        if self.bind:
            self._start_server()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._send_q.put_nowait(None)
        except queue.Full:
            pass
        if self._sender_thread is not None:
            self._sender_thread.join(timeout=2.0)
        if self._server is not None:
            try:
                self._server.stop()
            except Exception:
                pass

    @property
    def stats(self) -> dict:
        return {
            "sent_total": self.sent_total,
            "send_dropped_queue": self.send_dropped_queue,
            "received_total": self.received_total,
            "send_queue_depth": self._send_q.qsize(),
        }

    # -- edge: send ---------------------------------------------------

    def send(self, delta: Delta) -> bool:
        try:
            self._send_q.put_nowait(delta)
        except queue.Full:
            self.send_dropped_queue += 1
            return False
        return True

    def _sender_loop(self) -> None:
        """Maintain a long-lived outbound connection to central, with
        exponential backoff on failure.  When connected, drain the
        send queue and write each delta as one text frame."""
        backoff = 1.0
        while not self._stop.is_set():
            sock = None
            try:
                sock = self._connect_and_handshake()
                # Application-layer hello carrying the bearer token; the
                # central side validates this before accepting any
                # deltas (see WebSocketPushTransport._on_inbound_message).
                if self.token:
                    hello = json.dumps(
                        {"type": "hello", "token": self.token},
                        separators=(",", ":"),
                    ).encode("utf-8")
                    self._send_text(sock, hello)
                log.info("WS transport connected to %s", self.central_url)
                backoff = 1.0
                # Drain the queue while connected
                while not self._stop.is_set():
                    item = self._send_q.get()
                    if item is None and self._stop.is_set():
                        break
                    if not isinstance(item, Delta):
                        continue
                    payload = json.dumps(item.to_dict(),
                                         separators=(",", ":")).encode("utf-8")
                    self._send_text(sock, payload)
                    self.sent_total += 1
            except (OSError, ConnectionError, ValueError) as e:
                if self._stop.is_set():
                    return
                log.warning("WS transport error: %s — reconnecting in %.1fs",
                            e, backoff)
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            if not self._stop.is_set():
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    def _connect_and_handshake(self) -> socket.socket:
        url = urlsplit(self.central_url)
        host = url.hostname or "localhost"
        port = url.port or (443 if url.scheme == "wss" else 80)
        if url.scheme == "wss":
            raise ValueError("wss:// not supported in v1; put TLS in front of central")
        sock = socket.create_connection((host, port), timeout=5)
        sock.settimeout(None)
        # Build handshake.
        nonce = base64.b64encode(os.urandom(16)).decode()
        path = url.path or self.path
        if url.query:
            path += "?" + url.query
        headers = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {nonce}",
            "Sec-WebSocket-Version: 13",
        ]
        if self.token:
            headers.append(f"Authorization: Bearer {self.token}")
        req = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii")
        sock.sendall(req)
        # Read response.  Tiny parser: read until \r\n\r\n.
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("central closed during handshake")
            buf += chunk
            if len(buf) > 16384:
                raise ConnectionError("central handshake response too large")
        head, _, _rest = buf.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode("ascii", "replace")
        if " 101 " not in status:
            raise ConnectionError(f"handshake failed: {status}")
        # We don't bother validating Sec-WebSocket-Accept; if the server
        # spoke 101 we trust it.  RFC compliance is on us for the frames.
        return sock

    @staticmethod
    def _send_text(sock: socket.socket, payload: bytes) -> None:
        """Write one masked text frame.  Clients MUST mask per RFC 6455."""
        header = bytearray()
        header.append(0x80 | _OP_TEXT)            # FIN=1, opcode=text
        n = len(payload)
        mask_bit = 0x80
        if n < 126:
            header.append(mask_bit | n)
        elif n < 65536:
            header.append(mask_bit | 126)
            header += struct.pack("!H", n)
        else:
            header.append(mask_bit | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        sock.sendall(bytes(header) + masked)

    # -- central: subscribe ------------------------------------------

    def subscribe(self, callback: DeltaCallback) -> None:
        self._subs.append(callback)

    def _start_server(self) -> None:
        from skywatch.server.websocket import WebSocketServer
        host, _, port = self.bind.partition(":")
        self._server = WebSocketServer(
            host=host or "0.0.0.0",
            port=int(port or 8767),
            on_message=self._on_inbound_message,
            on_open=self._on_inbound_open,
        )
        # Optional: reject upgrades without the right Authorization header.
        # The stock WebSocketServer doesn't expose handshake headers;
        # since the token is an opt-in trust boundary, we enforce it
        # at the application layer instead — first incoming message
        # must include a "type": "hello" with the token if configured.
        # This is a small protocol layer documented at the edge end too.
        self._server.start()
        log.info("WS ingest listening on %s%s", self.bind, self.path)

    def _on_inbound_open(self, client) -> None:
        # We don't push anything on connect; edges initiate.  But if
        # the auth token is configured, mark this client as
        # un-authenticated until the first hello.
        if self.token:
            client._sw_authed = False
        else:
            client._sw_authed = True

    def _on_inbound_message(self, client, text: str) -> None:
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            return
        # Handle the auth handshake first (when a token is configured).
        # `hmac.compare_digest` over `==` to keep timing-attack
        # resistance if anyone ever puts TLS in front of this; both
        # operands are coerced to str so a missing/null `token` field
        # on the wire doesn't raise.
        if not getattr(client, "_sw_authed", True):
            supplied = str(doc.get("token") or "")
            if (doc.get("type") == "hello"
                    and hmac.compare_digest(supplied, self.token or "")):
                client._sw_authed = True
                return
            try:
                client.close()
            except Exception:
                pass
            return
        try:
            delta = Delta.from_dict(doc)
        except Exception:
            return
        self.received_total += 1
        for cb in self._subs:
            try:
                cb(delta)
            except Exception:
                log.exception("delta callback raised")
