"""Live market tiles that update in the browser without a Streamlit rerun.

The three number cards (index spot and the two break levels) used to be painted by
the auto-refreshing fragment, so the whole page re-ran every few seconds just to
move one number. Here the cards are a small HTML island rendered once. Its own
JavaScript polls two static JSON files and patches the numbers in place, so the
price ticks live while Python never re-runs and the page never jumps the scroll.

Two sources, both served by Streamlit's static file server under ``/app/static``:

* ``feed_snapshot.json`` — the live ticks the instance loop's socket writes
  (~1s). Used for the spot when it is fresh.
* ``desk_state_<name>.json`` — written by the desk each refresh with the current
  spot and the two break levels. The fallback when no socket tick is fresh (for
  example the loop is off, or the Upstox SDK is not installed), so the tiles
  still update as fast as the desk computes them.

Nothing here touches trade logic; it is display only.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import streamlit as st

from config.settings import PROJECT_ROOT

STATIC_DIR = PROJECT_ROOT / "static"
SNAPSHOT_URL = "/app/static/feed_snapshot.json"
# A socket tick older than this many seconds is ignored in favour of the
# desk-written state, so a dead socket never freezes the number.
SPOT_FRESH_SECONDS = 6


def state_path(name: str) -> Path:
    return STATIC_DIR / f"desk_state_{name}.json"


def write_tile_state(
    name: str,
    spot: float,
    call_above: str,
    put_below: str,
) -> None:
    """Write the current tile values for the browser to poll. Never raises."""

    payload = {
        "spot": float(spot) if spot else None,
        "call_above": str(call_above),
        "put_below": str(put_below),
        "ts": time.time(),
    }
    try:
        STATIC_DIR.mkdir(parents=True, exist_ok=True)
        path = state_path(name)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        try:
            tmp.replace(path)
        except OSError:
            path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


_TEMPLATE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<style>
  :root { color-scheme: dark; }
  html, body { margin: 0; padding: 0; background: transparent; }
  body { font-family: "Source Sans Pro", -apple-system, system-ui, sans-serif; }
  .row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 0.75rem; }
  .card {
    background: #141b23; border: 1px solid #2a3646; border-radius: 12px;
    padding: 0.95rem 1rem 0.85rem 1rem; min-height: 96px; box-sizing: border-box;
  }
  .label {
    color: #8b9aab; font-size: 0.75rem; font-weight: 700;
    letter-spacing: 0.08em; text-transform: uppercase;
    display: flex; align-items: center; gap: 0.4rem;
  }
  .value { color: #f4f6f8; font-size: 1.55rem; font-weight: 760; line-height: 1.2; margin-top: 0.25rem; }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: #3dd68c; opacity: 0.35; transition: opacity 0.2s; }
  .dot.on { opacity: 1; box-shadow: 0 0 6px #3dd68c; }
  @media (max-width: 520px) { .row { grid-template-columns: 1fr; } }
</style>
</head>
<body>
  <div class="row">
    <div class="card">
      <div class="label">__SPOT_LABEL__ <span id="dot" class="dot"></span></div>
      <div class="value" id="spot">__SPOT0__</div>
    </div>
    <div class="card">
      <div class="label">Call above</div>
      <div class="value" id="call">__CALL0__</div>
    </div>
    <div class="card">
      <div class="label">Put below</div>
      <div class="value" id="put">__PUT0__</div>
    </div>
  </div>
<script>
  var SPOT_KEY = __SPOT_KEY__;
  var STATE_URL = __STATE_URL__;
  var SNAP_URL = __SNAP_URL__;
  var FRESH = __FRESH__;

  function fmt(n) {
    if (n === null || n === undefined || isNaN(n)) return "\\u2014";
    return Math.round(n).toLocaleString("en-IN");
  }
  async function getJson(url) {
    try {
      var r = await fetch(url + "?t=" + Date.now(), { cache: "no-store" });
      if (!r.ok) return null;
      return await r.json();
    } catch (e) { return null; }
  }
  async function tick() {
    var spot = null, live = false;
    var state = await getJson(STATE_URL);
    if (state) {
      if (state.spot !== null && state.spot !== undefined) spot = state.spot;
      if (state.call_above !== undefined) document.getElementById("call").textContent = state.call_above;
      if (state.put_below !== undefined) document.getElementById("put").textContent = state.put_below;
    }
    var snap = await getJson(SNAP_URL);
    if (snap && snap.prices && snap.prices[SPOT_KEY]) {
      var e = snap.prices[SPOT_KEY];
      if (e.ltp && (Date.now() / 1000 - e.ts) < FRESH) { spot = e.ltp; live = true; }
    }
    if (spot !== null) document.getElementById("spot").textContent = fmt(spot);
    document.getElementById("dot").className = live ? "dot on" : "dot";
  }
  tick();
  setInterval(tick, 1000);
</script>
</body>
</html>
"""


def live_market_tiles(
    spot_key: str,
    state_name: str,
    spot_label: str = "Nifty",
    spot0: str = "…",
    call0: str = "…",
    put0: str = "…",
    height: int = 150,
) -> None:
    """Render the live tile island. Call once, outside any auto-refresh fragment."""

    doc = (
        _TEMPLATE.replace("__SPOT_LABEL__", spot_label)
        .replace("__SPOT0__", spot0)
        .replace("__CALL0__", call0)
        .replace("__PUT0__", put0)
        .replace("__SPOT_KEY__", json.dumps(spot_key))
        .replace("__STATE_URL__", json.dumps(f"/app/static/desk_state_{state_name}.json"))
        .replace("__SNAP_URL__", json.dumps(SNAPSHOT_URL))
        .replace("__FRESH__", str(SPOT_FRESH_SECONDS))
    )
    # Raw HTML string embedded in a JavaScript-executing, same-origin iframe —
    # the direct replacement for the deprecated st.components.v1.html.
    st.iframe(doc, height=height)
