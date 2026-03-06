from __future__ import annotations

from tools.execution_router import execute_trade as _execute_trade


def execute_trade(
    market: dict,
    side: str,
    bet_usdc: float,
    config: dict,
    order_type: str = "limit",
    limit_price: float | None = None,
) -> dict:
    _ = bet_usdc, order_type, limit_price
    """Route order execution using the project-level deterministic router."""
    return _execute_trade(market=market, side=side, edge=float(market.get("_computed_edge", 0.0)), config=config)
