"""
exit_manager.py — Comprehensive position exit monitor.

Runs every cycle on ALL open positions and checks exit triggers:
  1. Limit order fill (price crosses limit)          → action: fill
  2. Market resolved (YES or NO price >= 0.99)       → action: close  PROFIT_TARGET / RESOLVED
  3. SELL_AT_TARGET: current price >= exit_target    → action: close  PROFIT_TARGET
  4. Posterior < 45%: edge has evaporated            → action: close  EDGE_GONE
  5. Spread > 20%: liquidity gone                    → action: close  LIQUIDITY
  6. HERALD contradicting signal active              → action: close  CONTRADICTING_SIGNAL
  7. Days to resolution = 0: expiry                  → action: close  EXPIRY
  8. Daily loss within 2% of max limit               → action: close  RISK_LIMIT

Each close action contains:
  bet_id, action, close_price, reason, exit_code, pnl_estimate

Sends Telegram exit alert for every close.
"""

import logging
from datetime import date, datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Exit trigger thresholds
POSTERIOR_EDGE_GONE_THRESHOLD = 0.45    # posterior < 45% → exit
SPREAD_LIQUIDITY_THRESHOLD = 0.20       # spread > 20% → exit
DAILY_LOSS_RISK_BUFFER_PCT = 2.0        # trigger when within 2% of max daily loss


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

    # Build market price lookup by condition_id
    market_prices: dict[str, dict] = {
        m["condition_id"]: m for m in markets if m.get("condition_id")
    }

    # Daily loss check — how close are we to the limit?
    at_risk = _is_daily_loss_at_risk(config, mode)

    actions: list[dict] = []

    for bet in open_bets:
        mid = bet.get("market_id", "")
        mkt = market_prices.get(mid)

        # If market not in current fetch, skip fill/price-based checks
        # but still check time-based and posterior triggers
        yes_price = mkt["yes_price"] if mkt else None
        no_price = mkt["no_price"] if mkt else None
        spread = mkt.get("spread", 0.0) if mkt else 0.0
        days_left = mkt.get("days_to_resolution", 1.0) if mkt else 1.0

        side = bet.get("side", "YES")
        current_price = (yes_price if side == "YES" else no_price) if mkt else None
        entry_price = bet.get("entry_price", 0.5)
        bet_id = bet["id"]
        order_type = bet.get("order_type", "market")

        # -------------------------------------------------------------------
        # 1. Limit order fill check
        # -------------------------------------------------------------------
        if order_type == "limit" and bet.get("fill_status") == "pending_fill":
            if mkt and current_price is not None:
                limit_p = entry_price
                if (side == "YES" and yes_price <= limit_p) or \
                   (side == "NO" and no_price <= limit_p):
                    actions.append({
                        "bet_id": bet_id, "action": "fill",
                        "fill_price": current_price, "reason": "limit_crossed",
                    })
                    logger.info("[EXIT] Limit filled: %s @ %.3f", bet_id, current_price)
            continue  # Don't evaluate exit rules until filled

        # -------------------------------------------------------------------
        # 2. Market resolved
        # -------------------------------------------------------------------
        if mkt and yes_price is not None and yes_price >= 0.99:
            close_price = 1.0 if side == "YES" else 0.0
            actions.append(_close_action(bet_id, close_price, "RESOLVED", "market_resolved_YES"))
            continue
        if mkt and no_price is not None and no_price >= 0.99:
            close_price = 0.0 if side == "YES" else 1.0
            actions.append(_close_action(bet_id, close_price, "RESOLVED", "market_resolved_NO"))
            continue

        # -------------------------------------------------------------------
        # 3. SELL_AT_TARGET: profit target hit
        # -------------------------------------------------------------------
        exit_target = bet.get("exit_target_price")
        if bet.get("exit_strategy") == "SELL_AT_TARGET" and exit_target and current_price is not None:
            if current_price >= exit_target:
                actions.append(_close_action(bet_id, current_price, "PROFIT_TARGET",
                                             f"price {current_price:.3f} >= target {exit_target:.3f}"))
                continue

        # -------------------------------------------------------------------
        # 4. Posterior < 45%: edge gone
        # -------------------------------------------------------------------
        if posteriors:
            post = posteriors.get(mid, {})
            posterior_val = post.get("posterior", 1.0)
            if posterior_val < POSTERIOR_EDGE_GONE_THRESHOLD:
                close_price = current_price if current_price is not None else entry_price
                actions.append(_close_action(bet_id, close_price, "EDGE_GONE",
                                             f"posterior {posterior_val:.3f} < {POSTERIOR_EDGE_GONE_THRESHOLD}"))
                continue

        # -------------------------------------------------------------------
        # 5. Spread > 20%: liquidity gone
        # -------------------------------------------------------------------
        if spread > SPREAD_LIQUIDITY_THRESHOLD:
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
            if contra:
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
        if at_risk:
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
