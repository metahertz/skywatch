"""MongoDB-backed transport: edges write to `state_deltas`, central tails
that collection's change stream.

This is one of two transport implementations behind the Transport ABC
in `skywatch.transport`.  Selected at startup via
`--transport mongo`; mutually exclusive with the WebSocket transport.

Wire shape: `state_deltas` is a regular collection (NOT a time-series
collection — change streams on TS collections have stricter semantics
in older Mongo versions, and we don't need columnar compression for
what is essentially a transient event log).  Documents are short-lived
TTL'd; the long-term archive lives in `aircraft_state` / `events` / etc.
which the central node writes from the merged view.

Resume tokens are persisted in the `_meta` collection so a restarted
central picks up exactly where it left off, with no re-broadcast of
already-processed deltas.

Requirements:
  * Mongo deployment must be a replica set (single-node RS is fine for
    dev and small production).  Standalone Mongo will reject `.watch()`.
"""
from __future__ import annotations

import datetime as _dt
import logging
import queue
import threading
import time

from . import Delta, DeltaCallback, Transport

log = logging.getLogger("skywatch.transport.mongo")

_DEFAULT_DB = "skywatch"
_DELTAS_COLL = "state_deltas"
_META_COLL = "_meta"
_RESUME_TOKEN_KEY = "central_resume_token"

# Hot-path queue cap for the edge sender.
_SEND_QUEUE_MAX = 5000
_SEND_BATCH = 200
_SEND_FLUSH_INTERVAL_S = 0.25       # tighter than MongoStore for low latency

# TTL for state_deltas — they're transient pubsub messages, not history.
_DELTAS_TTL_S = 600


class MongoChangeStreamTransport(Transport):
    """Bidirectional transport.  Edge mode uses `send`; central mode
    uses `subscribe`.  Either side may also be used in the same process
    (e.g. for in-process tests)."""

    def __init__(
        self,
        uri: str,
        db_name: str = _DEFAULT_DB,
        send_queue_max: int = _SEND_QUEUE_MAX,
        deltas_ttl_s: int = _DELTAS_TTL_S,
    ):
        self.uri = uri
        self.db_name = db_name
        self.send_queue_max = send_queue_max
        self.deltas_ttl_s = deltas_ttl_s

        # pymongo handles, populated by start()
        self._client = None
        self._db = None

        # Edge sender state
        self._send_q: queue.Queue = queue.Queue(maxsize=send_queue_max)
        self._sender_thread: threading.Thread | None = None

        # Central subscriber state
        self._subs: list[DeltaCallback] = []
        self._watcher_thread: threading.Thread | None = None

        self._stop = threading.Event()
        self.sent_total = 0
        self.send_dropped_queue = 0
        self.received_total = 0

    # -- lifecycle ----------------------------------------------------

    def start(self) -> None:
        import pymongo
        self._client = pymongo.MongoClient(
            self.uri, serverSelectionTimeoutMS=3000,
        )
        # Confirm replica-set + reachability up-front.
        info = self._client.admin.command("hello")
        if not info.get("setName"):
            raise RuntimeError(
                "MongoDB at %s is not part of a replica set; "
                "change streams require a replica set (single-node RS is "
                "fine).  Run mongod with --replSet and rs.initiate() once."
                % self.uri,
            )
        self._db = self._client[self.db_name]
        self._ensure_collections()
        # Spin up the edge sender thread.  Cheap if no one calls send().
        self._sender_thread = threading.Thread(
            target=self._sender_loop,
            daemon=True,
            name="skywatch-mongo-tx-sender",
        )
        self._sender_thread.start()
        log.info("MongoChangeStreamTransport connected to %s/%s",
                 self.uri, self.db_name)

    def stop(self) -> None:
        self._stop.set()
        # Unblock the sender's queue.get
        try:
            self._send_q.put_nowait(None)
        except queue.Full:
            pass
        for t in (self._sender_thread, self._watcher_thread):
            if t is not None:
                t.join(timeout=2.0)
        if self._client is not None:
            self._client.close()

    @property
    def stats(self) -> dict:
        return {
            "sent_total": self.sent_total,
            "send_dropped_queue": self.send_dropped_queue,
            "received_total": self.received_total,
            "send_queue_depth": self._send_q.qsize(),
        }

    # -- schema -------------------------------------------------------

    def _ensure_collections(self) -> None:
        existing = set(self._db.list_collection_names())
        if _DELTAS_COLL not in existing:
            self._db.create_collection(_DELTAS_COLL)
        # TTL index on `ts` (BSON Date mirror).
        self._db[_DELTAS_COLL].create_index(
            "ts_date",
            name="ttl",
            expireAfterSeconds=self.deltas_ttl_s,
        )
        if _META_COLL not in existing:
            self._db.create_collection(_META_COLL)

    # -- edge: send ---------------------------------------------------

    def send(self, delta: Delta) -> bool:
        try:
            self._send_q.put_nowait(delta)
        except queue.Full:
            self.send_dropped_queue += 1
            return False
        return True

    def _sender_loop(self) -> None:
        from pymongo.errors import PyMongoError
        batch: list[dict] = []
        last_flush = time.time()
        while not self._stop.is_set():
            try:
                timeout = max(0.05, _SEND_FLUSH_INTERVAL_S -
                              (time.time() - last_flush))
                item = self._send_q.get(timeout=timeout)
            except queue.Empty:
                item = None
            if item is None and self._stop.is_set():
                break
            if isinstance(item, Delta):
                doc = self._delta_to_doc(item)
                batch.append(doc)
            full = len(batch) >= _SEND_BATCH
            timed = batch and (time.time() - last_flush >= _SEND_FLUSH_INTERVAL_S)
            if full or timed:
                try:
                    self._db[_DELTAS_COLL].insert_many(batch, ordered=False)
                    self.sent_total += len(batch)
                except PyMongoError as e:
                    # On Mongo failure, the runner's spool path will
                    # absorb the deltas — but we still need to drop
                    # this batch so the queue doesn't wedge.
                    log.warning("delta batch insert failed (%d docs): %s",
                                len(batch), e)
                batch = []
                last_flush = time.time()
        # Final drain
        try:
            while True:
                item = self._send_q.get_nowait()
                if isinstance(item, Delta):
                    batch.append(self._delta_to_doc(item))
        except queue.Empty:
            pass
        if batch:
            try:
                self._db[_DELTAS_COLL].insert_many(batch, ordered=False)
                self.sent_total += len(batch)
            except Exception:
                pass

    @staticmethod
    def _delta_to_doc(delta: Delta) -> dict:
        return {
            **delta.to_dict(),
            "ts_date": _dt.datetime.fromtimestamp(delta.ts, tz=_dt.timezone.utc),
        }

    # -- central: subscribe ------------------------------------------

    def subscribe(self, callback: DeltaCallback) -> None:
        self._subs.append(callback)
        if self._watcher_thread is None:
            self._watcher_thread = threading.Thread(
                target=self._watcher_loop,
                daemon=True,
                name="skywatch-mongo-tx-watcher",
            )
            self._watcher_thread.start()

    def _watcher_loop(self) -> None:
        from pymongo.errors import PyMongoError
        # Resume from any token we've persisted so we don't replay.
        token = self._load_resume_token()
        while not self._stop.is_set():
            try:
                pipeline = [{"$match": {"operationType": "insert"}}]
                kwargs = {"max_await_time_ms": 500}
                if token is not None:
                    kwargs["resume_after"] = token
                with self._db[_DELTAS_COLL].watch(pipeline, **kwargs) as stream:
                    for change in stream:
                        if self._stop.is_set():
                            break
                        doc = change.get("fullDocument") or {}
                        try:
                            delta = Delta.from_dict(doc)
                        except Exception as e:
                            log.warning("malformed delta in stream: %s", e)
                            continue
                        self.received_total += 1
                        for cb in self._subs:
                            try:
                                cb(delta)
                            except Exception:
                                log.exception("delta callback raised")
                        token = change.get("_id")
                        if token is not None:
                            self._save_resume_token(token)
            except PyMongoError as e:
                if self._stop.is_set():
                    return
                log.warning("change stream interrupted: %s — reconnecting",
                            e)
                time.sleep(1.0)
            except Exception:
                if self._stop.is_set():
                    return
                log.exception("change stream watcher crashed; restarting")
                time.sleep(1.0)

    def _load_resume_token(self):
        try:
            doc = self._db[_META_COLL].find_one({"_id": _RESUME_TOKEN_KEY})
            return doc.get("token") if doc else None
        except Exception:
            return None

    def _save_resume_token(self, token) -> None:
        try:
            self._db[_META_COLL].replace_one(
                {"_id": _RESUME_TOKEN_KEY},
                {"_id": _RESUME_TOKEN_KEY, "token": token},
                upsert=True,
            )
        except Exception:
            log.debug("resume token save failed", exc_info=True)
