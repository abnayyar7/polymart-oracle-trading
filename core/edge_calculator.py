from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


def calculate_edge(
    posterior_probability: float,
    market_probability: float,
    min_net_edge: float,
    market_id: str = "unknown",
) -> dict:
    """Compute deterministic edge and threshold pass/fail decision."""
    posterior = float(posterior_probability)
    market = float(market_probability)
    raw_edge = posterior - market

    # Diagnostic estimates for post-fee edge visibility.
    fee_est = market * 0.02
    slippage_est = 0.005
    net_edge = raw_edge - fee_est - slippage_est
    has_edge = raw_edge >= float(min_net_edge)

    safe_market_id = str(market_id or "unknown")
    logger.info(
        f"[EDGE] {safe_market_id[:25]:<25} "
        f"model={posterior:.3f} market={market:.3f} "
        f"raw={raw_edge:+.3f} fee={fee_est:.3f} net={net_edge:+.3f} "
        f"{'-> TRADE' if net_edge >= float(min_net_edge) else '-> SKIP'}"
    )

    return {
        "edge": round(raw_edge, 6),
        "raw_edge": round(raw_edge, 6),
        "net_edge": round(net_edge, 6),
        "fee_est": round(fee_est, 6),
        "posterior_probability": round(posterior, 6),
        "market_probability": round(market, 6),
        "min_net_edge": round(float(min_net_edge), 6),
        "has_edge": has_edge,
    }
