from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_order_router_never_imports_gemini() -> None:
    imports = _imported_modules(ROOT / "modules" / "upstox" / "orders.py")
    assert not any("copilot" in name or "google.genai" in name or "google" == name for name in imports)


def test_mock_mode_refuses_live_order_post() -> None:
    from modules.upstox.exceptions import UpstoxAuthError
    from modules.upstox.orders import flatten_mis_position, place_mis_market_order

    try:
        place_mis_market_order(
            instrument_token="NSE_EQ|INE002A01018",
            side="BUY",
            quantity=1,
            symbol="RELIANCE",
        )
        raise AssertionError("MOCK mode must not reach the order endpoint")
    except UpstoxAuthError as exc:
        assert "MOCK" in str(exc)

    try:
        flatten_mis_position(
            instrument_token="NSE_EQ|INE002A01018",
            open_side="BUY",
            quantity=1,
            symbol="RELIANCE",
        )
        raise AssertionError("MOCK mode must not flatten live MIS")
    except UpstoxAuthError as exc:
        assert "MOCK" in str(exc)


def test_strategy_engine_has_no_broker_or_llm_io() -> None:
    for rel in ("modules/strategy/orb.py", "modules/strategy/banknifty_late.py", "modules/options/simulator.py"):
        imports = _imported_modules(ROOT / rel)
        assert not any(name.startswith("modules.upstox") or "genai" in name for name in imports)


def test_risk_interceptor_never_imports_order_router() -> None:
    imports = _imported_modules(ROOT / "modules" / "risk" / "risk_manager.py")
    assert not any("orders" in name or "genai" in name for name in imports)
