"""
market_gate.py — Pre-APEX deterministic quality gate.

Runs AFTER edge_calculator and BEFORE APEX/Gemini.
Pure Python logic — no LLM involved.

Design principle:
  APEX should only make ONE decision: "Given a high-quality market with real
  edge, do we bet YES or NO?"  All market-quality decisions are made here.

Gate rules (checked in order — first failure rejects):
  1. Days remaining > 60        → reject
     Days remaining > 30        → reject unless volume > $50k
  2. Volume < $1,000            → reject
  3. YES price in [0.43, 0.57]  → reject (coin-flip zone)
  4. Uncertain resolution kw    → reject
  5. Scout NO_DATA sentinel     → reject
  6. Scout confidence < 60%     → reject
  7. Edge < 5%                  → reject
  8. YES price > 0.90 or < 0.10 → reject (unless sniper candidate)
  9. Market age < 1 hour        → reject

Returns: (approved: bool, reason: str)
  approved=True  → reason "APPROVED"
  approved=False → reason "RULE_NAME: detail"
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Threshold constants
# ---------------------------------------------------------------------------
GATE_MAX_DAYS             = 30          # Standard day ceiling
GATE_MAX_DAYS_HIGH_VOL    = 60          # Extended ceiling for high-volume markets
GATE_HIGH_VOL_THRESHOLD   = 50_000      # USDC — qualifies for extended day ceiling
GATE_MIN_VOLUME           = 1_000       # USDC minimum market volume
GATE_COIN_FLIP_LO         = 0.43        # Coin-flip zone lower bound
GATE_COIN_FLIP_HI         = 0.57        # Coin-flip zone upper bound
GATE_MIN_CONFIDENCE       = 60          # SCOUT minimum confidence after gate
GATE_MIN_EDGE             = 0.05        # Net edge minimum (5%)
GATE_EXTREME_PRICE_HI     = 0.90        # Extreme price upper bound
GATE_EXTREME_PRICE_LO     = 0.10        # Extreme price lower bound
GATE_MIN_MARKET_AGE_H     = 1.0         # Minimum market age in hours

# Phrases that indicate resolution timing is ambiguous / speculative
UNCERTAIN_KEYWORDS = [
    "before gta",
    "before grand theft",
    "before any game",
    "before movie",
    "before album",
    "before season",
    " ever ",
    "at some point",
    "before elon",
    "before trump announces",
    "at any point",
    "someday",
    "at any time",
    "eventually",
    "before the end of time",
    "before humanity",
]

# Compatibility aliases matching ORACLE_FIXES.md terminology
UNCERTAIN_RESOLUTION = [
    "before gta", "before grand theft auto",
    "before minecraft", "before fortnite",
    "before the movie", "before film releases",
    "before album", "before season ",
    "at some point", "ever happen",
    "before elon", "someday",
]

OBSCURE_MARKETS = [
    "colombian", "will ph win", "will cd win",
    "will plc win", "nominee for co-08",
    "nominee for nh-01", "nominee for al-",
    "nominee for va-", "nominee for ny-10",
]

BTC_EXTREME_TARGETS = [
    "bitcoin hit $500,000", "bitcoin hit $1m",
    "bitcoin hit $1,000,000", "bitcoin reach $500k",
    "bitcoin reach $1m", "btc hit $500",
    "btc hit $1m",
]


def check_market(market: dict, scout_result: dict | None = None) -> tuple[bool, str]:
    """Hard gate rules before APEX to block low-quality markets."""
    question = str(market.get("question", "")).lower()
    days = float(market.get("days_remaining", market.get("days_to_resolution", 0)) or 0)
    volume = float(market.get("volume") or 0)
    yes_price = float(market.get("yes_price", 0.5) or 0.5)

    if days > 90:
        return False, f"DAYS_TOO_LONG:{days:.1f}"

    if volume < 1000:
        return False, f"LOW_VOLUME:{volume:.0f}"

    if 0.43 <= yes_price <= 0.57:
        return False, f"COIN_FLIP:{yes_price:.3f}"

    for phrase in UNCERTAIN_RESOLUTION:
        if phrase in question:
            return False, f"UNCERTAIN_RESOLUTION:{phrase}"

    for phrase in OBSCURE_MARKETS:
        if phrase in question:
            return False, f"OBSCURE_MARKET:{phrase}"

    for phrase in BTC_EXTREME_TARGETS:
        if phrase in question:
            return False, f"EXTREME_TARGET:{phrase}"

    if scout_result:
        if scout_result.get("data_quality") == "NO_DATA" or scout_result.get("sentiment") == "NO_DATA":
            return False, "NO_SIGNAL_DATA"
        if int(scout_result.get("confidence", 0)) < 60:
            return False, f"LOW_CONFIDENCE:{scout_result.get('confidence', 0)}%"

    return True, "APPROVED"


class MarketGate:
    """
    Stateless deterministic filter applied between edge_calculator and APEX.

    Usage:
        gate = MarketGate()
        approved, reason = gate.check(market, scout_result, edge_result)
        if not approved:
            self._gate_stats[reason] = self._gate_stats.get(reason, 0) + 1
            continue
    """

    def check(
        self,
        market: dict,
        scout_result: dict,
        edge_result: dict | None = None,
    ) -> tuple[bool, str]:
        """
        Evaluate all gate rules in order.

        Returns:
            (True,  "APPROVED")           if all rules pass
            (False, "RULE_NAME: detail")  on first failure
        """
        question = market.get("question", market.get("market_question", "")).lower()
        yes_price = float(market.get("yes_price", 0.5))
        volume    = float(market.get("volume", 0.0))
        days_left = float(market.get("days_to_resolution", 999.0))
        is_sniper = bool(market.get("_sniper_candidate", False))

        # ------------------------------------------------------------------
        # Rule 1: Days-to-resolution ceiling
        # ------------------------------------------------------------------
        if days_left > GATE_MAX_DAYS_HIGH_VOL:
            reason = f"DAYS_TOO_FAR: {days_left:.0f} days > {GATE_MAX_DAYS_HIGH_VOL}"
            self._log_reject(market, reason)
            return False, reason

        if days_left > GATE_MAX_DAYS and volume < GATE_HIGH_VOL_THRESHOLD:
            reason = (
                f"DAYS_TOO_FAR: {days_left:.0f} days > {GATE_MAX_DAYS} "
                f"and volume ${volume:,.0f} < ${GATE_HIGH_VOL_THRESHOLD:,}"
            )
            self._log_reject(market, reason)
            return False, reason

        # ------------------------------------------------------------------
        # Rule 2: Minimum volume
        # ------------------------------------------------------------------
        if volume < GATE_MIN_VOLUME:
            reason = f"LOW_VOLUME: ${volume:,.0f} < ${GATE_MIN_VOLUME:,}"
            self._log_reject(market, reason)
            return False, reason

        # ------------------------------------------------------------------
        # Rule 3: Coin-flip zone (no edge can reliably exist here)
        # ------------------------------------------------------------------
        if GATE_COIN_FLIP_LO <= yes_price <= GATE_COIN_FLIP_HI:
            reason = f"COIN_FLIP: YES={yes_price:.3f} in [{GATE_COIN_FLIP_LO}, {GATE_COIN_FLIP_HI}]"
            self._log_reject(market, reason)
            return False, reason

        # ------------------------------------------------------------------
        # Rule 4: Uncertain resolution keywords
        # ------------------------------------------------------------------
        for kw in UNCERTAIN_KEYWORDS:
            if kw in question:
                reason = f"UNCERTAIN_RESOLUTION: matched '{kw.strip()}'"
                self._log_reject(market, reason)
                return False, reason

        # ------------------------------------------------------------------
        # Rule 5: SCOUT NO_DATA sentinel
        # ------------------------------------------------------------------
        sentinel = scout_result.get("sentiment", "")
        if sentinel == "NO_DATA":
            reason = "NO_SIGNALS: SCOUT returned NO_DATA"
            self._log_reject(market, reason)
            return False, reason

        # ------------------------------------------------------------------
        # Rule 6: SCOUT confidence floor
        # ------------------------------------------------------------------
        confidence = int(scout_result.get("confidence", 0))
        if confidence < GATE_MIN_CONFIDENCE:
            reason = f"LOW_CONFIDENCE: SCOUT={confidence}% < {GATE_MIN_CONFIDENCE}%"
            self._log_reject(market, reason)
            return False, reason

        # ------------------------------------------------------------------
        # Rule 7: Net edge floor (only checked if edge_result provided)
        # ------------------------------------------------------------------
        if edge_result is not None:
            net_edge = float(edge_result.get("net_edge", 0.0))
            if net_edge < GATE_MIN_EDGE:
                reason = f"INSUFFICIENT_EDGE: net_edge={net_edge*100:.1f}% < {GATE_MIN_EDGE*100:.0f}%"
                self._log_reject(market, reason)
                return False, reason

        # ------------------------------------------------------------------
        # Rule 8: Extreme price zone (skip unless sniper)
        # ------------------------------------------------------------------
        if not is_sniper:
            if yes_price > GATE_EXTREME_PRICE_HI:
                reason = f"EXTREME_PRICE_HIGH: YES={yes_price:.3f} > {GATE_EXTREME_PRICE_HI}"
                self._log_reject(market, reason)
                return False, reason
            if yes_price < GATE_EXTREME_PRICE_LO:
                reason = f"EXTREME_PRICE_LOW: YES={yes_price:.3f} < {GATE_EXTREME_PRICE_LO}"
                self._log_reject(market, reason)
                return False, reason

        # ------------------------------------------------------------------
        # Rule 9: Market age < 1 hour (avoid flash markets with no data)
        # ------------------------------------------------------------------
        created_at = market.get("created_at") or market.get("start_date_iso") or ""
        if created_at:
            age_h = self._market_age_hours(created_at)
            if age_h is not None and age_h < GATE_MIN_MARKET_AGE_H:
                reason = f"TOO_NEW: market age {age_h:.1f}h < {GATE_MIN_MARKET_AGE_H}h"
                self._log_reject(market, reason)
                return False, reason

        logger.debug("[GATE] APPROVED: %s", market.get("question", "")[:60])
        return True, "APPROVED"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _log_reject(market: dict, reason: str) -> None:
        q = market.get("question", market.get("market_question", ""))[:60]
        logger.info("[GATE REJECT] %s | %s", q, reason)

    @staticmethod
    def _market_age_hours(created_at: str) -> float | None:
        """Parse ISO timestamp and return age in hours. Returns None on failure."""
        try:
            # Handle both 'Z' suffix and '+00:00' offsets
            ts = created_at.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            return (now - dt).total_seconds() / 3600.0
        except Exception:
            return None
