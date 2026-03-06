"""Execution router for deterministic ORACLE pipeline."""

from __future__ import annotations

import json
from pathlib import Path

from config import trading_config as tc

_CONFIG_JSON = Path(__file__).parent.parent / "config" / "config.json"


def _read_runtime_mode() -> str:
    """Read runtime mode from config file with safe fallback to paper."""
    try:
        with open(_CONFIG_JSON, encoding="utf-8") as f:
            cfg = json.load(f)
        paper_trading = bool(cfg.get("betting", {}).get("paper_trading", True))
        return "paper" if paper_trading else "live"
    except Exception:
        return "paper"


def get_current_mode() -> str:
    if tc.LIVE_TRADING:
        return "live"
    return _read_runtime_mode()


def execute_trade(market: dict, side: str, edge: float, config: dict) -> dict:
    mode = get_current_mode()
    if mode == "paper":
        from tools import paper_trader

        return paper_trader.place_limit_order(
            market=market,
            side=side,
            limit_price=float(market.get("yes_price") if side == "YES" else market.get("no_price", 1.0 - float(market.get("yes_price", 0.5)))),
            edge=float(edge),
        )

    from tools.live_executor import execute as live_execute

    bet_usdc = float(config.get("betting", {}).get("min_bet_usdc", tc.MIN_POSITION_USDC))
    return live_execute(
        market=market,
        side=side,
        bet_usdc=bet_usdc,
        config=config,
        order_type="limit",
    )


def route(
    market: dict,
    side: str,
    bet_usdc: float,
    config: dict,
    exit_target_price: float | None = None,
    order_type: str | None = None,
    limit_price: float | None = None,
) -> dict:
    _ = bet_usdc, exit_target_price, order_type, limit_price
    edge_hint = float(market.get("_computed_edge", 0.0))
    return execute_trade(market=market, side=side, edge=edge_hint, config=config)
