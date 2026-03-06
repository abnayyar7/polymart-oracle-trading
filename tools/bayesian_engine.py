"""Compatibility wrapper for deterministic Bayesian update."""

from __future__ import annotations

from core.bayesian_engine import bayesian_update


def update_posterior(
    market: dict,
    scout_result: dict,
    signals: dict,
    herald_active: bool = False,
    signal_age_hours: float = 0.0,
) -> dict:
    """Backward-compatible interface mapped to deterministic Bayesian update."""
    _ = signals, herald_active, signal_age_hours
    model_probability = float(scout_result.get("probability") or scout_result.get("model_probability") or scout_result.get("confidence", 50) / 100.0)
    market_prob = float(market.get("yes_price", 0.5))
    out = bayesian_update(model_probability=model_probability, market_price_probability=market_prob)
    return {
        "posterior": out["posterior_probability"],
        "posterior_confidence": int(out["posterior_probability"] * 100),
        "direction": "YES" if out["posterior_probability"] >= 0.5 else "NO",
        "bayes_strength": out["bayes_strength"],
    }
