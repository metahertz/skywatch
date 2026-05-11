"""MongoDB-backed persistence for skywatch.

Schema rationale (per the mongodb-schema-design skill):

  * `receivers`        — small registry, embedded everything, _id = receiver_id.
  * `aircraft_state`   — hot working set (≤ 1000 active), upserted on update;
                         trail (≤120 points) and by_receiver map (≤10 keys)
                         are bounded so they stay embedded.  _id = ICAO.
  * `frames`           — time-series, timeField=ts, metaField=receiver_id
                         (low-cardinality, queried alongside time range).
                         icao is a regular field, indexed separately for
                         per-aircraft replay.  TTL configurable.
  * `events`           — regular collection with TTL on `t` and indexes on
                         (t desc) + (icao, t desc) for ticker and per-aircraft
                         history.
  * `receiver_metrics` — time-series, metaField=receiver_id, ~1/s/receiver.

Write paths:
  * Hot path  — frame archive.  Non-blocking enqueue to a background
                writer thread that batches with insert_many; queue
                back-pressure drops to a dropped-counter rather than
                blocking the engine.
  * Cold path — aircraft_state upserts, event log, receiver upserts.
                These are infrequent enough to write synchronously
                from the broadcaster thread.

The store gracefully degrades: every public method tolerates pymongo
errors (logs and continues) so a flaky DB never takes the engine down.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

import pymongo
from pymongo.errors import (
    CollectionInvalid, ConnectionFailure, OperationFailure, PyMongoError,
)

log = logging.getLogger("skywatch.store")


_DEFAULT_DB = "skywatch"
_FRAMES_TTL_S = 24 * 3600          # 1 day of raw frames by default
_METRICS_TTL_S = 7 * 24 * 3600     # 7 days of receiver health
_EVENTS_TTL_S = 30 * 24 * 3600     # 30 days of events
_COMMS_TTL_S = 30 * 24 * 3600      # 30 days of VDL2/CPDLC/ACARS messages
_FRAME_QUEUE_MAX = 5000            # hot-path queue depth before drops
_FRAME_FLUSH_BATCH = 200           # max docs per insert_many
_FRAME_FLUSH_INTERVAL_S = 1.0      # flush at least this often
_COMMS_QUEUE_MAX = 2000            # comms volume is much lower than frames


class MongoStore:
    """All MongoDB persistence in one place.  Construct once at startup;
    call `start()` to spin up the background writer."""

    def __init__(self, uri: str, db_name: str = _DEFAULT_DB,
                 frame_ttl_s: int = _FRAMES_TTL_S,
                 metrics_ttl_s: int = _METRICS_TTL_S,
                 events_ttl_s: int = _EVENTS_TTL_S,
                 comms_ttl_s: int = _COMMS_TTL_S) -> None:
        self.uri = uri
        self.db_name = db_name
        self._frame_ttl_s = frame_ttl_s
        self._metrics_ttl_s = metrics_ttl_s
        self._events_ttl_s = events_ttl_s
        self._comms_ttl_s = comms_ttl_s

        self._client: pymongo.MongoClient | None = None
        self._db = None

        # Hot-path queue + background writer (frames archive)
        self._frame_q: queue.Queue = queue.Queue(maxsize=_FRAME_QUEUE_MAX)
        # Parallel queue for VDL2/CPDLC/ACARS messages.  Lower volume
        # than frames but worth its own queue + writer so a flood of
        # 1090 frames can't starve comms persistence.
        self._comms_q: queue.Queue = queue.Queue(maxsize=_COMMS_QUEUE_MAX)
        self._stop = threading.Event()
        self._writer: threading.Thread | None = None
        self._comms_writer: threading.Thread | None = None

        # Counters exposed for stats/debug
        self.frames_written = 0
        self.frames_dropped_queue = 0
        self.frames_dropped_error = 0
        self.comms_written = 0
        self.comms_dropped_queue = 0
        self.comms_dropped_error = 0

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Connect, ensure schema, and spin up the background writer."""
        self._client = pymongo.MongoClient(
            self.uri, serverSelectionTimeoutMS=3000,
        )
        # Force a server-selection round-trip so connection failures
        # surface here, not on the first write.
        self._client.admin.command("ping")
        self._db = self._client[self.db_name]
        self._ensure_collections()
        self._writer = threading.Thread(
            target=self._writer_loop, daemon=True, name="skywatch-mongo-writer",
        )
        self._writer.start()
        self._comms_writer = threading.Thread(
            target=self._comms_writer_loop, daemon=True,
            name="skywatch-mongo-comms-writer",
        )
        self._comms_writer.start()
        log.info("MongoStore connected to %s/%s", self.uri, self.db_name)

    def stop(self) -> None:
        self._stop.set()
        # Unblock the writer queues.
        for q in (self._frame_q, self._comms_q):
            try:
                q.put_nowait(None)
            except queue.Full:
                pass
        for t in (self._writer, self._comms_writer):
            if t is not None:
                t.join(timeout=2.0)
        if self._client is not None:
            self._client.close()

    # -- schema --------------------------------------------------------

    def _ensure_collections(self) -> None:
        """Idempotently create collections + indexes.  Safe to call on
        every startup."""
        existing = set(self._db.list_collection_names())

        # frames (time-series)
        if "frames" not in existing:
            try:
                self._db.create_collection(
                    "frames",
                    timeseries={
                        "timeField": "ts",
                        "metaField": "receiver_id",
                        "granularity": "seconds",
                    },
                    expireAfterSeconds=self._frame_ttl_s,
                )
            except CollectionInvalid:
                pass
        # Per-icao replay index — auto index covers (receiver_id, ts)
        # already; this adds the icao-time path.
        self._db.frames.create_index([("icao", 1), ("ts", -1)],
                                     name="icao_ts")

        # receiver_metrics (time-series)
        if "receiver_metrics" not in existing:
            try:
                self._db.create_collection(
                    "receiver_metrics",
                    timeseries={
                        "timeField": "ts",
                        "metaField": "receiver_id",
                        "granularity": "seconds",
                    },
                    expireAfterSeconds=self._metrics_ttl_s,
                )
            except CollectionInvalid:
                pass

        # comms (time-series).  VDL2 / CPDLC / ACARS message archive,
        # one doc per VdlFrame.  Same metaField pattern as `frames`
        # so per-receiver queries stay fast.
        if "comms" not in existing:
            try:
                self._db.create_collection(
                    "comms",
                    timeseries={
                        "timeField": "ts",
                        "metaField": "receiver_id",
                        "granularity": "seconds",
                    },
                    expireAfterSeconds=self._comms_ttl_s,
                )
            except CollectionInvalid:
                pass
        # Per-aircraft comms replay.
        self._db.comms.create_index([("aircraft_icao", 1), ("ts", -1)],
                                    name="aircraft_ts")
        # Per-kind filter for "show me all CPDLC" pages.
        self._db.comms.create_index([("kind", 1), ("ts", -1)],
                                    name="kind_ts")

        # events (regular, TTL by `t`)
        if "events" not in existing:
            self._db.create_collection("events")
        self._db.events.create_index([("t", -1)], name="t_desc")
        self._db.events.create_index([("icao", 1), ("t", -1)],
                                     name="icao_t_desc")
        self._db.events.create_index([("type", 1), ("t", -1)],
                                     name="type_t_desc")
        # TTL: `t` is a unix epoch float for skywatch; pymongo expects a
        # BSON Date for TTL.  We mirror the unix `t` into `ts_date` so
        # the TTL index can use it.
        self._db.events.create_index(
            "ts_date", name="ttl",
            expireAfterSeconds=self._events_ttl_s,
        )

        # aircraft_state (regular, _id = ICAO)
        if "aircraft_state" not in existing:
            self._db.create_collection("aircraft_state")
        self._db.aircraft_state.create_index([("last_seen", 1)],
                                             name="last_seen")

        # receivers (regular, _id = receiver_id)
        if "receivers" not in existing:
            self._db.create_collection("receivers")

    # -- hot path: frames ---------------------------------------------

    def enqueue_frame(self, frame, icao: str | None) -> None:
        """Non-blocking enqueue of a single frame.  Drops on overflow."""
        doc = {
            "ts": _to_bson_date(time.time()),
            "receiver_id": frame.receiver_id,
            "icao": icao,
            "df": frame.df,
            "rssi": frame.rssi_dbfs,
            "raw_hex": frame.raw_hex,
        }
        try:
            self._frame_q.put_nowait(doc)
        except queue.Full:
            self.frames_dropped_queue += 1

    def _writer_loop(self) -> None:
        """Background writer.  Drains the frame queue in batches, with
        a periodic timeout so the buffer doesn't sit forever during low
        traffic."""
        batch: list[dict] = []
        last_flush = time.time()
        while not self._stop.is_set():
            try:
                # Wait up to flush-interval for the next item.
                timeout = max(0.05, _FRAME_FLUSH_INTERVAL_S -
                              (time.time() - last_flush))
                doc = self._frame_q.get(timeout=timeout)
            except queue.Empty:
                doc = None
            if doc is None and self._stop.is_set():
                break
            if doc is not None:
                batch.append(doc)
            # Flush when the batch is full or the interval has elapsed.
            should_flush = (len(batch) >= _FRAME_FLUSH_BATCH or
                            (batch and time.time() - last_flush >= _FRAME_FLUSH_INTERVAL_S))
            if should_flush:
                self._flush_frames(batch)
                batch = []
                last_flush = time.time()
        # Final drain on shutdown.
        try:
            while True:
                batch.append(self._frame_q.get_nowait())
        except queue.Empty:
            pass
        if batch:
            self._flush_frames(batch)

    def _flush_frames(self, batch: list[dict]) -> None:
        if not batch:
            return
        try:
            self._db.frames.insert_many(batch, ordered=False)
            self.frames_written += len(batch)
        except PyMongoError as e:
            self.frames_dropped_error += len(batch)
            log.warning("frame batch insert failed (%d docs): %s",
                        len(batch), e)

    # -- hot path: VDL2 / CPDLC / ACARS comms -------------------------

    def enqueue_comms(self, vdl_frame) -> None:
        """Non-blocking enqueue of one VdlFrame.  Drops on overflow."""
        doc = {
            "ts": _to_bson_date(vdl_frame.ts),
            "receiver_id": vdl_frame.receiver_id,
            "aircraft_icao": vdl_frame.aircraft_icao,
            "src_icao": vdl_frame.src_icao,
            "dst_icao": vdl_frame.dst_icao,
            "direction": vdl_frame.direction,
            "kind": vdl_frame.kind,
            "label": vdl_frame.label,
            "text": vdl_frame.text,
            "flight": vdl_frame.flight,
            "reg": vdl_frame.reg,
            "raw": vdl_frame.raw,
        }
        try:
            self._comms_q.put_nowait(doc)
        except queue.Full:
            self.comms_dropped_queue += 1

    def _comms_writer_loop(self) -> None:
        """Mirror of _writer_loop for the comms time-series collection."""
        batch: list[dict] = []
        last_flush = time.time()
        while not self._stop.is_set():
            try:
                timeout = max(0.05, _FRAME_FLUSH_INTERVAL_S -
                              (time.time() - last_flush))
                doc = self._comms_q.get(timeout=timeout)
            except queue.Empty:
                doc = None
            if doc is None and self._stop.is_set():
                break
            if doc is not None:
                batch.append(doc)
            should_flush = (len(batch) >= _FRAME_FLUSH_BATCH or
                            (batch and time.time() - last_flush >= _FRAME_FLUSH_INTERVAL_S))
            if should_flush:
                self._flush_comms(batch)
                batch = []
                last_flush = time.time()
        # Final drain
        try:
            while True:
                batch.append(self._comms_q.get_nowait())
        except queue.Empty:
            pass
        if batch:
            self._flush_comms(batch)

    def _flush_comms(self, batch: list[dict]) -> None:
        if not batch:
            return
        try:
            self._db.comms.insert_many(batch, ordered=False)
            self.comms_written += len(batch)
        except PyMongoError as e:
            self.comms_dropped_error += len(batch)
            log.warning("comms batch insert failed (%d docs): %s",
                        len(batch), e)

    # -- cold path: state, events, receivers --------------------------

    def upsert_aircraft(self, payload: dict) -> None:
        """Upsert the live aircraft state.  Trail and by_receiver are
        embedded; the `$slice` modifier on trail is applied client-side
        before send (the whole payload is the latest snapshot)."""
        if not payload.get("icao"):
            return
        try:
            self._db.aircraft_state.replace_one(
                {"_id": payload["icao"]},
                {**payload, "_id": payload["icao"]},
                upsert=True,
            )
        except PyMongoError as e:
            log.debug("aircraft_state upsert failed: %s", e)

    def delete_aircraft(self, icao: str) -> None:
        """Called when an aircraft is pruned from the engine."""
        try:
            self._db.aircraft_state.delete_one({"_id": icao})
        except PyMongoError as e:
            log.debug("aircraft_state delete failed: %s", e)

    def upsert_receiver(self, receiver: dict) -> None:
        rid = receiver.get("id")
        if not rid:
            return
        try:
            self._db.receivers.replace_one(
                {"_id": rid},
                {**receiver, "_id": rid},
                upsert=True,
            )
        except PyMongoError as e:
            log.debug("receiver upsert failed: %s", e)

    def log_event(self, event: dict) -> None:
        """Append an event (TCAS RA, intent_change, emergency, ...).
        `t` is preserved as the original unix-epoch float; `ts_date`
        is the TTL-friendly BSON date mirror."""
        doc = {**event}
        if "t" in doc:
            doc["ts_date"] = _to_bson_date(doc["t"])
        else:
            doc["ts_date"] = _to_bson_date(time.time())
        try:
            self._db.events.insert_one(doc)
        except PyMongoError as e:
            log.debug("event insert failed: %s", e)

    def record_receiver_metrics(self, receiver_id: str, metrics: dict) -> None:
        """One sample of per-receiver health; goes to the time-series
        collection."""
        doc = {
            "ts": _to_bson_date(time.time()),
            "receiver_id": receiver_id,
            **metrics,
        }
        try:
            self._db.receiver_metrics.insert_one(doc)
        except PyMongoError as e:
            log.debug("receiver_metrics insert failed: %s", e)

    # -- cold path: load on startup -----------------------------------

    def load_receivers(self) -> list[dict]:
        try:
            return [doc for doc in self._db.receivers.find({})]
        except PyMongoError as e:
            log.warning("receivers load failed: %s", e)
            return []

    def load_aircraft_state(self, max_age_s: float = 600) -> list[dict]:
        """Return any aircraft seen within the last `max_age_s` seconds.
        Older docs are skipped — they're effectively stale on restart."""
        cutoff = time.time() - max_age_s
        try:
            return list(self._db.aircraft_state.find(
                {"last_seen": {"$gte": cutoff}}
            ))
        except PyMongoError as e:
            log.warning("aircraft_state load failed: %s", e)
            return []

    def load_recent_events(self, n: int = 200) -> list[dict]:
        try:
            cursor = self._db.events.find({}).sort("t", -1).limit(n)
            # Reverse so the caller can iterate oldest-first.
            return list(reversed(list(cursor)))
        except PyMongoError as e:
            log.warning("events load failed: %s", e)
            return []


def _to_bson_date(epoch_s: float):
    """Convert a unix-epoch float to a python datetime, which pymongo
    serialises as a BSON Date."""
    import datetime as _dt
    return _dt.datetime.fromtimestamp(epoch_s, tz=_dt.timezone.utc)
