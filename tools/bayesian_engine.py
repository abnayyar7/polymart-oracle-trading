"""
bayesian_engine.py — Bayesian posterior probability updater.

Takes SCOUT's initial confidence as prior probability and applies signal
multipliers to produce a posterior. Stores history in data/posteriors.json.

Multipliers (applied to the log-odds to avoid probability boundary issues):
  HERALD breaking signal active:       × 1.25
  Price momentum > 5% in direction:    × 1.15
  Volume spike > 2× 24h average:       × 1.10
  Contradicting news in RSI/sentiment: × 0.80
  Spread widens (> 10%):               × 0.85
  Time decay > 6h since signal:        × 0.95 per hour (compounding)

Output:
  posterior: float 0-1 (calibrated probability)
  posterior_confidence: int 0-100 (× 100 for use by APEX)
  multipliers_applied: list[str] (for logging / APEX context)
"""

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
POSTERIORS_JSON = ROOT / "data" / "posteriors.json"


# ---------------------------------------------------------------------------
# Multiplier definitions
# ---------------------------------------------------------------------------

_MULTIPLIERS = {
    "herald_signal":      1.25,
    "price_momentum":     1.15,
    "volume_spike":       1.10,
    "contradicting_news": 0.80,
    "spread_widening":    0.85,
}

_TIME_DECAY_PER_HOUR = 0.95   # applied once per hour past the 6h threshold
_TIME_DECAY_THRESHOLD_H = 6


def _prob_to_log_odds(p: float) -> float:
    p = max(0.001, min(0.999, p))
    return math.log(p / (1 - p))


def _log_odds_to_prob(lo: float) -> float:
    return 1.0 / (1.0 + math.exp(-lo))


# ---------------------------------------------------------------------------
# Core update
# ---------------------------------------------------------------------------

def update_posterior(
    market: dict,
    scout_result: dict,
    signals: dict,
    herald_active: bool = False,
    signal_age_hours: float = 0.0,
) -> dict:
    """
    Compute posterior probability from SCOUT prior + signal multipliers.

    Args:
        market:           Polymarket market dict (for spread check).
        scout_result:     SCOUT output with 'confidence' and 'sentiment'.
        signals:          Raw signal dict from collect_signals().
        herald_active:    True if HERALD breaking signal is active.
        signal_age_hours: Hours since the SCOUT signal was generated.

    Returns dict with:
        prior:                 float (SCOUT confidence / 100)
        posterior:             float (updated probability)
        posterior_confidence:  int (posterior × 100)
        multipliers_applied:   list[str]
        direction:             "YES" | "NO" | "NEUTRAL"
    """
    sentiment = scout_result.get("sentiment", "NEUTRAL")
    confidence = scout_result.get("confidence", 0)

    if sentiment == "NEUTRAL" or confidence == 0:
        return _neutral_result(confidence)

    prior = confidence / 100.0
    log_odds = _prob_to_log_odds(prior)
    applied: list[str] = []

    # 1. HERALD breaking signal
    if herald_active or signals.get("herald_active", False):
        boost = _MULTIPLIERS["herald_signal"]
        log_odds *= boost
        applied.append(f"herald_signal ×{boost}")

    # 2. Price momentum in the expected direction
    price_change = signals.get("btc_price_change_pct_24h", 0.0)
    if sentiment == "BULLISH" and price_change > 5.0:
        m = _MULTIPLIERS["price_momentum"]
        log_odds *= m
        applied.append(f"price_momentum_bullish ×{m}")
    elif sentiment == "BEARISH" and price_change < -5.0:
        m = _MULTIPLIERS["price_momentum"]
        log_odds *= m
        applied.append(f"price_momentum_bearish ×{m}")

    # 3. Volume spike (> 2× average approximated by RSI momentum)
    rsi = signals.get("rsi_14")
    if rsi is not None:
        # RSI > 70 with BULLISH or < 30 with BEARISH = momentum confirmation
        if (sentiment == "BULLISH" and rsi > 70) or (sentiment == "BEARISH" and rsi < 30):
            m = _MULTIPLIERS["volume_spike"]
            log_odds *= m
            applied.append(f"rsi_momentum_confirm ×{m}")

    # 4. Contradicting signals (RSI extreme in opposite direction)
    if (sentiment == "BULLISH" and rsi is not None and rsi < 30) or \
       (sentiment == "BEARISH" and rsi is not None and rsi > 70):
        m = _MULTIPLIERS["contradicting_news"]
        log_odds *= m
        applied.append(f"contradicting_rsi ×{m}")

    # Also check StockTwits for contradiction
    st_bullish = signals.get("stocktwits_bullish_pct", 50)
    st_bearish = signals.get("stocktwits_bearish_pct", 50)
    if sentiment == "BULLISH" and st_bearish > 70:
        m = _MULTIPLIERS["contradicting_news"]
        log_odds *= m
        applied.append(f"stocktwits_contra ×{m}")
    elif sentiment == "BEARISH" and st_bullish > 70:
        m = _MULTIPLIERS["contradicting_news"]
        log_odds *= m
        applied.append(f"stocktwits_contra ×{m}")

    # 5. Spread widening (> 10% = liquidity concern)
    spread = market.get("spread", 0.0)
    if spread > 0.10:
        m = _MULTIPLIERS["spread_widening"]
        log_odds *= m
        applied.append(f"spread_wide({spread*100:.1f}%) ×{m}")

    # 6. Time decay (past 6h threshold)
    if signal_age_hours > _TIME_DECAY_THRESHOLD_H:
        excess_hours = signal_age_hours - _TIME_DECAY_THRESHOLD_H
        decay = _TIME_DECAY_PER_HOUR ** excess_hours
        log_odds *= decay
        applied.append(f"time_decay({signal_age_hours:.1f}h) ×{decay:.3f}")

    posterior = _log_odds_to_prob(log_odds)
    posterior = round(max(0.01, min(0.99, posterior)), 4)
    posterior_confidence = int(posterior * 100)
    direction = "YES" if sentiment == "BULLISH" else "NO"

    result = {
        "prior": round(prior, 4),
        "posterior": posterior,
        "posterior_confidence": posterior_confidence,
        "multipliers_applied": applied,
        "direction": direction,
    }

    logger.info(
        "BAYESIAN: prior=%.3f posterior=%.3f (conf=%d%%) | %s | market: %s",
        prior, posterior, posterior_confidence,
        ", ".join(applied) if applied else "no adjustments",
        market.get("question", "")[:55],
    )

    _persist(market.get("condition_id", ""), result)
    return result


def _neutral_result(confidence: int) -> dict:
    return {
        "prior": confidence / 100.0,
        "posterior": 0.5,
        "posterior_confidence": 50,
        "multipliers_applied": [],
        "direction": "NEUTRAL",
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _persist(market_id: str, result: dict):
    """Append this posterior to data/posteriors.json."""
    try:
        if POSTERIORS_JSON.exists():
            with open(POSTERIORS_JSON) as f:
                history = json.load(f)
            if not isinstance(history, list):
                history = []
        else:
            history = []

        history.append({
            "market_id": market_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **result,
        })

        # Keep last 500 records
        if len(history) > 500:
            history = history[-500:]

        POSTERIORS_JSON.parent.mkdir(parents=True, exist_ok=True)
        with open(POSTERIORS_JSON, "w") as f:
            json.dump(history, f, indent=2)
    except Exception as exc:
        logger.debug("Posteriors persist failed: %s", exc)


def get_latest_posterior(market_id: str) -> dict | None:
    """Return the most recent posterior record for a given market_id."""
    try:
        if not POSTERIORS_JSON.exists():
            return None
        with open(POSTERIORS_JSON) as f:
            history = json.load(f)
        matches = [r for r in history if r.get("market_id") == market_id]
        return matches[-1] if matches else None
    except Exception:
        return None
