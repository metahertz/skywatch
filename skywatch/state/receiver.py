"""Receiver registry — one entry per BEAST source feeding the engine.

Skywatch supports ingesting from multiple receivers simultaneously
(co-located antennas or distributed sites with overlapping coverage).
Every BeastFrame carries the receiver_id of the source that produced
it; this registry keeps the per-receiver metadata (location, range
filter, link health) the engine and UI need.

The registry is in-memory and authoritative for a running process.
When persistence is enabled, MongoStore mirrors it to the `receivers`
collection on every change so the registry survives a restart.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class Receiver:
    """One BEAST source.  `id` is the stable handle used everywhere."""
    id: str
    name: str | None = None
    lat: float | None = None
    lon: float | None = None
    max_range_nm: float = 280.0

    # Link-health counters.
    connected: bool = False
    first_seen: float = field(default_factory=time.time)
    last_frame_at: float | None = None
    frames_total: int = 0
    frames_dropped: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name or self.id,
            "lat": self.lat,
            "lon": self.lon,
            "max_range_nm": self.max_range_nm,
            "connected": self.connected,
            "first_seen": self.first_seen,
            "last_frame_at": self.last_frame_at,
            "frames_total": self.frames_total,
            "frames_dropped": self.frames_dropped,
        }


class ReceiverRegistry:
    """Mutable map of receiver_id → Receiver."""

    def __init__(self) -> None:
        self._receivers: dict[str, Receiver] = {}

    def __contains__(self, receiver_id: str) -> bool:
        return receiver_id in self._receivers

    def __iter__(self):
        return iter(self._receivers.values())

    def __len__(self) -> int:
        return len(self._receivers)

    def get(self, receiver_id: str) -> Receiver | None:
        return self._receivers.get(receiver_id)

    def upsert(
        self,
        receiver_id: str,
        *,
        name: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        max_range_nm: float | None = None,
    ) -> Receiver:
        """Create or update a receiver entry.  Only non-None fields
        overwrite; this lets the CLI seed a partial entry that's later
        filled in by the BEAST client (e.g. once a connection succeeds)."""
        rx = self._receivers.get(receiver_id)
        if rx is None:
            rx = Receiver(id=receiver_id)
            self._receivers[receiver_id] = rx
        if name is not None:
            rx.name = name
        if lat is not None:
            rx.lat = lat
        if lon is not None:
            rx.lon = lon
        if max_range_nm is not None:
            rx.max_range_nm = max_range_nm
        return rx

    def get_or_create(self, receiver_id: str) -> Receiver:
        return self.upsert(receiver_id)

    def primary(self) -> Receiver | None:
        """The first registered receiver, used for legacy single-RX views
        (range ring / `receiver` snapshot block).  Returns None if empty."""
        for rx in self._receivers.values():
            return rx
        return None

    def to_list(self) -> list[dict]:
        return [rx.to_dict() for rx in self._receivers.values()]
