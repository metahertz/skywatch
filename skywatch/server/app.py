"""Application server: BEAST TCP client + state engine + WebSocket broadcast.

This is the runtime that gets started by `python -m skywatch`. It connects
to a dump1090 BEAST stream (or runs the synthetic generator), feeds frames
into the StateEngine, and pushes deltas + periodic full snapshots to all
WebSocket subscribers.

Architecture:
    BEAST source ─► BeastParser ─► StateEngine ─► WS deltas/snapshots
                                       │
                                       └── Aircraft state with DB info

Wire protocol on the WebSocket (all messages are JSON):
    {"type": "snapshot", ...}        full state, sent to new clients
    {"type": "update", "icao", "data"}  per-aircraft delta
    {"type": "event", "event": ...}  e.g. new_aircraft, tcas_ra_started
    {"type": "stats", ...}           periodic counters / health
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from skywatch.decoder.beast import BeastParser
from skywatch.state import StateEngine

log = logging.getLogger("skywatch.app")


def _json_default(obj):
    """JSON serialiser fallback for deque, set, etc."""
    if isinstance(obj, (deque, set, frozenset)):
        return list(obj)
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


def _to_json(payload: dict) -> str:
    return json.dumps(payload, default=_json_default, separators=(",", ":"))


class AppServer:
    """Glues the BEAST input, the engine, and the broadcasters together."""

    def __init__(
        self,
        engine: StateEngine,
        ws_server,
        snapshot_interval_s: float = 10.0,
        update_throttle_ms: int = 500,
    ):
        self.engine = engine
        self.ws = ws_server
        self.snapshot_interval_s = snapshot_interval_s
        self.update_throttle_ms = update_throttle_ms

        # Per-ICAO last-broadcast time for throttling
        self._last_broadcast: dict[str, float] = {}
        # Buffer of pending updates (coalesced; final state wins)
        self._pending: dict[str, dict] = {}
        self._pending_lock = threading.Lock()

        # Background threads
        self._stop = threading.Event()
        self._broadcaster_thread: Optional[threading.Thread] = None
        self._snapshotter_thread: Optional[threading.Thread] = None
        # Single-receiver mode keeps `_input_thread` set for back-compat.
        # Multi-receiver mode appends every BEAST client to `_input_threads`.
        self._input_thread: Optional[threading.Thread] = None
        self._input_threads: list[threading.Thread] = []

    # ------------------------------------------------------------------
    # Listener wiring
    # ------------------------------------------------------------------

    def attach(self) -> None:
        """Subscribe to engine events. Call on construction."""
        self.engine.subscribe(self._on_engine_event)
        # Send a snapshot to each new WS client.
        self.ws.on_open = self._send_snapshot
        # Accept client → server config messages (e.g. enrichment toggles).
        self.ws.on_message = self._on_ws_message

    def _on_engine_event(self, event: dict) -> None:
        if event.get("type") == "update":
            icao = event["icao"]
            with self._pending_lock:
                # Coalesce: latest state wins
                self._pending[icao] = event["data"]
        elif event.get("type") == "event":
            # Events go out immediately (they're rare and important)
            self.ws.broadcast(_to_json(event))

    def _send_snapshot(self, client) -> None:
        snap = self.engine.snapshot()
        snap["config"] = self._current_config()
        client.send_text(_to_json(snap))

    def _current_config(self) -> dict:
        rr = getattr(self.engine, "route_resolver", None)
        return {
            "route_enrichment": bool(rr.enabled) if rr is not None else False,
            "route_enrichment_available": rr is not None,
        }

    def _on_ws_message(self, client, text: str) -> None:
        try:
            env = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(env, dict):
            return
        if env.get("type") == "set_route_enrichment":
            rr = getattr(self.engine, "route_resolver", None)
            if rr is None:
                return
            rr.set_enabled(bool(env.get("enabled")))
            # Echo the new config to all clients so every UI updates.
            self.ws.broadcast(_to_json({
                "type": "config",
                "config": self._current_config(),
            }))

    # ------------------------------------------------------------------
    # Broadcaster: drains the pending queue at fixed rate
    # ------------------------------------------------------------------

    def _broadcaster_loop(self) -> None:
        interval = self.update_throttle_ms / 1000.0
        while not self._stop.is_set():
            time.sleep(interval)
            with self._pending_lock:
                if not self._pending:
                    continue
                batch = self._pending
                self._pending = {}
            # Send as one combined message to reduce per-frame overhead
            msg = _to_json({
                "type": "updates",
                "aircraft": list(batch.values()),
                "t": time.time(),
            })
            self.ws.broadcast(msg)

    def _snapshotter_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(self.snapshot_interval_s)
            self.engine.prune_stale()
            snap_msg = _to_json(self.engine.snapshot())
            self.ws.broadcast(snap_msg)

    # ------------------------------------------------------------------
    # Input sources
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._broadcaster_thread = threading.Thread(
            target=self._broadcaster_loop, daemon=True, name="skywatch-broadcaster",
        )
        self._broadcaster_thread.start()
        self._snapshotter_thread = threading.Thread(
            target=self._snapshotter_loop, daemon=True, name="skywatch-snapshotter",
        )
        self._snapshotter_thread.start()

    def start_beast_client(
        self,
        host: str,
        port: int,
        receiver_id: str | None = None,
    ) -> None:
        """Connect to a remote BEAST source (typically dump1090 on :30005).
        Reconnects automatically if the connection drops.

        `receiver_id` is the stable string used to tag every frame
        produced by this connection.  Defaults to "host:port" when
        unspecified; AppServer can manage many concurrent clients,
        each with a distinct receiver_id, when the user has more than
        one --beast configured.
        """
        rid = receiver_id or f"{host}:{port}"
        thread = threading.Thread(
            target=self._beast_client_loop,
            args=(host, port, rid),
            daemon=True,
            name=f"skywatch-beast-{rid}",
        )
        # Track every input thread so multi-receiver setups can shut
        # down cleanly.  `_input_thread` retained for back-compat.
        self._input_thread = thread
        self._input_threads.append(thread)
        thread.start()

    def _beast_client_loop(
        self, host: str, port: int, receiver_id: str,
    ) -> None:
        """Runs in a thread; reconnects with exponential backoff on failure."""
        backoff = 1.0
        while not self._stop.is_set():
            sock = None
            try:
                log.info("[%s] Connecting to BEAST source at %s:%d ...",
                         receiver_id, host, port)
                sock = socket.create_connection((host, port), timeout=5)
                sock.settimeout(None)
                log.info("[%s] Connected to BEAST source at %s:%d",
                         receiver_id, host, port)
                backoff = 1.0  # reset on success
                # Tag every frame with this client's receiver_id.
                parser = BeastParser(receiver_id=receiver_id)
                while not self._stop.is_set():
                    chunk = sock.recv(8192)
                    if not chunk:
                        log.warning("[%s] BEAST source closed the connection",
                                    receiver_id)
                        break
                    for frame in parser.feed(chunk):
                        self.engine.feed(frame)
            except (OSError, ConnectionError) as e:
                log.warning("[%s] BEAST client error: %s — reconnecting in %.1fs",
                            receiver_id, e, backoff)
            finally:
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
                # Mark the receiver as disconnected.
                rx = self.engine.receivers.get(receiver_id)
                if rx is not None:
                    rx.connected = False
            # Backoff before reconnect
            if not self._stop.is_set():
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    def start_vdl2_client(
        self,
        host: str,
        port: int,
        receiver_id: str | None = None,
    ) -> None:
        """Connect to a remote dumpvdl2 JSON output (typically on
        :5555).  Reads newline-delimited JSON and feeds parsed
        VdlFrames into engine.feed_vdl2().  Reconnects on failure
        with exponential backoff identical to the BEAST client.
        """
        rid = receiver_id or f"vdl2:{host}:{port}"
        thread = threading.Thread(
            target=self._vdl2_client_loop,
            args=(host, port, rid),
            daemon=True,
            name=f"skywatch-vdl2-{rid}",
        )
        self._input_threads.append(thread)
        thread.start()

    def _vdl2_client_loop(
        self, host: str, port: int, receiver_id: str,
    ) -> None:
        """Runs in a thread; reconnects with exponential backoff."""
        from skywatch.decoder.vdl2 import parse_vdl2_line
        backoff = 1.0
        while not self._stop.is_set():
            sock = None
            try:
                log.info("[%s] Connecting to dumpvdl2 at %s:%d ...",
                         receiver_id, host, port)
                sock = socket.create_connection((host, port), timeout=5)
                sock.settimeout(None)
                log.info("[%s] Connected to dumpvdl2 at %s:%d",
                         receiver_id, host, port)
                backoff = 1.0
                # dumpvdl2 emits one JSON document per line.  Read by
                # accumulating until '\n' so partial network frames
                # don't truncate a JSON object mid-decode.
                buf = bytearray()
                while not self._stop.is_set():
                    chunk = sock.recv(8192)
                    if not chunk:
                        log.warning("[%s] dumpvdl2 closed the connection",
                                    receiver_id)
                        break
                    buf.extend(chunk)
                    while True:
                        nl = buf.find(b"\n")
                        if nl < 0:
                            break
                        line = bytes(buf[:nl]).decode("utf-8", "replace")
                        del buf[:nl + 1]
                        frame = parse_vdl2_line(line, receiver_id=receiver_id)
                        if frame is not None:
                            self.engine.feed_vdl2(frame)
            except (OSError, ConnectionError) as e:
                log.warning("[%s] dumpvdl2 client error: %s — reconnect in %.1fs",
                            receiver_id, e, backoff)
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                rx = self.engine.receivers.get(receiver_id)
                if rx is not None:
                    rx.connected = False
            if not self._stop.is_set():
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    def start_synthetic_input(self, scenario=None, time_scale: float = 1.0) -> None:
        """Run the synthetic message generator as input source."""
        from skywatch.decoder.synthetic import default_scenario
        from skywatch.decoder.beast import encode_beast

        scn = scenario or default_scenario()
        # Configure receiver position from scenario if engine doesn't have one
        if self.engine.receiver_lat is None:
            self.engine.receiver_lat = scn.receiver_lat
            self.engine.receiver_lon = scn.receiver_lon

        # Synthetic feed gets a stable receiver_id so the engine's
        # multi-receiver bookkeeping has something coherent to attribute
        # frames to (rather than every test creating a fresh "default"
        # entry by accident).
        SYNTH_RID = "synthetic"
        self.engine.receivers.upsert(
            SYNTH_RID, name="synthetic",
            lat=scn.receiver_lat, lon=scn.receiver_lon,
        )

        def _runner():
            parser = BeastParser(receiver_id=SYNTH_RID)
            tick = 0
            while not self._stop.is_set():
                tick += 1
                for t, msg in scn.step(1.0):
                    beast = encode_beast(msg, ts_seconds=t, signal=180)
                    for f in parser.feed(beast):
                        self.engine.feed(f)
                # Sleep wall-clock; time_scale<1 makes simulation faster
                self._stop.wait(1.0 / max(time_scale, 0.001))

        self._input_thread = threading.Thread(
            target=_runner, daemon=True, name="skywatch-synthetic-input",
        )
        self._input_thread.start()

    def stop(self) -> None:
        self._stop.set()
