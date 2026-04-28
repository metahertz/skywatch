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
        self._input_thread: Optional[threading.Thread] = None

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

    def start_beast_client(self, host: str, port: int) -> None:
        """Connect to a remote BEAST source (typically dump1090 on :30005).
        Reconnects automatically if the connection drops.
        """
        self._input_thread = threading.Thread(
            target=self._beast_client_loop,
            args=(host, port),
            daemon=True,
            name="skywatch-beast-input",
        )
        self._input_thread.start()

    def _beast_client_loop(self, host: str, port: int) -> None:
        """Runs in a thread; reconnects with exponential backoff on failure."""
        backoff = 1.0
        while not self._stop.is_set():
            sock = None
            try:
                log.info("Connecting to BEAST source at %s:%d ...", host, port)
                sock = socket.create_connection((host, port), timeout=5)
                sock.settimeout(None)
                log.info("Connected to BEAST source at %s:%d", host, port)
                backoff = 1.0  # reset on success
                parser = BeastParser()
                while not self._stop.is_set():
                    chunk = sock.recv(8192)
                    if not chunk:
                        log.warning("BEAST source closed the connection")
                        break
                    for frame in parser.feed(chunk):
                        self.engine.feed(frame)
            except (OSError, ConnectionError) as e:
                log.warning("BEAST client error: %s — reconnecting in %.1fs",
                            e, backoff)
            finally:
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
            # Backoff before reconnect
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

        def _runner():
            parser = BeastParser()
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
