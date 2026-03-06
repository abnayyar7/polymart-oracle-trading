from __future__ import annotations

import math


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _logit(probability: float) -> float:
    p = _clamp(float(probability), 1e-6, 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


def bayesian_update(
    model_probability: float,
    market_price_probability: float,
    bayes_strength: float = 0.35,
) -> dict:
    """Blend model and market information in log-odds space for stable posterior updates."""
    strength = _clamp(float(bayes_strength), 0.0, 1.0)
    model_logit = _logit(model_probability)
    market_logit = _logit(market_price_probability)

    posterior_logit = ((1.0 - strength) * model_logit) + (strength * market_logit)
    posterior_probability = _clamp(_sigmoid(posterior_logit), 0.01, 0.99)

    return {
        "posterior_probability": round(posterior_probability, 6),
        "bayes_strength": round(strength, 6),
    }
