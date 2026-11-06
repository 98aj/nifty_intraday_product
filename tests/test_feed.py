"""Tests for the market-data feed cache and snapshot.

The feed never touches a real socket here. These cover the parts the desks
depend on: pulling a last price out of a decoded message, treating a stale tick
as missing so REST fallback engages, and the snapshot the UI process reads.
"""

from __future__ import annotations

import json
import time

from modules.upstox.feed import MarketFeed, extract_prices, read_snapshot_price

NIFTY = "NSE_INDEX|Nifty 50"
OPTION = "NSE_FO|49123"


def test_extract_prices_ltpc_mode():
    message = {"feeds": {NIFTY: {"ltpc": {"ltp": 24500.5, "cp": 24480.0}}}}
    assert extract_prices(message) == {NIFTY: 24500.5}


def test_extract_prices_full_mode_nested():
    message = {
        "feeds": {
            NIFTY: {"fullFeed": {"indexFF": {"ltpc": {"ltp": 24510.0, "cp": 24480.0}}}},
            OPTION: {"fullFeed": {"marketFF": {"ltpc": {"ltp": 131.25, "ltq": "75"}}}},
        }
    }
    assert extract_prices(message) == {NIFTY: 24510.0, OPTION: 131.25}


def test_extract_prices_skips_non_positive_and_junk():
    message = {"feeds": {NIFTY: {"ltpc": {"ltp": 0.0}}, OPTION: {"ltpc": {"ltp": "na"}}}}
    assert extract_prices(message) == {}


def test_extract_prices_handles_bad_shapes():
    assert extract_prices(None) == {}
    assert extract_prices({}) == {}
    assert extract_prices({"feeds": "oops"}) == {}


def test_last_price_fresh_then_stale():
    live = MarketFeed(access_token="", fresh_seconds=4.0)
    live._prices[NIFTY] = (24500.0, 1000.0)
    assert live.last_price(NIFTY, now=1002.0) == 24500.0  # within the window
    assert live.last_price(NIFTY, now=1005.0) is None  # older than fresh_seconds
    assert live.last_price("UNKNOWN", now=1002.0) is None


def test_on_message_populates_cache():
    live = MarketFeed(access_token="")
    live._on_message({"feeds": {NIFTY: {"ltpc": {"ltp": 24525.0}}}})
    assert live.last_price(NIFTY) == 24525.0


def test_start_without_token_stays_dormant():
    live = MarketFeed(access_token="")
    assert live.start([NIFTY]) is False
    assert live.last_price(NIFTY) is None


def test_snapshot_round_trip(tmp_path):
    path = tmp_path / "feed_snapshot.json"
    live = MarketFeed(access_token="", snapshot_path=path)
    live._on_message({"feeds": {NIFTY: {"ltpc": {"ltp": 24540.0}}}})
    assert path.exists()
    # Fresh read returns the price; a read far in the future is treated as stale.
    assert read_snapshot_price(NIFTY, path=path) == 24540.0
    assert read_snapshot_price(NIFTY, path=path, now=10_000_000_000.0) is None
    assert read_snapshot_price("UNKNOWN", path=path) is None


def test_read_snapshot_price_missing_file(tmp_path):
    assert read_snapshot_price(NIFTY, path=tmp_path / "nope.json") is None


def test_read_snapshot_price_ignores_non_positive(tmp_path):
    path = tmp_path / "feed_snapshot.json"
    path.write_text(json.dumps({"prices": {NIFTY: {"ltp": 0.0, "ts": time.time()}}}), encoding="utf-8")
    assert read_snapshot_price(NIFTY, path=path) is None
