"""Compatibility wrapper for deterministic edge calculator."""

from __future__ import annotations

from config.trading_config import MIN_NET_EDGE
from core.edge_calculator import calculate_edge as _calc_edge


def calculate_edge(market: dict, scout_result: dict, bet_usdc: float = 1.0, order_type: str = "limit") -> dict:
    """Backward-compatible edge API returning legacy fields plus deterministic decision."""
    _ = bet_usdc, order_type
    posterior = float(scout_result.get("posterior") or scout_result.get("posterior_probability") or scout_result.get("probability") or 0.5)
    market_probability = float(market.get("yes_price", 0.5))
    result = _calc_edge(
        posterior_probability=posterior,
        market_probability=market_probability,
        min_net_edge=float(MIN_NET_EDGE),
    )

    # Legacy fields expected by older call-sites.
    return {
        "side": "YES" if result["edge"] >= 0 else "NO",
        "bet_side": "YES" if result["edge"] >= 0 else "NO",
        "model_prob": round(posterior, 6),
        "implied_prob": round(market_probability, 6),
        "raw_edge": result["edge"],
        "fee_drag": 0.0,
        "net_edge": result["edge"],
        "ev": round(result["edge"], 6),
        "skip": not result["has_edge"],
        "skip_reason": "BELOW_MIN_NET_EDGE" if not result["has_edge"] else "",
        "has_edge": result["has_edge"],
        "reason": "" if result["has_edge"] else "BELOW_MIN_NET_EDGE",
    }
