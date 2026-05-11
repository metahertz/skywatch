"""Edge ↔ central transport layer.

In skywatch's edge architecture, each receiver site runs its own
`skywatch.edge` process that does full local decode and pushes
*deltas* (per-aircraft state updates and events) to a single central
`skywatch.central` process that merges across receivers and serves the
UI.

Two transport implementations live in this package, deliberately
mutually exclusive at runtime so the operator can A/B their
latency-vs-simplicity tradeoffs without ambiguity:

  * `mongo_changestream`  — edges write to a MongoDB `state_deltas`
                             collection; central tails a change stream.
                             Persistent by design; ~100-500 ms latency.
  * `websocket_push`      — edges open an outbound WebSocket to central;
                             central runs an inbound ingest server.
                             ~10-50 ms latency; non-persistent.

This module defines:
  * The `Delta` envelope (the wire shape of every edge → central message)
  * The `Transport` ABC the two backends implement
  * `make_transport()` — a small factory used by both edge and central
    entrypoints to construct the chosen transport from CLI args.
"""
from __future__ import annotations

import abc
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

# ----------------------------------------------------------------------
# Delta envelope
# ----------------------------------------------------------------------

DELTA_TYPE_AIRCRAFT = "aircraft"     # per-aircraft state update from one RX
DELTA_TYPE_EVENT = "event"           # TCAS RA / intent_change / emergency
DELTA_TYPE_RECEIVER = "receiver"     # receiver registry status
DELTA_TYPE_METRICS = "metrics"       # per-receiver health sample
# One per VDL2 frame the edge ingested (CPDLC / ACARS / ATN-CM /
# link-mgmt).  Carries just the comms-relevant fields produced by
# `parse_vdl2_line()` — no full Aircraft payload — so the central
# can populate the dedicated `comms` time-series collection without
# needing to re-derive the message shape from `Aircraft.comms`.
#
# Wire shape (payload):
#   {
#     "ts": float,            # frame.ts (edge wall-clock)
#     "frame_ts": float|None, # dumpvdl2 message timestamp
#     "src_icao": str|None, "dst_icao": str|None,
#     "aircraft_icao": str|None,
#     "direction": "uplink"|"downlink"|"peer",
#     "kind": "cpdlc"|"acars"|"atn_cm"|"link_mgmt"|"other",
#     "label": str|None, "text": str|None,
#     "flight": str|None, "reg": str|None,
#     "sig_level": float|None,
#     "raw": str,             # original dumpvdl2 JSON line
#   }
DELTA_TYPE_COMMS = "comms"


@dataclass
class Delta:
    """One unit of work the edge ships to the central.

    Fields:
      * `type`        — see DELTA_TYPE_* constants
      * `receiver_id` — the edge's receiver_id
      * `gen`         — monotonic per-edge sequence number; central uses
                        it to detect gaps and request resync.  Reset to
                        0 on edge restart.
      * `ts`          — wall-clock seconds at the edge when this delta
                        was generated.  Wire timestamp; merger should
                        use *receipt* time at central for "freshest" decisions.
      * `payload`     — the type-specific dict (Aircraft.to_dict, event,
                        receiver.to_dict, metrics dict)
    """
    type: str
    receiver_id: str
    gen: int
    payload: dict
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Delta":
        return cls(
            type=d["type"],
            receiver_id=d["receiver_id"],
            gen=int(d.get("gen", 0)),
            payload=d.get("payload") or {},
            ts=float(d.get("ts") or time.time()),
        )


# ----------------------------------------------------------------------
# Transport ABC
# ----------------------------------------------------------------------

DeltaCallback = Callable[[Delta], None]


class Transport(abc.ABC):
    """Bidirectional edge ↔ central transport.

    The same class implements both ends; the role is decided by which
    methods the caller actually invokes.  An edge calls `start()` then
    `send(delta)` repeatedly.  A central calls `start()` and
    `subscribe(callback)`; the transport pumps incoming deltas through
    the callback on its own thread.
    """

    @abc.abstractmethod
    def start(self) -> None:
        """Open the underlying connection / spawn worker threads."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Close cleanly.  Idempotent."""

    @abc.abstractmethod
    def send(self, delta: Delta) -> bool:
        """Edge-side: hand a delta to the transport.

        Must NOT block on the network.  Returns True if the delta was
        accepted (in-memory queue or wire); False if the transport
        rejected it (queue full, transport closed) and the caller
        should fall back to the spool.
        """

    @abc.abstractmethod
    def subscribe(self, callback: DeltaCallback) -> None:
        """Central-side: register a callback that fires once per
        incoming delta.  Multiple subscriptions are allowed; each
        callback is called for every delta in registration order."""

    # Optional: stats accessor for diagnostics.
    @property
    def stats(self) -> dict:
        return {}


# ----------------------------------------------------------------------
# Factory
# ----------------------------------------------------------------------

def make_transport(kind: str, **kwargs) -> Transport:
    """Construct a transport by name.  `kind` is "mongo" or "ws"."""
    if kind == "mongo":
        from .mongo_changestream import MongoChangeStreamTransport
        return MongoChangeStreamTransport(**kwargs)
    if kind == "ws":
        from .websocket_push import WebSocketPushTransport
        return WebSocketPushTransport(**kwargs)
    raise ValueError(f"Unknown transport kind: {kind!r}")
