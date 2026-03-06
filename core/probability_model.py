from __future__ import annotations


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def compute_model_probability(market_features: dict, model_weights: dict | None = None) -> dict:
    """Compute deterministic YES probability from extracted features and config weights."""
    weights = model_weights or {}

    w_momentum = float(weights.get("momentum", 0.30))
    w_volume = float(weights.get("volume", 0.20))
    w_liquidity = float(weights.get("liquidity", 0.20))
    w_reversion = float(weights.get("reversion", 0.15))
    w_time = float(weights.get("time_decay", 0.15))

    weight_sum = w_momentum + w_volume + w_liquidity + w_reversion + w_time
    if weight_sum <= 0:
        weight_sum = 1.0

    base_price = _clamp(float(market_features.get("market_probability", 0.5)), 0.01, 0.99)

    momentum = float(market_features.get("price_momentum", 0.0))
    volume_score = _clamp(float(market_features.get("volume_score", 0.0)), 0.0, 1.0)
    liquidity_proxy = _clamp(float(market_features.get("liquidity_proxy", 0.0)), 0.0, 1.0)
    volatility = _clamp(float(market_features.get("volatility", 0.0)), 0.0, 1.0)
    time_score = _clamp(float(market_features.get("time_score", 0.0)), 0.0, 1.0)

    # Deterministic adjustment terms around the current market-implied baseline.
    momentum_term = momentum * 0.08
    volume_term = (volume_score - 0.5) * 0.06
    liquidity_term = (liquidity_proxy - 0.5) * 0.05
    reversion_term = (0.5 - base_price) * (1.0 - volatility) * 0.10
    time_term = (time_score - 0.5) * 0.04

    adjustment = (
        w_momentum * momentum_term
        + w_volume * volume_term
        + w_liquidity * liquidity_term
        + w_reversion * reversion_term
        + w_time * time_term
    ) / weight_sum

    model_probability = _clamp(base_price + adjustment, 0.01, 0.99)
    return {"model_probability": round(model_probability, 6)}
