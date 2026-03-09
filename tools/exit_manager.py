"""
exit_manager.py — Comprehensive position exit monitor.

Runs every cycle on ALL open positions and checks exit triggers:
  1. Limit order fill (price crosses limit)          → action: fill
  2. Market resolved (YES or NO price >= 0.99)       → action: close  PROFIT_TARGET / RESOLVED
  3. Exit Score > Threshold                          → action: close  SCORE_EXIT
  4. Posterior < 45%: edge has evaporated            → action: close  EDGE_GONE
  5. Spread > 20%: liquidity gone                    → action: close  LIQUIDITY
  6. HERALD contradicting signal active              → action: close  CONTRADICTING_SIGNAL
  7. Days to resolution = 0: expiry                  → action: close  EXPIRY
  8. Daily loss within 2% of max limit               → action: close  RISK_LIMIT

Each close action contains:
  bet_id, action, close_price, reason, exit_code, pnl_estimate
"""

import logging
from datetime import date, datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Exit trigger thresholds
POSTERIOR_EDGE_GONE_THRESHOLD = 0.45    # posterior < 45% → exit
SPREAD_LIQUIDITY_THRESHOLD = 0.20       # spread > 20% → exit
DAILY_LOSS_RISK_BUFFER_PCT = 2.0        # trigger when within 2% of max daily loss
HARD_SPREAD_GUARD = 0.15                # hard block close if spread > 15% (unless resolution)


class ExitMonitor:
    def __init__(self, config: dict):
        self.config = config
        self.auto_exit = config.get("betting", {}).get("auto_exit", False)
        self.exit_threshold = config.get("betting", {}).get("exit_score_threshold", 70)

    def calculate_exit_score(self, position: dict, market: dict | None) -> dict:
        """
        Compute an exit score (0-100) for an open position.
        Score > 70 generally suggests an exit.
        """
        # FAIL-SAFE: If market data is missing/stale, always HOLD.
        if not market:
            return {
                "score": 0, "reasons": ["missing_market_data"],
                "should_exit": False, "current_price": 0.0, "captured_edge": 0.0
            }

        score = 0
        reasons = []

        entry_price = float(position.get("entry_price", 0.5))
        side = str(position.get("side", "YES")).upper()
        current_price = float(market.get("yes_price" if side == "YES" else "no_price", 0.5))
        
        # 1. PnL / Captured Edge (0-40 points)
        denom = (1.0 - entry_price)
        captured_edge = 0.0
        if denom > 0:
            captured_edge = (current_price - entry_price) / denom
            if captured_edge >= 0.80:
                score += 40
                reasons.append(f"high_pnl:{captured_edge:.1%}")
            elif captured_edge >= 0.50:
                score += 25
                reasons.append(f"mid_pnl:{captured_edge:.1%}")
            elif captured_edge < -0.20:
                score += 30 # Stop loss territory
                reasons.append(f"stop_loss_trigger:{captured_edge:.1%}")

        # 2. Liquidity & Spread (0-20 points)
        spread = float(market.get("spread", 0.0))
        if spread > SPREAD_LIQUIDITY_THRESHOLD:
            score += 20
            reasons.append(f"low_liquidity:spread={spread:.1%}")
        elif spread > 0.10:
            score += 10
            reasons.append(f"thinning_liquidity:spread={spread:.1%}")

        # 3. Time to Resolution (0-20 points)
        days_left = float(market.get("days_to_resolution", 1.0))
        if days_left < 1:
            score += 20
            reasons.append("near_expiry")
        elif days_left < 3:
            score += 10
            reasons.append("closing_soon")

        # 4. Exit Target (0-20 points)
        exit_target = position.get("exit_target_price")
        if exit_target and current_price >= float(exit_target):
            score += 20
            reasons.append("target_hit")

        # HARD GUARD: Liquidity/Slippage block.
        # We block closing if the spread is too wide, even if the score is high.
        # This applies strictly to the monitor-side exit loop to prevent slippage losses.
        should_exit = score >= self.exit_threshold
        if should_exit and spread > HARD_SPREAD_GUARD:
            logger.warning("[EXIT_GUARD] High exit score (%d) but spread too wide (%.1f%%). Holding.", score, spread*100)
            should_exit = False
            reasons.append(f"HARD_GUARD:spread={spread:.1%}")

        return {
            "score": score,
            "reasons": reasons,
            "should_exit": should_exit,
            "current_price": current_price,
            "captured_edge": captured_edge
        }


def log_shadow_exits(open_positions: list[dict], current_markets: list[dict]):
    """Log hypothetical exits without executing trades (research mode only)."""
    if not open_positions or not current_markets:
        return

    monitor = ExitMonitor({}) # Default config for shadow logging

    market_lookup: dict[str, dict] = {}
    for m in current_markets:
        mid = str(m.get("condition_id") or m.get("id") or "").strip()
        if mid:
            market_lookup[mid] = m

    for pos in open_positions:
        market_id = str(pos.get("market_id", "")).strip()
        mkt = market_lookup.get(market_id)
        
        # FAIL-SAFE: Already handled by calculate_exit_score returning score=0 for None market.
        analysis = monitor.calculate_exit_score(pos, mkt)
        question = str(pos.get("question") or pos.get("market_question") or market_id)

        now = datetime.now(timezone.utc)
        hours_left = 999.0
        if mkt:
            end_date_raw = pos.get("end_date_iso") or pos.get("end_date") or mkt.get("end_date_iso") or mkt.get("end_date")
            if end_date_raw:
                try:
                    end_date = datetime.fromisoformat(str(end_date_raw).replace("Z", "+00:00"))
                    hours_left = (end_date - now).total_seconds() / 3600
                except Exception:
                    pass

        logger.info(
            "[SHADOW_EXIT] market=%s score=%d captured=%.0f%% hours_left=%.1f -> %s",
            question[:50],
            analysis["score"],
            analysis["captured_edge"] * 100,
            hours_left,
            "EXIT" if analysis["should_exit"] else "HOLD",
        )

        if analysis["should_exit"]:
            logger.info(
                "[SHADOW_EXIT] Suggesting exit for %s: Score %d (%s). Current price: %.4f.",
                question,
                analysis["score"],
                ", ".join(analysis["reasons"]),
                analysis["current_price"],
            )


def run_exit_checks(
    markets: list[dict],
    config: dict,
    herald_agent=None,
    posteriors: dict | None = None,
) -> list[dict]:
    """
    Check all open positions for exit triggers.

    Args:
        markets:       Current fetched markets (for live price lookup).
        config:        Full config dict.
        herald_agent:  HeraldAgent instance for contradicting signal check.
        posteriors:    Dict of {market_id: posterior_result} from bayesian_engine.

    Returns list of action dicts. Each dict has:
        bet_id, action, close_price, reason, exit_code
    """
    from tools.memory import get_open_bets
    from tools.execution_router import get_current_mode

    mode = get_current_mode()
    open_bets = [b for b in get_open_bets() if b.get("mode") == mode]
    if not open_bets:
        return []

    market_prices: dict[str, dict] = {
        m["condition_id"]: m for m in markets if m.get("condition_id")
    }

    at_risk = _is_daily_loss_at_risk(config, mode)
    monitor = ExitMonitor(config)

    actions: list[dict] = []

    for bet in open_bets:
        mid = bet.get("market_id", "")
        mkt = market_prices.get(mid)

        # FAIL-SAFE: Skip all automated logic if market data is missing/stale.
        if not mkt:
            continue

        yes_price = mkt.get("yes_price")
        no_price = mkt.get("no_price")
        spread = mkt.get("spread", 0.0)
        days_left = mkt.get("days_to_resolution", 1.0)

        side = bet.get("side", "YES")
        current_price = yes_price if side == "YES" else no_price
        entry_price = bet.get("entry_price", 0.5)
        bet_id = bet["id"]
        order_type = bet.get("order_type", "market")

        # -------------------------------------------------------------------
        # 1. Limit order fill check
        # -------------------------------------------------------------------
        if order_type == "limit" and bet.get("fill_status") == "pending_fill":
            if current_price is not None:
                limit_p = entry_price
                if (side == "YES" and yes_price <= limit_p) or \
                   (side == "NO" and no_price <= limit_p):
                    actions.append({
                        "bet_id": bet_id, "action": "fill",
                        "fill_price": current_price, "reason": "limit_crossed",
                    })
                    logger.info("[EXIT] Limit filled: %s @ %.3f", bet_id, current_price)
            continue

        # -------------------------------------------------------------------
        # 2. Market resolved (99% probability is effectively resolved)
        # -------------------------------------------------------------------
        if yes_price is not None and yes_price >= 0.99:
            close_price = 1.0 if side == "YES" else 0.0
            actions.append(_close_action(bet_id, close_price, "RESOLVED", "market_resolved_YES"))
            continue
        if no_price is not None and no_price >= 0.99:
            close_price = 0.0 if side == "YES" else 1.0
            actions.append(_close_action(bet_id, close_price, "RESOLVED", "market_resolved_NO"))
            continue

        # -------------------------------------------------------------------
        # 3. Exit Score Check (Automated Exit)
        # -------------------------------------------------------------------
        if current_price is not None:
            analysis = monitor.calculate_exit_score(bet, mkt)
            if analysis["should_exit"]:
                reason = f"Exit score {analysis['score']} ({', '.join(analysis['reasons'])})"
                actions.append(_close_action(bet_id, current_price, "SCORE_EXIT", reason))
                continue

        # -------------------------------------------------------------------
        # 4. Posterior < 45%: edge gone
        # -------------------------------------------------------------------
        if posteriors:
            post = posteriors.get(mid, {})
            posterior_val = post.get("posterior", 1.0)
            if posterior_val < POSTERIOR_EDGE_GONE_THRESHOLD:
                # Still respect HARD_SPREAD_GUARD for non-resolution exits
                if spread <= HARD_SPREAD_GUARD:
                    close_price = current_price if current_price is not None else entry_price
                    actions.append(_close_action(bet_id, close_price, "EDGE_GONE",
                                                 f"posterior {posterior_val:.3f} < {POSTERIOR_EDGE_GONE_THRESHOLD}"))
                    continue

        # -------------------------------------------------------------------
        # 5. Spread > 20%: liquidity gone (extreme spread)
        # -------------------------------------------------------------------
        if spread > SPREAD_LIQUIDITY_THRESHOLD:
            # Note: This trigger specifically exits because spread is too wide, 
            # so we don't guard it with HARD_SPREAD_GUARD.
            close_price = current_price if current_price is not None else entry_price
            actions.append(_close_action(bet_id, close_price, "LIQUIDITY",
                                         f"spread {spread*100:.1f}% > 20%"))
            continue

        # -------------------------------------------------------------------
        # 6. HERALD contradicting signal
        # -------------------------------------------------------------------
        if herald_agent is not None:
            market_q = bet.get("market_question", "").lower()
            contra = _check_herald_contra(herald_agent, bet, market_q)
            if contra and spread <= HARD_SPREAD_GUARD:
                close_price = current_price if current_price is not None else entry_price
                actions.append(_close_action(bet_id, close_price, "CONTRADICTING_SIGNAL",
                                             f"HERALD contra: {contra}"))
                continue

        # -------------------------------------------------------------------
        # 7. Days to resolution = 0
        # -------------------------------------------------------------------
        if days_left <= 0:
            close_price = current_price if current_price is not None else entry_price
            actions.append(_close_action(bet_id, close_price, "EXPIRY", "days_remaining=0"))
            continue

        # -------------------------------------------------------------------
        # 8. Daily loss within 2% of the limit
        # -------------------------------------------------------------------
        if at_risk and spread <= HARD_SPREAD_GUARD:
            close_price = current_price if current_price is not None else entry_price
            actions.append(_close_action(bet_id, close_price, "RISK_LIMIT",
                                         "daily loss within 2% of max limit"))
            continue

    return actions


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _close_action(bet_id: str, close_price: float, exit_code: str, reason: str) -> dict:
    logger.info("[EXIT] Close trigger: %s | code=%s | price=%.3f | %s",
                bet_id, exit_code, close_price, reason)
    return {
        "bet_id": bet_id,
        "action": "close",
        "close_price": round(close_price, 4),
        "exit_code": exit_code,
        "reason": reason,
    }


def _check_herald_contra(herald_agent, bet: dict, market_q: str) -> str | None:
    """
    Return a description string if HERALD has a signal that contradicts the bet direction.
    E.g. bet is YES/BULLISH but HERALD has a 'crash' or 'hack' signal active.
    """
    try:
        active = herald_agent.get_active_signals()
    except Exception:
        return None

    if isinstance(active, dict):
        signal_items = active.items()
    elif isinstance(active, list):
        signal_items = [
            (str(signal.get("keyword", "")), signal)
            for signal in active
            if isinstance(signal, dict)
        ]
    else:
        signal_items = []

    BEARISH_SIGNALS = {"crash", "hack", "exploit", "ban", "regulation", "liquidation",
                       "attack", "invasion", "crisis", "sanctions"}
    BULLISH_SIGNALS = {"etf", "adoption", "institutional", "halving", "approval"}

    side = bet.get("side", "YES")

    for kw, signal in signal_items:
        kw_lower = kw.lower()
        if side == "YES" and kw_lower in BEARISH_SIGNALS:
            # A bearish event signal while holding YES — contradiction
            if _keyword_in_market(kw_lower, market_q):
                return f"'{kw}' signal (bearish) vs YES position"
        elif side == "NO" and kw_lower in BULLISH_SIGNALS:
            # A bullish event signal while holding NO — contradiction
            if _keyword_in_market(kw_lower, market_q):
                return f"'{kw}' signal (bullish) vs NO position"
    return None


def _keyword_in_market(kw: str, market_q: str) -> bool:
    """Loose check — any word in kw appears in market question."""
    for word in kw.split():
        if word in market_q:
            return True
    return False


def _is_daily_loss_at_risk(config: dict, mode: str) -> bool:
    """True if today's losses are within DAILY_LOSS_RISK_BUFFER_PCT of the daily max."""
    try:
        from tools.memory import get_all_bets, get_mode_balance
        betting_cfg = config.get("betting", {})
        max_loss_pct = betting_cfg.get("max_daily_loss_pct", 10.0)
        bal = get_mode_balance(mode)
        starting = bal["starting"]
        today = date.today().isoformat()
        bets = get_all_bets()
        todays_pnl = sum(
            b.get("pnl", 0) or 0
            for b in bets
            if b.get("mode") == mode
            and (b.get("closed_at") or "").startswith(today)
            and b.get("pnl") is not None
        )
        if todays_pnl >= 0 or starting <= 0:
            return False
        current_loss_pct = abs(todays_pnl) / starting * 100
        buffer_threshold = max_loss_pct - DAILY_LOSS_RISK_BUFFER_PCT
        return current_loss_pct >= buffer_threshold
    except Exception as exc:
        logger.debug("Daily loss risk check failed: %s", exc)
        return False
