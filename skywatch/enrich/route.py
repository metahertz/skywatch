"""Callsign → route resolver, backed by the public adsbdb.com API.

Responsibilities:
  * Looks up `origin` / `destination` airports (and the operating airline)
    for a given callsign on a background worker thread.
  * Caches results in-memory for the lifetime of the process: positive
    hits for ~24h, negative results for ~10 minutes.  This keeps the
    third-party API cost low and avoids leaking the same callsign over
    and over.
  * Off by default — enabled at runtime via `set_enabled(True)`.  When
    disabled, `request()` is a no-op (we don't fan out to the network).

Privacy note: every lookup transmits the callsign to api.adsbdb.com.
The toggle exists so operators can keep that opt-in.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from queue import Queue
from typing import Callable

log = logging.getLogger("skywatch.route")

API_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"
USER_AGENT = "skywatch/0.1 (callsign route enrichment)"

# Be a polite client of a free public API.
_TIMEOUT_S = 5.0
_MIN_INTERVAL_S = 0.25       # rate-limit ourselves to ~4 req/s
_POS_TTL_S = 24 * 3600        # cache hits for a day
_NEG_TTL_S = 10 * 60          # back off failed lookups for 10 minutes


class RouteResolver:
    """Background callsign → route lookup with in-memory caching."""

    def __init__(
        self,
        on_route: Callable[[str, dict | None], None],
        enabled: bool = False,
    ) -> None:
        self._on_route = on_route
        self._enabled = bool(enabled)
        # callsign -> (expires_at, route_or_None)
        self._cache: dict[str, tuple[float, dict | None]] = {}
        self._inflight: set[str] = set()
        self._queue: Queue = Queue()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_req_at = 0.0
        self._worker: threading.Thread | None = None

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        self._worker = threading.Thread(
            target=self._run, daemon=True, name="skywatch-route",
        )
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        # Unblock the worker
        self._queue.put(None)

    # -- toggle --------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    # -- request -------------------------------------------------------

    def request(self, callsign: str | None) -> None:
        """Schedule a route lookup for `callsign`.

        Idempotent.  If the callsign is already cached and still fresh,
        a positive result is replayed via `on_route` so a fresh aircraft
        entry can pick it up without a network round-trip.
        """
        if not callsign:
            return
        cs = callsign.strip().upper()
        if not cs:
            return
        replay: dict | None = None
        with self._lock:
            entry = self._cache.get(cs)
            if entry and entry[0] > time.time():
                # Cache hit (positive or negative).  Replay positives so
                # a newly-spawned aircraft with the same callsign gets
                # the existing data immediately.
                if entry[1] is not None:
                    replay = entry[1]
                if replay is None:
                    return
            else:
                if cs in self._inflight or not self._enabled:
                    return
                self._inflight.add(cs)
                self._queue.put(cs)
        if replay is not None:
            self._on_route(cs, replay)

    # -- worker --------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            cs = self._queue.get()
            if cs is None:
                return
            try:
                self._do_lookup(cs)
            except Exception:
                log.exception("route lookup crashed for %s", cs)
            finally:
                with self._lock:
                    self._inflight.discard(cs)

    def _do_lookup(self, cs: str) -> None:
        # Polite throttle between successive outbound requests.
        wait = (self._last_req_at + _MIN_INTERVAL_S) - time.time()
        if wait > 0:
            self._stop.wait(wait)
            if self._stop.is_set():
                return
        self._last_req_at = time.time()

        url = API_URL.format(callsign=cs)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        route: dict | None = None
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
                if 200 <= r.status < 300:
                    payload = json.loads(r.read().decode("utf-8"))
                    route = self._parse(payload)
                else:
                    log.debug("adsbdb %s -> HTTP %d", cs, r.status)
        except urllib.error.HTTPError as e:
            # 404 is the common "unknown callsign" signal — cache it
            # negatively so we don't keep asking.
            if e.code != 404:
                log.debug("adsbdb %s HTTPError %s", cs, e)
        except (urllib.error.URLError, socket.timeout, OSError, ValueError) as e:
            log.debug("adsbdb %s error: %s", cs, e)

        ttl = _POS_TTL_S if route else _NEG_TTL_S
        with self._lock:
            self._cache[cs] = (time.time() + ttl, route)

        if route:
            self._on_route(cs, route)

    @staticmethod
    def _parse(payload: dict) -> dict | None:
        try:
            fr = payload["response"]["flightroute"]
        except (KeyError, TypeError):
            return None
        if not isinstance(fr, dict):
            return None

        def _airport(a: dict | None) -> dict | None:
            if not isinstance(a, dict):
                return None
            return {
                "iata": a.get("iata_code"),
                "icao": a.get("icao_code"),
                "name": a.get("name"),
                "municipality": a.get("municipality"),
                "country": a.get("country_iso_name") or a.get("country_name"),
                "lat": a.get("latitude"),
                "lon": a.get("longitude"),
            }

        airline = fr.get("airline")
        return {
            "callsign_icao": fr.get("callsign_icao"),
            "callsign_iata": fr.get("callsign_iata"),
            "airline": (airline or {}).get("name") if isinstance(airline, dict) else None,
            "airline_iata": (airline or {}).get("iata") if isinstance(airline, dict) else None,
            "origin": _airport(fr.get("origin")),
            "destination": _airport(fr.get("destination")),
            "source": "adsbdb",
        }
