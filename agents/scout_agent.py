"""Deterministic SCOUT probability model (no LLM dependencies)."""

from __future__ import annotations

import logging
from collections import defaultdict, deque

from tools.market_features import (
    compute_liquidity_score,
    compute_momentum,
    compute_time_decay,
    compute_volume_acceleration,
    order_flow_imbalance,
    price_reversion_signal,
    spread_anomaly,
    volume_spike_detector,
)

logger = logging.getLogger(__name__)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class ScoutAgent:
    def __init__(self, config: dict):
        self.config = config
        self.min_confidence = float(config.get("betting", {}).get("min_confidence", 55))
        self.debug_signals = bool(config.get("DEBUG_SIGNALS", False) or config.get("debug_signals", False))
        self._price_history = defaultdict(lambda: deque(maxlen=8))
        self._volume_history = defaultdict(lambda: deque(maxlen=8))
        self._spread_history = defaultdict(lambda: deque(maxlen=8))

        strategy_cfg = config.get("strategies", {}) or {}
        self.strategies = {
            "cross_market_mispricing": bool(strategy_cfg.get("cross_market_mispricing", True)),
            "momentum_burst": bool(strategy_cfg.get("momentum_burst", True)),
            "liquidity_shock": bool(strategy_cfg.get("liquidity_shock", True)),
            "late_resolution_edge": bool(strategy_cfg.get("late_resolution_edge", True)),
        }

        default_weights = {
            "momentum": 0.3,
            "volume": 0.2,
            "liquidity": 0.2,
            "reversion": 0.15,
            "time_decay": 0.15,
        }
        cfg_weights = config.get("model_weights", {}) or {}
        merged = {k: float(cfg_weights.get(k, v)) for k, v in default_weights.items()}
        total = sum(max(0.0, w) for w in merged.values())
        if total <= 0:
            self.model_weights = default_weights
        else:
            self.model_weights = {k: max(0.0, w) / total for k, w in merged.items()}

    @staticmethod
    def _has_minimum_signals(signals: dict) -> bool:
        required = (
            "market_id",
            "polymarket_yes_price",
            "market_volume_usdc",
            "market_liquidity_usdc",
            "days_to_resolution",
        )
        return all(signals.get(k) is not None for k in required)

    def analyze(self, signals: dict) -> dict | None:
        """Return deterministic probability, confidence, and feature diagnostics."""
        if not self._has_minimum_signals(signals):
            return {
                "market_id": signals.get("market_id", ""),
                "probability": 0.5,
                "confidence": 0.0,
                "sentiment": "NEUTRAL",
                "features": {},
                "narrative": "Insufficient market data to score.",
                "key_signals": [],
                "conflicting_signals": ["missing_required_features"],
                "reasoning_chain": "Feature model skipped due to incomplete inputs.",
            }

        market_id = str(signals.get("market_id", ""))
        yes_price = float(signals.get("polymarket_yes_price", 0.5) or 0.5)
        volume = float(signals.get("market_volume_usdc", 0.0) or 0.0)
        liquidity_input = float(signals.get("market_liquidity_usdc", 0.0) or 0.0)
        days_to_resolution = float(signals.get("days_to_resolution", 0.0) or 0.0)
        spread = float(signals.get("market_spread", 0.0) or 0.0)
        hours_to_resolution = max(0.0, days_to_resolution * 24.0)

        self._price_history[market_id].append(yes_price)
        self._volume_history[market_id].append(volume)
        self._spread_history[market_id].append(spread)

        price_series = list(self._price_history[market_id])
        volume_series = list(self._volume_history[market_id])
        spread_series = list(self._spread_history[market_id])

        raw_momentum = compute_momentum(price_series)  # [-1, 1]
        raw_vol_accel = compute_volume_acceleration(volume_series)  # [-1, 1]
        time_window_hours = max(1.0, min(24.0, hours_to_resolution if hours_to_resolution > 0 else 24.0))
        liquidity_score = compute_liquidity_score(max(volume, liquidity_input), time_window_hours)  # [0, 1]
        time_decay = compute_time_decay(hours_to_resolution)  # [0, 1]
        ofi = order_flow_imbalance(price_series, volume_series)  # [-1, 1]
        vol_spike = volume_spike_detector(volume_series)  # [0, 1]
        spread_anom = spread_anomaly(spread_series)  # [0, 1]
        reversion_signal = price_reversion_signal(price_series)  # [0, 1]

        # Required feature: distance from 0.5 market equilibrium.
        equilibrium_distance = _clamp(abs(yes_price - 0.5) * 2.0, 0.0, 1.0)
        equilibrium_signed = _clamp((yes_price - 0.5) * 2.0, -1.0, 1.0)
        if not self.strategies["cross_market_mispricing"]:
            equilibrium_distance = 0.0
            equilibrium_signed = 0.0

        normalized_momentum = _clamp(
            0.5 * (0.5 + 0.5 * raw_momentum)
            + 0.3 * (0.5 + 0.5 * equilibrium_signed)
            + 0.2 * (0.5 + 0.5 * ofi),
            0.0,
            1.0,
        )
        normalized_vol_accel = _clamp(0.7 * (0.5 + 0.5 * raw_vol_accel) + 0.3 * vol_spike, 0.0, 1.0)
        adjusted_liquidity = _clamp(liquidity_score * (1.0 - 0.35 * spread_anom), 0.0, 1.0)
        adjusted_reversion = _clamp(reversion_signal * (1.0 - 0.25 * spread_anom), 0.0, 1.0)

        if not self.strategies["momentum_burst"]:
            normalized_momentum = 0.5
            normalized_vol_accel = 0.5
        if not self.strategies["liquidity_shock"]:
            spread_anom = 0.0
            adjusted_liquidity = liquidity_score
            adjusted_reversion = reversion_signal
        if not self.strategies["late_resolution_edge"]:
            time_decay = 0.5

        w = self.model_weights

        probability = _clamp(
            w["momentum"] * normalized_momentum
            + w["volume"] * normalized_vol_accel
            + w["liquidity"] * adjusted_liquidity
            + w["reversion"] * adjusted_reversion
            + w["time_decay"] * time_decay,
            0.0,
            1.0,
        )

        if probability >= 0.55:
            sentiment = "BULLISH"
        elif probability <= 0.45:
            sentiment = "BEARISH"
        else:
            sentiment = "NEUTRAL"

        confidence = _clamp(50.0 + abs(probability - 0.5) * 100.0 + equilibrium_distance * 10.0, 0.0, 100.0)

        features = {
            "momentum": round(raw_momentum, 4),
            "normalized_momentum": round(normalized_momentum, 4),
            "volume_acceleration": round(raw_vol_accel, 4),
            "normalized_volume_acceleration": round(normalized_vol_accel, 4),
            "order_flow_imbalance": round(ofi, 4),
            "volume_spike": round(vol_spike, 4),
            "spread_anomaly": round(spread_anom, 4),
            "price_reversion_signal": round(reversion_signal, 4),
            "liquidity_score": round(liquidity_score, 4),
            "adjusted_liquidity": round(adjusted_liquidity, 4),
            "adjusted_reversion": round(adjusted_reversion, 4),
            "equilibrium_distance": round(equilibrium_distance, 4),
            "time_decay": round(time_decay, 4),
            "hours_to_resolution": round(hours_to_resolution, 2),
            "weights": {k: round(v, 4) for k, v in w.items()},
            "strategies": self.strategies,
        }

        result = {
            "market_id": market_id,
            "probability": round(probability, 4),
            "confidence": round(confidence, 2),
            "sentiment": sentiment,
            "features": features,
            "narrative": "Deterministic model from momentum, volume acceleration, liquidity, and time decay.",
            "key_signals": [
                f"momentum={features['momentum']}",
                f"vol_accel={features['volume_acceleration']}",
                f"liq={features['liquidity_score']}",
            ],
            "conflicting_signals": [],
            "reasoning_chain": (
                "prob = w_momentum*momentum + w_volume*volume + w_liquidity*liquidity "
                "+ w_reversion*reversion + w_time_decay*time_decay"
            ),
        }

        logger.info(
            "SCOUT DET: %s | prob=%.3f conf=%.1f sentiment=%s",
            signals.get("market_question", "")[:60],
            result["probability"],
            result["confidence"],
            result["sentiment"],
        )
        if self.debug_signals:
            logger.info(
                "SCOUT FEATURES: market=%s mom=%.4f ofi=%.4f vol_accel=%.4f vol_spike=%.4f liq=%.4f "
                "spread_anom=%.4f rev=%.4f time=%.4f w=%s strat=%s",
                market_id,
                features["momentum"],
                features["order_flow_imbalance"],
                features["volume_acceleration"],
                features["volume_spike"],
                features["adjusted_liquidity"],
                features["spread_anomaly"],
                features["adjusted_reversion"],
                features["time_decay"],
                features["weights"],
                features["strategies"],
            )
        return result

    def should_escalate(self, scout_result: dict) -> bool:
        """True if confidence >= min_confidence and directional sentiment is present."""
        if not scout_result:
            return False
        confidence = float(scout_result.get("confidence", 0.0) or 0.0)
        sentiment = scout_result.get("sentiment", "NEUTRAL")
        return confidence >= self.min_confidence and sentiment in ("BULLISH", "BEARISH")
