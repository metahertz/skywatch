"""Minimal pure-stdlib WebSocket server (RFC 6455).

Why not use the `websockets` package? Skywatch is meant to run on minimal,
possibly air-gapped systems. Bundling a WS implementation that fits in
~200 lines means a zero-dependency install. We support exactly what we
need: the server side of an unmasked text-frame stream.

Reference: RFC 6455 sections 5.2 and 5.5.

Usage:
    server = WebSocketServer(host="0.0.0.0", port=8765, on_open=..., on_message=...)
    server.start()                      # spawns daemon thread
    server.broadcast('{"type": "..."}')
    server.stop()
"""
from __future__ import annotations

import base64
import hashlib
import logging
import socket
import struct
import threading
from typing import Callable

log = logging.getLogger("skywatch.ws")

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# Opcodes per RFC 6455 §5.2
OPCODE_CONT = 0x0
OPCODE_TEXT = 0x1
OPCODE_BIN = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA


class WebSocketClient:
    """One connected client. Thread-safe sends; reads in own thread."""

    def __init__(self, sock: socket.socket, addr: tuple) -> None:
        self.sock = sock
        self.addr = addr
        self._send_lock = threading.Lock()
        self._closed = False

    def send_text(self, message: str) -> None:
        if self._closed:
            return
        try:
            data = message.encode("utf-8")
            frame = self._build_frame(OPCODE_TEXT, data)
            with self._send_lock:
                self.sock.sendall(frame)
        except (OSError, BrokenPipeError):
            self.close()

    def send_pong(self, payload: bytes) -> None:
        if self._closed:
            return
        try:
            with self._send_lock:
                self.sock.sendall(self._build_frame(OPCODE_PONG, payload))
        except OSError:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            with self._send_lock:
                # Best-effort close frame
                try:
                    self.sock.sendall(self._build_frame(OPCODE_CLOSE, b"\x03\xe8"))
                except OSError:
                    pass
                self.sock.close()
        except OSError:
            pass

    @property
    def closed(self) -> bool:
        return self._closed

    @staticmethod
    def _build_frame(opcode: int, payload: bytes) -> bytes:
        """Build an unmasked server -> client frame."""
        b1 = 0x80 | (opcode & 0x0F)  # FIN=1, opcode
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", b1, n)
        elif n < 65536:
            header = struct.pack("!BBH", b1, 126, n)
        else:
            header = struct.pack("!BBQ", b1, 127, n)
        return header + payload


class WebSocketServer:
    """Tiny multi-client WS server.  One thread per client.

    Callbacks:
      on_open(client)             - new client connected
      on_message(client, text)    - received a text frame
      on_close(client)            - client disconnected
    All callbacks run on a per-client thread; the server doesn't
    serialise them.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        on_open: Callable | None = None,
        on_message: Callable | None = None,
        on_close: Callable | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.on_open = on_open
        self.on_message = on_message
        self.on_close = on_close

        self._clients: set[WebSocketClient] = set()
        self._clients_lock = threading.Lock()
        self._listen_sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(8)
        s.settimeout(0.5)  # so we can poll _stop
        self._listen_sock = s
        self._accept_thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="skywatch-ws-accept",
        )
        self._accept_thread.start()
        log.info("WebSocket server listening on ws://%s:%d", self.host, self.port)

    def stop(self) -> None:
        self._stop.set()
        with self._clients_lock:
            for c in list(self._clients):
                c.close()
            self._clients.clear()
        if self._listen_sock:
            try:
                self._listen_sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Broadcast
    # ------------------------------------------------------------------

    def broadcast(self, message: str) -> int:
        """Send `message` (already JSON-encoded text) to every client.
        Returns the number of clients still connected after the send.
        """
        with self._clients_lock:
            clients = list(self._clients)
        sent = 0
        dead = []
        for c in clients:
            if c.closed:
                dead.append(c)
                continue
            c.send_text(message)
            if c.closed:
                dead.append(c)
            else:
                sent += 1
        if dead:
            with self._clients_lock:
                for c in dead:
                    self._clients.discard(c)
        return sent

    @property
    def client_count(self) -> int:
        with self._clients_lock:
            return len(self._clients)

    # ------------------------------------------------------------------
    # Accept loop & per-client handling
    # ------------------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._listen_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(
                target=self._handle_client, args=(conn, addr),
                daemon=True, name=f"skywatch-ws-client-{addr[1]}",
            )
            t.start()

    def _handle_client(self, conn: socket.socket, addr: tuple) -> None:
        try:
            if not self._handshake(conn):
                conn.close()
                return
        except Exception as e:
            log.debug("handshake failed for %s: %s", addr, e)
            try:
                conn.close()
            except OSError:
                pass
            return

        client = WebSocketClient(conn, addr)
        with self._clients_lock:
            self._clients.add(client)
        log.info("WS client connected from %s", addr)

        if self.on_open:
            try:
                self.on_open(client)
            except Exception:
                log.exception("on_open callback failed")

        try:
            self._receive_loop(client)
        finally:
            with self._clients_lock:
                self._clients.discard(client)
            client.close()
            log.info("WS client disconnected from %s", addr)
            if self.on_close:
                try:
                    self.on_close(client)
                except Exception:
                    log.exception("on_close callback failed")

    # ------------------------------------------------------------------
    # RFC 6455 handshake
    # ------------------------------------------------------------------

    def _handshake(self, conn: socket.socket) -> bool:
        """Read the HTTP upgrade request and send the 101 response."""
        conn.settimeout(5.0)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return False
            data += chunk
            if len(data) > 16384:
                return False  # absurd request, bail
        headers = self._parse_headers(data)
        if headers.get("upgrade", "").lower() != "websocket":
            return False
        key = headers.get("sec-websocket-key")
        if not key:
            return False
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        )
        conn.sendall(response.encode("ascii"))
        conn.settimeout(None)
        return True

    @staticmethod
    def _parse_headers(raw: bytes) -> dict:
        """Return lowercase-keyed dict of HTTP headers."""
        lines = raw.split(b"\r\n\r\n", 1)[0].split(b"\r\n")
        headers = {}
        for line in lines[1:]:
            if b":" in line:
                k, _, v = line.partition(b":")
                headers[k.decode("ascii").strip().lower()] = (
                    v.decode("ascii", "replace").strip()
                )
        return headers

    # ------------------------------------------------------------------
    # Frame reader
    # ------------------------------------------------------------------

    def _receive_loop(self, client: WebSocketClient) -> None:
        sock = client.sock
        while not self._stop.is_set() and not client.closed:
            try:
                header = self._recv_exact(sock, 2)
            except (OSError, ConnectionError):
                return
            if header is None:
                return
            b1, b2 = header
            fin = (b1 & 0x80) != 0
            opcode = b1 & 0x0F
            masked = (b2 & 0x80) != 0
            length = b2 & 0x7F

            if length == 126:
                ext = self._recv_exact(sock, 2)
                if ext is None:
                    return
                length = struct.unpack("!H", ext)[0]
            elif length == 127:
                ext = self._recv_exact(sock, 8)
                if ext is None:
                    return
                length = struct.unpack("!Q", ext)[0]

            mask_key = b""
            if masked:
                mask_key = self._recv_exact(sock, 4)
                if mask_key is None:
                    return

            payload = b""
            if length:
                payload = self._recv_exact(sock, length)
                if payload is None:
                    return
                if masked:
                    payload = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))

            if opcode == OPCODE_CLOSE:
                return
            elif opcode == OPCODE_PING:
                client.send_pong(payload)
            elif opcode == OPCODE_PONG:
                pass  # keep-alive ignored
            elif opcode == OPCODE_TEXT and fin:
                if self.on_message:
                    try:
                        self.on_message(client, payload.decode("utf-8", "replace"))
                    except Exception:
                        log.exception("on_message callback failed")
            # Continuation frames and binary not used by skywatch's protocol.

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
        out = bytearray()
        while len(out) < n:
            chunk = sock.recv(n - len(out))
            if not chunk:
                return None
            out.extend(chunk)
        return bytes(out)
