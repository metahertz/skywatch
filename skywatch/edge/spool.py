"""On-disk delta spool — the durable side of the edge's hybrid buffer.

When the in-memory transport queue overflows, or the transport reports
write failures, the edge runner rolls deltas onto this spool.  When
the transport recovers and the in-memory queue is empty, the runner
drains the spool in FIFO order before resuming the fast path.

Implementation notes:
  * SQLite is the storage layer — tiny dependency, robust, durable
    across crashes, naturally ordered by autoincrement rowid.
  * Bounded by total row count.  On overflow the oldest row is
    evicted (FIFO), and a dropped-counter increments.  No row count
    on disk explicitly; a periodic count keeps it cheap.
  * Each row stores the JSON-encoded Delta envelope, opaque to the
    spool itself.  Decoding/encoding is the caller's responsibility.
  * Concurrency: a single sqlite connection per `Spool`, used from
    one thread (the edge runner's transport-drain loop).  Not
    thread-safe by itself — the runner serialises access.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

log = logging.getLogger("skywatch.edge.spool")

# Default size cap.  10k deltas is enough to ride out a several-minute
# central outage at typical traffic; larger caps are easy to opt into.
DEFAULT_MAX_ROWS = 10_000


class Spool:
    """Bounded FIFO delta buffer backed by sqlite.

    Public surface mirrors a queue:
      * `enqueue(delta_dict)` — append (FIFO drop on overflow)
      * `peek_batch(n)`       — read up to n oldest rows without removing
      * `pop_to(rowid)`       — delete every row up to and including rowid
      * `count()`             — current row count
      * `dropped`             — total deltas dropped to overflow

    The drain pattern is: peek_batch → ship to transport → pop_to(highest_rowid).
    """

    def __init__(self, path: str | os.PathLike, max_rows: int = DEFAULT_MAX_ROWS):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_rows = max_rows
        self.dropped = 0
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)
        # WAL mode keeps writes fast and reads concurrent.  Synchronous
        # NORMAL is durable across process crashes (matters for our use)
        # but not across power loss — acceptable for an edge spool.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS deltas (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   payload TEXT NOT NULL
               )"""
        )

    # -- lifecycle ----------------------------------------------------

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    # -- inspection ---------------------------------------------------

    def count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) FROM deltas")
        (n,) = cur.fetchone()
        return int(n)

    # -- write --------------------------------------------------------

    def enqueue(self, delta_dict: dict) -> None:
        """Append a delta.  Evicts the oldest row(s) on overflow."""
        payload = json.dumps(delta_dict, separators=(",", ":"))
        self._conn.execute("INSERT INTO deltas(payload) VALUES (?)", (payload,))
        # Cheap bound check — only count when we might be near the cap.
        # (sqlite COUNT(*) is fast on small tables but worth skipping
        # in the steady state.)
        if self.count() > self.max_rows:
            overflow = self.count() - self.max_rows
            self._conn.execute(
                "DELETE FROM deltas WHERE id IN ("
                "    SELECT id FROM deltas ORDER BY id ASC LIMIT ?"
                ")", (overflow,),
            )
            self.dropped += overflow

    # -- read / drain -------------------------------------------------

    def peek_batch(self, n: int) -> list[tuple[int, dict]]:
        """Return up to n oldest rows as (rowid, delta_dict)."""
        cur = self._conn.execute(
            "SELECT id, payload FROM deltas ORDER BY id ASC LIMIT ?", (n,),
        )
        out: list[tuple[int, dict]] = []
        for rowid, payload in cur.fetchall():
            try:
                out.append((int(rowid), json.loads(payload)))
            except (ValueError, TypeError, json.JSONDecodeError):
                # Corrupt row — drop it.
                self._conn.execute("DELETE FROM deltas WHERE id = ?", (rowid,))
                log.warning("dropped corrupt spool row %d", rowid)
        return out

    def pop_to(self, rowid: int) -> int:
        """Delete every row with id <= rowid.  Returns deleted count."""
        cur = self._conn.execute(
            "DELETE FROM deltas WHERE id <= ?", (rowid,),
        )
        return cur.rowcount or 0
