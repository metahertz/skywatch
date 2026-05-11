"""Edge runner: BEAST → StateEngine → Transport sink, with hybrid
in-memory + spool buffering.

The edge process exists to keep one receiver-site's state machine
local — the existing `StateEngine` does the actual decode/CPR/BDS
work.  This module just wires it together with a transport sink so
every state change becomes a `Delta` shipped to the central.

Wiring:
  * `BeastParser(receiver_id=name)` tags frames.
  * `StateEngine` consumes them, building per-aircraft state.
  * We `subscribe()` to the engine's update/event firehose; each
    callback is converted into a `Delta` and handed to a transport.
  * If the transport rejects (queue full, disconnected), the delta
    rolls onto a sqlite spool which the same loop drains on the
    next tick once the transport recovers.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from pathlib import Path

from skywatch.decoder.beast import BeastParser
from skywatch.edge.spool import Spool
from skywatch.state import StateEngine
from skywatch.state.aircraft import Aircraft
from skywatch.transport import (
    DELTA_TYPE_AIRCRAFT, DELTA_TYPE_COMMS, DELTA_TYPE_EVENT,
    DELTA_TYPE_RECEIVER, Delta, Transport,
)

log = logging.getLogger("skywatch.edge.runner")


class EdgeRunner:
    """One edge process worth of state.  Owns the engine, the BEAST
    client thread, and the spool drainer."""

    def __init__(
        self,
        receiver_id: str,
        beast_host: str,
        beast_port: int,
        transport: Transport,
        receiver_lat: float | None = None,
        receiver_lon: float | None = None,
        max_range_nm: float = 280.0,
        spool_path: Path | None = None,
        info_lookup=None,
        vdl2_host: str | None = None,
        vdl2_port: int | None = None,
    ):
        self.receiver_id = receiver_id
        self.beast_host = beast_host
        self.beast_port = beast_port
        self.vdl2_host = vdl2_host
        self.vdl2_port = vdl2_port
        self.transport = transport
        self.spool = Spool(spool_path) if spool_path else None

        # Per-edge gen counter — applied to every outgoing delta in
        # registration order, with the engine's update events serving
        # as the natural sequencing point.  Reset to 0 on edge restart;
        # central detects the reset and resyncs.
        self._gen = 0
        self._gen_lock = threading.Lock()

        # Build a fresh engine seeded with this single receiver.
        self.engine = StateEngine(
            receiver_lat=receiver_lat,
            receiver_lon=receiver_lon,
            max_range_nm=max_range_nm,
            info_lookup=info_lookup,
        )
        self.engine.receivers.upsert(
            receiver_id, name=receiver_id,
            lat=receiver_lat, lon=receiver_lon,
            max_range_nm=max_range_nm,
        )
        # Wire engine emissions to the transport.
        self.engine.subscribe(self._on_engine_event)
        # Per-VDL2-frame hook: ship a dedicated comms delta so the
        # central populates its `comms` time-series collection.
        # Without this, only the embedded Aircraft.comms list (inside
        # DELTA_TYPE_AIRCRAFT) and the ticker event survive the trip.
        self.engine.on_vdl2_frame = self._ship_comms_delta

        self._stop = threading.Event()
        self._beast_thread: threading.Thread | None = None
        self._vdl2_thread: threading.Thread | None = None
        self._spool_thread: threading.Thread | None = None

    # -- lifecycle ----------------------------------------------------

    def start(self) -> None:
        # First, push the receiver registration so the central knows
        # who we are before any aircraft data arrives.
        self._push_receiver_state()
        # BEAST input thread
        self._beast_thread = threading.Thread(
            target=self._beast_loop, daemon=True,
            name=f"skywatch-edge-beast-{self.receiver_id}",
        )
        self._beast_thread.start()
        # VDL2 input thread (optional).
        if self.vdl2_host and self.vdl2_port:
            self._vdl2_thread = threading.Thread(
                target=self._vdl2_loop, daemon=True,
                name=f"skywatch-edge-vdl2-{self.receiver_id}",
            )
            self._vdl2_thread.start()
        # Spool drain thread (only if spool configured)
        if self.spool is not None:
            self._spool_thread = threading.Thread(
                target=self._spool_drain_loop, daemon=True,
                name=f"skywatch-edge-spool-{self.receiver_id}",
            )
            self._spool_thread.start()

    def stop(self) -> None:
        self._stop.set()
        for t in (self._beast_thread, self._vdl2_thread, self._spool_thread):
            if t is not None:
                t.join(timeout=2.0)
        if self.spool is not None:
            self.spool.close()

    # -- transport bridging ------------------------------------------

    def _next_gen(self) -> int:
        with self._gen_lock:
            self._gen += 1
            return self._gen

    def _ship(self, kind: str, payload: dict) -> None:
        """Either send to transport, or roll onto the spool."""
        delta = Delta(
            type=kind,
            receiver_id=self.receiver_id,
            gen=self._next_gen(),
            payload=payload,
        )
        ok = self.transport.send(delta)
        if not ok and self.spool is not None:
            try:
                self.spool.enqueue(delta.to_dict())
            except Exception:
                log.exception("spool enqueue failed; dropping delta")

    def _on_engine_event(self, env: dict) -> None:
        """Engine-event listener.  Converts each engine event into the
        wire delta that the central consumes."""
        if env.get("type") == "update":
            payload = env.get("data") or {}
            self._ship(DELTA_TYPE_AIRCRAFT, payload)
        elif env.get("type") == "event":
            self._ship(DELTA_TYPE_EVENT, env.get("event") or {})

    def _push_receiver_state(self) -> None:
        rx = self.engine.receivers.get(self.receiver_id)
        if rx is None:
            return
        self._ship(DELTA_TYPE_RECEIVER, rx.to_dict())

    def _ship_comms_delta(self, frame) -> None:
        """Engine on_vdl2_frame hook: emit one DELTA_TYPE_COMMS per
        VDL2 frame so the central can populate its dedicated `comms`
        time-series collection.  Wire shape is documented alongside
        the constant in skywatch/transport/__init__.py."""
        self._ship(DELTA_TYPE_COMMS, {
            "ts": frame.ts,
            "frame_ts": frame.frame_ts,
            "src_icao": frame.src_icao,
            "dst_icao": frame.dst_icao,
            "aircraft_icao": frame.aircraft_icao,
            "direction": frame.direction,
            "kind": frame.kind,
            "label": frame.label,
            "text": frame.text,
            "flight": frame.flight,
            "reg": frame.reg,
            "sig_level": frame.sig_level,
            "raw": frame.raw,
        })

    # -- spool drain --------------------------------------------------

    def _spool_drain_loop(self) -> None:
        """Best-effort: when the in-memory queue is healthy and the
        spool has rows, hand them back to the transport in FIFO order.
        We deliberately let `transport.send()` queue them — same path
        a fresh delta takes — so we never have two competing senders."""
        backoff = 0.5
        while not self._stop.is_set():
            try:
                rows = self.spool.peek_batch(64)
            except Exception:
                rows = []
            if not rows:
                self._stop.wait(0.5)
                continue
            sent_to = None
            for rowid, doc in rows:
                try:
                    delta = Delta.from_dict(doc)
                except Exception:
                    self.spool.pop_to(rowid)
                    continue
                if not self.transport.send(delta):
                    # Transport's queue is full again — back off and
                    # retry from the same point.
                    break
                sent_to = rowid
            if sent_to is not None:
                self.spool.pop_to(sent_to)
                backoff = 0.5
            else:
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 5.0)

    # -- BEAST input loop --------------------------------------------

    def _beast_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            sock = None
            try:
                log.info("[%s] connecting to BEAST %s:%d", self.receiver_id,
                         self.beast_host, self.beast_port)
                sock = socket.create_connection(
                    (self.beast_host, self.beast_port), timeout=5)
                sock.settimeout(None)
                log.info("[%s] connected", self.receiver_id)
                backoff = 1.0
                parser = BeastParser(receiver_id=self.receiver_id)
                while not self._stop.is_set():
                    chunk = sock.recv(8192)
                    if not chunk:
                        log.warning("[%s] BEAST source closed",
                                    self.receiver_id)
                        break
                    for frame in parser.feed(chunk):
                        self.engine.feed(frame)
            except (OSError, ConnectionError) as e:
                log.warning("[%s] BEAST error: %s — reconnect in %.1fs",
                            self.receiver_id, e, backoff)
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                rx = self.engine.receivers.get(self.receiver_id)
                if rx is not None:
                    rx.connected = False
                    # Tell the central about the disconnect.
                    self._push_receiver_state()
            if not self._stop.is_set():
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    # -- VDL2 input loop ---------------------------------------------

    def _vdl2_loop(self) -> None:
        """Mirror of `_beast_loop` for one dumpvdl2 JSON source.

        dumpvdl2 emits newline-delimited JSON on TCP.  Each line goes
        through `parse_vdl2_line()` and into `engine.feed_vdl2()`,
        which fires the `on_vdl2_frame` hook this runner registered
        — that hook in turn ships a `DELTA_TYPE_COMMS` envelope per
        frame.
        """
        from skywatch.decoder.vdl2 import parse_vdl2_line
        backoff = 1.0
        while not self._stop.is_set():
            sock = None
            try:
                log.info("[%s] connecting to dumpvdl2 %s:%d",
                         self.receiver_id, self.vdl2_host, self.vdl2_port)
                sock = socket.create_connection(
                    (self.vdl2_host, self.vdl2_port), timeout=5)
                sock.settimeout(None)
                log.info("[%s] connected (vdl2)", self.receiver_id)
                backoff = 1.0
                buf = bytearray()
                while not self._stop.is_set():
                    chunk = sock.recv(8192)
                    if not chunk:
                        log.warning("[%s] dumpvdl2 source closed",
                                    self.receiver_id)
                        break
                    buf.extend(chunk)
                    while True:
                        nl = buf.find(b"\n")
                        if nl < 0:
                            break
                        line = bytes(buf[:nl]).decode("utf-8", "replace")
                        del buf[:nl + 1]
                        frame = parse_vdl2_line(
                            line, receiver_id=self.receiver_id)
                        if frame is not None:
                            self.engine.feed_vdl2(frame)
            except (OSError, ConnectionError) as e:
                log.warning("[%s] dumpvdl2 error: %s — reconnect in %.1fs",
                            self.receiver_id, e, backoff)
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            if not self._stop.is_set():
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
