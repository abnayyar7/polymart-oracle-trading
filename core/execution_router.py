from __future__ import annotations

import logging

from tools.execution_router import execute_trade as _execute_trade


logger = logging.getLogger(__name__)


def init_cycle_skip_reasons() -> dict[str, int]:
    """Initialize per-cycle routing diagnostics counters."""
    return {"EDGE_FAIL": 0, "LIQ_FAIL": 0, "RISK_FAIL": 0, "TRADE": 0}


def increment_cycle_skip_reason(skip_reasons: dict[str, int], reason: str) -> None:
    """Increment a known skip reason counter (or initialize unknown keys)."""
    skip_reasons[reason] = int(skip_reasons.get(reason, 0)) + 1


def log_cycle_summary(skip_reasons: dict[str, int], total: int, wallet: float) -> None:
    """Emit one-line cycle summary with skip reason breakdown."""
    logger.info(
        f"Cycle | markets={int(total)} "
        f"TRADE={int(skip_reasons.get('TRADE', 0))} "
        f"EDGE_FAIL={int(skip_reasons.get('EDGE_FAIL', 0))} "
        f"LIQ_FAIL={int(skip_reasons.get('LIQ_FAIL', 0))} "
        f"RISK_FAIL={int(skip_reasons.get('RISK_FAIL', 0))} "
        f"wallet={float(wallet):.2f}"
    )


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
