"""Deterministic market feature helpers for ORACLE."""

from __future__ import annotations

import math

from core.market_features import extract_market_features


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def compute_momentum(price_series: list[float]) -> float:
    """Return short-term momentum in [-1, 1] from a price series."""
    clean = [float(p) for p in price_series if p is not None]
    if len(clean) < 2:
        return 0.0

    window = clean[-5:]
    start = window[0]
    end = window[-1]
    if start <= 0:
        return 0.0

    pct_change = (end - start) / start
    return _clamp(pct_change / 0.10, -1.0, 1.0)


def compute_volume_acceleration(volume_series: list[float]) -> float:
    """Return volume acceleration in [-1, 1] by comparing recent vs prior slope."""
    clean = [max(0.0, float(v)) for v in volume_series if v is not None]
    if len(clean) < 4:
        return 0.0

    a, b, c, d = clean[-4], clean[-3], clean[-2], clean[-1]
    prev_slope = b - a
    curr_slope = d - c

    denom = max(abs(prev_slope), abs(curr_slope), 1.0)
    accel = (curr_slope - prev_slope) / denom
    return _clamp(accel, -1.0, 1.0)


def compute_liquidity_score(volume: float, time_window: float) -> float:
    """Return liquidity score in [0, 1] as log(volume / time_window)."""
    v = max(0.0, float(volume))
    tw = max(1e-6, float(time_window))
    liquidity_rate = v / tw

    # 0..1 around practical trading scale; saturates for very large values.
    score = math.log1p(liquidity_rate) / math.log1p(10_000.0)
    return _clamp(score, 0.0, 1.0)


def compute_time_decay(hours_to_resolution: float) -> float:
    """Return decay score in [0, 1]: nearer resolution => higher score."""
    h = max(0.0, float(hours_to_resolution))
    # Half-life style decay around 3 days.
    score = 1.0 / (1.0 + (h / 72.0))
    return _clamp(score, 0.0, 1.0)


def order_flow_imbalance(price_series: list[float], volume_series: list[float]) -> float:
    """Estimate signed order-flow imbalance in [-1, 1] from price/volume co-movement."""
    prices = [float(p) for p in price_series if p is not None]
    volumes = [max(0.0, float(v)) for v in volume_series if v is not None]
    n = min(len(prices), len(volumes))
    if n < 3:
        return 0.0

    prices = prices[-n:]
    volumes = volumes[-n:]
    signed_flow = 0.0
    total_flow = 0.0
    for i in range(1, n):
        p0 = prices[i - 1]
        p1 = prices[i]
        if p0 <= 0:
            continue
        ret = (p1 - p0) / p0
        vol = volumes[i]
        signed_flow += ret * vol
        total_flow += abs(ret) * vol

    if total_flow <= 0:
        return 0.0
    return _clamp(signed_flow / total_flow, -1.0, 1.0)


def volume_spike_detector(volume_series: list[float]) -> float:
    """Return spike score in [0, 1] using latest volume vs trailing baseline."""
    vols = [max(0.0, float(v)) for v in volume_series if v is not None]
    if len(vols) < 3:
        return 0.0

    latest = vols[-1]
    baseline = sum(vols[:-1]) / max(1, len(vols) - 1)
    if baseline <= 0:
        return 0.0

    ratio = latest / baseline
    # 1.0 -> 0 spike, 3.0+ -> full spike
    score = (ratio - 1.0) / 2.0
    return _clamp(score, 0.0, 1.0)


def spread_anomaly(spread_series: list[float]) -> float:
    """Return spread anomaly score in [0, 1] where higher means abnormal spread widening."""
    spreads = [max(0.0, float(s)) for s in spread_series if s is not None]
    if len(spreads) < 3:
        return 0.0

    latest = spreads[-1]
    baseline_list = spreads[:-1]
    baseline = sum(baseline_list) / max(1, len(baseline_list))
    if baseline <= 0:
        return _clamp(latest / 0.15, 0.0, 1.0)

    ratio = latest / baseline
    # 1.0 -> normal, 2.0+ -> anomalous
    score = ratio - 1.0
    return _clamp(score, 0.0, 1.0)


def price_reversion_signal(price_series: list[float], equilibrium: float = 0.5) -> float:
    """Return reversion probability in [0, 1] biased toward mean-reversion to equilibrium."""
    prices = [float(p) for p in price_series if p is not None]
    if not prices:
        return 0.5

    latest = prices[-1]
    dist = latest - equilibrium

    momentum = compute_momentum(prices)

    # If price is below equilibrium, reversion implies upward move (higher YES probability).
    # If price is above equilibrium, reversion implies downward move (lower YES probability).
    base = 0.5 - dist

    # Strong directional momentum reduces immediate reversion odds.
    damp = 1.0 - 0.5 * min(1.0, abs(momentum))
    score = 0.5 + (base - 0.5) * damp
    return _clamp(score, 0.0, 1.0)
