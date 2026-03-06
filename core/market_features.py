from __future__ import annotations

import math
from statistics import pstdev


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _norm_log_scale(value: float, cap: float) -> float:
    safe_value = max(0.0, float(value))
    return _clamp(math.log1p(safe_value) / math.log1p(max(1.0, cap)), 0.0, 1.0)


def _compute_momentum(price_series: list[float]) -> float:
    if len(price_series) < 2:
        return 0.0
    start = float(price_series[0])
    end = float(price_series[-1])
    if start <= 0:
        return 0.0
    return _clamp((end - start) / start, -1.0, 1.0)


def _compute_volatility(price_series: list[float]) -> float:
    if len(price_series) < 3:
        return 0.0

    returns: list[float] = []
    for idx in range(1, len(price_series)):
        prev = float(price_series[idx - 1])
        curr = float(price_series[idx])
        if prev <= 0:
            continue
        returns.append((curr - prev) / prev)

    if len(returns) < 2:
        return 0.0

    # Saturate around 15% return volatility to keep deterministic bounded behavior.
    vol = pstdev(returns)
    return _clamp(vol / 0.15, 0.0, 1.0)


def extract_market_features(
    market: dict,
    price_history: list[float] | None = None,
    volume_history: list[float] | None = None,
) -> dict:
    """Extract deterministic market features for both live and backtest paths."""
    yes_price = _clamp(float(market.get("yes_price", 0.5) or 0.5), 0.01, 0.99)
    spread = _clamp(float(market.get("spread", 0.0) or 0.0), 0.0, 1.0)
    volume = max(0.0, float(market.get("volume", 0.0) or 0.0))
    liquidity = max(0.0, float(market.get("liquidity", volume) or volume))
    days_to_resolution = max(0.0, float(market.get("days_to_resolution", 0.0) or 0.0))

    prices = [float(p) for p in (price_history or []) if p is not None][-20:]
    if not prices:
        prices = [yes_price]
    elif prices[-1] != yes_price:
        prices = prices + [yes_price]

    volumes = [max(0.0, float(v)) for v in (volume_history or []) if v is not None][-20:]
    if not volumes:
        volumes = [volume]
    elif volumes[-1] != volume:
        volumes = volumes + [volume]

    momentum = _compute_momentum(prices[-6:])
    volatility = _compute_volatility(prices[-12:])

    volume_score = _norm_log_scale(volume, 250_000.0)
    liquidity_proxy = _norm_log_scale(liquidity, 250_000.0)
    time_score = 1.0 / (1.0 + (days_to_resolution / 30.0))

    return {
        "market_id": str(market.get("condition_id") or market.get("id") or ""),
        "question": str(market.get("question", "")),
        "price": yes_price,
        "spread": spread,
        "liquidity_proxy": round(liquidity_proxy, 6),
        "volume": round(volume, 6),
        "volume_score": round(volume_score, 6),
        "time_to_resolution": round(days_to_resolution, 6),
        "time_score": round(_clamp(time_score, 0.0, 1.0), 6),
        "price_momentum": round(momentum, 6),
        "volatility": round(volatility, 6),
        "market_probability": yes_price,
    }
