"""Upstox V3 market-data feed over WebSocket.

The desks used to learn every price by polling the REST quote endpoint on a
fixed clock. This module keeps a live last-traded-price cache fed by the Upstox
streaming socket, so a desk reads the latest tick instead of waiting for the
next poll. The win is on exits: a stop or trailing level is now checked against
the tick that just arrived, not one up to five seconds old.

The strategy never imports this module. It only changes *where* a price comes
from: a desk asks :func:`MarketFeed.last_price` first and falls back to the same
REST quote it always used when there is no fresh tick. So if the socket is down,
stale, or the SDK is not installed, the desk behaves exactly as it did before.

Two consumers, two entry points:

* The instance loop (``desk_loop``) runs in the same process as the socket and
  reads the in-memory cache through :meth:`MarketFeed.last_price`.
* The Streamlit pages run in a *different* process. They must not open a second
  socket (Upstox allows only a small number of market-data connections), so they
  read the price the loop writes to ``data/feed_snapshot.json`` through the
  module-level :func:`read_snapshot_price`.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from config.settings import PROJECT_ROOT

LOGGER = logging.getLogger("desk.feed")

# Written under static/ (not data/) so the Streamlit static server can expose it
# to the browser at /app/static/feed_snapshot.json for the live tile component.
SNAPSHOT_PATH = PROJECT_ROOT / "static" / "feed_snapshot.json"
# A tick older than this many seconds is treated as missing, so the caller
# falls back to REST rather than trading on a frozen price.
FRESH_SECONDS = 4.0
# The snapshot file is for the UI; rewriting it on every tick would thrash the
# disk, so writes are throttled to at most once per this interval.
SNAPSHOT_WRITE_SECONDS = 1.0
# ltpc is the lightest mode: last trade price, time, quantity and prior close.
FEED_MODE = "ltpc"


def _dig_ltp(obj: Any) -> float | None:
    """Find the nearest last-traded-price inside a decoded feed value.

    The V3 message shape differs by mode (``{"ltpc": {"ltp": ...}}`` in ltpc
    mode, nested under ``fullFeed``/``indexFF``/``marketFF`` in full mode), so
    this walks the structure and returns the first positive ``ltp`` it finds.
    """

    if isinstance(obj, dict):
        ltpc = obj.get("ltpc")
        if isinstance(ltpc, dict) and "ltp" in ltpc:
            found = _coerce_price(ltpc.get("ltp"))
            if found is not None:
                return found
        if "ltp" in obj:
            found = _coerce_price(obj.get("ltp"))
            if found is not None:
                return found
        for value in obj.values():
            found = _dig_ltp(value)
            if found is not None:
                return found
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            found = _dig_ltp(value)
            if found is not None:
                return found
    return None


def _coerce_price(raw: Any) -> float | None:
    try:
        price = float(raw)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def extract_prices(message: Any) -> dict[str, float]:
    """Map instrument key -> last price from one decoded feed message."""

    if not isinstance(message, dict):
        return {}
    feeds = message.get("feeds")
    if not isinstance(feeds, dict):
        feeds = message.get("feed") if isinstance(message.get("feed"), dict) else {}
    prices: dict[str, float] = {}
    for key, feed in feeds.items():
        ltp = _dig_ltp(feed)
        if ltp is not None:
            prices[str(key)] = ltp
    return prices


class MarketFeed:
    """A background WebSocket that keeps a fresh last-price cache.

    The cache holds a monotonic timestamp per key so :meth:`last_price` can tell
    a live tick from a stale one within this process. The snapshot file carries a
    wall-clock timestamp instead, because the reader is a separate process.
    """

    def __init__(
        self,
        access_token: str,
        snapshot_path: Path = SNAPSHOT_PATH,
        fresh_seconds: float = FRESH_SECONDS,
    ) -> None:
        self._token = access_token
        self._snapshot_path = snapshot_path
        self._fresh = fresh_seconds
        self._prices: dict[str, tuple[float, float]] = {}
        self._desired: set[str] = set()
        self._lock = threading.RLock()
        self._streamer: Any = None
        self._connected = False
        self._started = False
        self._last_write = 0.0

    # -- lifecycle -----------------------------------------------------------
    def start(self, keys: Iterable[str] = ()) -> bool:
        """Open the socket once and subscribe the initial keys.

        Returns ``False`` (and stays dormant, so every read falls back to REST)
        when the token is empty or the SDK is not installed. Never raises.
        """

        with self._lock:
            self._desired.update(k for k in keys if k)
            if self._started:
                return self._streamer is not None
            self._started = True
            if not self._token:
                LOGGER.info("Feed not started: no access token. REST fallback stays in use.")
                return False
            try:
                import upstox_client  # imported lazily so a missing SDK never breaks the app
            except Exception as exc:  # pragma: no cover - import guard
                LOGGER.warning("upstox-python-sdk not available (%s). REST fallback stays in use.", exc)
                return False
            try:
                configuration = upstox_client.Configuration()
                configuration.access_token = self._token
                streamer = upstox_client.MarketDataStreamerV3(
                    upstox_client.ApiClient(configuration)
                )
                streamer.on("open", self._on_open)
                streamer.on("message", self._on_message)
                streamer.on("close", self._on_close)
                streamer.on("error", self._on_error)
                try:
                    streamer.auto_reconnect(True, 5, 10)
                except Exception:  # pragma: no cover - older SDKs may differ
                    LOGGER.debug("auto_reconnect unavailable on this SDK build")
                self._streamer = streamer
            except Exception:
                LOGGER.exception("Failed to build the market-data streamer; REST fallback stays in use.")
                self._streamer = None
                return False

        # connect() runs the socket loop; keep it off the caller's thread.
        thread = threading.Thread(target=self._run, name="upstox-feed", daemon=True)
        thread.start()
        return True

    def _run(self) -> None:
        try:
            self._streamer.connect()
        except Exception:
            LOGGER.exception("Market-data socket stopped; REST fallback stays in use.")
            with self._lock:
                self._connected = False

    # -- subscription --------------------------------------------------------
    def subscribe(self, keys: Iterable[str]) -> None:
        """Add instrument keys to the live feed (safe to call repeatedly)."""

        fresh = {k for k in keys if k}
        if not fresh:
            return
        with self._lock:
            new = fresh - self._desired
            self._desired.update(fresh)
            streamer = self._streamer
            connected = self._connected
        if streamer is not None and connected and new:
            try:
                streamer.subscribe(sorted(new), FEED_MODE)
            except Exception:
                LOGGER.exception("Subscribe failed for %s", sorted(new))

    def unsubscribe(self, keys: Iterable[str]) -> None:
        drop = {k for k in keys if k}
        if not drop:
            return
        with self._lock:
            self._desired.difference_update(drop)
            streamer = self._streamer
            connected = self._connected
        if streamer is not None and connected:
            try:
                streamer.unsubscribe(sorted(drop))
            except Exception:
                LOGGER.exception("Unsubscribe failed for %s", sorted(drop))

    # -- reads ---------------------------------------------------------------
    def last_price(self, key: str, now: float | None = None) -> float | None:
        """The latest tick for ``key``, or ``None`` when there is no fresh one."""

        clock = now if now is not None else time.monotonic()
        with self._lock:
            entry = self._prices.get(key)
        if not entry:
            return None
        price, stamp = entry
        if clock - stamp > self._fresh:
            return None
        return price

    # -- SDK callbacks -------------------------------------------------------
    def _on_open(self) -> None:
        with self._lock:
            self._connected = True
            keys = sorted(self._desired)
            streamer = self._streamer
        LOGGER.info("Market-data socket open. Subscribing %d key(s).", len(keys))
        if streamer is not None and keys:
            try:
                streamer.subscribe(keys, FEED_MODE)
            except Exception:
                LOGGER.exception("Initial subscribe failed")

    def _on_message(self, message: Any) -> None:
        prices = extract_prices(message)
        if not prices:
            return
        clock = time.monotonic()
        with self._lock:
            for key, price in prices.items():
                self._prices[key] = (price, clock)
        self._maybe_write_snapshot()

    def _on_close(self, *args: Any) -> None:
        with self._lock:
            self._connected = False
        LOGGER.info("Market-data socket closed.")

    def _on_error(self, *args: Any) -> None:
        LOGGER.warning("Market-data socket error: %s", args)

    # -- snapshot for the UI process ----------------------------------------
    def _maybe_write_snapshot(self) -> None:
        clock = time.monotonic()
        with self._lock:
            if clock - self._last_write < SNAPSHOT_WRITE_SECONDS:
                return
            self._last_write = clock
            epoch = time.time()
            prices = {key: {"ltp": price, "ts": epoch} for key, (price, _) in self._prices.items()}
        payload = {"updated_at": epoch, "prices": prices}
        try:
            self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._snapshot_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            try:
                tmp.replace(self._snapshot_path)
            except OSError:
                self._snapshot_path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            LOGGER.debug("Could not write feed snapshot", exc_info=True)


# Module-level singleton used by the instance loop.
_FEED: MarketFeed | None = None
_FEED_LOCK = threading.Lock()


def get_feed(access_token: str, keys: Iterable[str] = ()) -> MarketFeed:
    """Return the process-wide feed, starting it on first call."""

    global _FEED
    with _FEED_LOCK:
        if _FEED is None:
            _FEED = MarketFeed(access_token)
            _FEED.start(keys)
        else:
            _FEED.subscribe(keys)
        return _FEED


def read_snapshot_price(
    key: str,
    path: Path = SNAPSHOT_PATH,
    max_age: float = FRESH_SECONDS,
    now: float | None = None,
) -> float | None:
    """Last price for ``key`` from the snapshot file, or ``None`` if stale/absent.

    Used by the Streamlit pages, which run in a separate process and must not
    open their own socket. Returns ``None`` on any problem so the page falls
    back to its existing REST read.
    """

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    prices = raw.get("prices") if isinstance(raw, dict) else None
    if not isinstance(prices, dict):
        return None
    entry = prices.get(key)
    if not isinstance(entry, dict):
        return None
    price = _coerce_price(entry.get("ltp"))
    if price is None:
        return None
    stamp = entry.get("ts")
    try:
        stamp = float(stamp)
    except (TypeError, ValueError):
        return None
    current = now if now is not None else time.time()
    if current - stamp > max_age:
        return None
    return price
