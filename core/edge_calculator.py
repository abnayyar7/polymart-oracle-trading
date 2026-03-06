from __future__ import annotations


def calculate_edge(
    posterior_probability: float,
    market_probability: float,
    min_net_edge: float,
) -> dict:
    """Compute deterministic edge and threshold pass/fail decision."""
    posterior = float(posterior_probability)
    market = float(market_probability)
    edge = posterior - market
    has_edge = edge >= float(min_net_edge)

    return {
        "edge": round(edge, 6),
        "posterior_probability": round(posterior, 6),
        "market_probability": round(market, 6),
        "min_net_edge": round(float(min_net_edge), 6),
        "has_edge": has_edge,
    }
