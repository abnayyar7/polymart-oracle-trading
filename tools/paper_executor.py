"""
paper_executor.py — Virtual bet execution for paper trading mode.

Simulates the full Polymarket CLOB order lifecycle without real money:
  - Market orders: fill immediately at current bid/ask, apply 3.15% taker fee
  - Limit orders:  record target price, check fill on each 15-min cycle
  - Position resolution: if market YES=1.0 or NO=1.0, close at resolved price
  - All state persisted in data/bets.json and data/balance.json via memory.py
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Polymarket taker fee on market orders
TAKER_FEE_PCT = 0.0315   # 3.15%
LIMIT_FEE_PCT = 0.0       # 0% — limit orders earn rebates (net 0 for simplicity)


def execute(
    market: dict,
    side: str,
    bet_usdc: float,
    order_type: str = "market",
    limit_price: float | None = None,
    exit_target_price: float | None = None,
) -> dict:
    """
    Simulate a paper bet placement.

    order_type:
      "market" — fill immediately at current CLOB price, apply 3.15% taker fee
      "limit"  — record target price, mark as pending_fill; auto-fills on next cycle

    Returns execution result dict (same shape as live_executor.execute).
    """
    price = market["yes_price"] if side == "YES" else market["no_price"]

    if order_type == "limit":
        fill_price = limit_price if limit_price is not None else price
        fee = 0.0
        effective_usdc = bet_usdc
        status = "pending_fill"
        logger.info(
            "[PAPER][LIMIT] BET_%s | %s | $%.2f | limit=%.3f | fee=$0.00",
            side, market.get("question", "")[:60], bet_usdc, fill_price,
        )
    else:
        fee = round(bet_usdc * TAKER_FEE_PCT, 4)
        effective_usdc = round(bet_usdc - fee, 4)
        fill_price = price
        status = "open"
        logger.info(
            "[PAPER][MARKET] BET_%s | %s | $%.2f | price=%.3f | fee=$%.4f",
            side, market.get("question", "")[:60], bet_usdc, fill_price, fee,
        )

    contracts = round(effective_usdc / fill_price, 4) if fill_price > 0 else 0

    return {
        "success": True,
        "mode": "paper",
        "order_type": order_type,
        "order_id": f"PAPER_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
        "market_id": market.get("condition_id", ""),
        "side": side,
        "bet_usdc": bet_usdc,
        "fee_paid": fee,
        "effective_usdc": effective_usdc,
        "executed_price": fill_price,
        "contracts": contracts,
        "exit_target_price": exit_target_price,
        "fill_status": status,
    }


def check_open_positions(markets: list[dict], config: dict) -> list[dict]:
    """
    Per-cycle check of all open paper positions.

    For each open bet:
      1. If order_type == "limit" and pending_fill: check if current price crossed limit
      2. If market resolved (YES=1.0 or NO=1.0): auto-close at resolved price
      3. If exit_strategy == SELL_AT_TARGET and current price >= exit_target: close

    Returns list of dicts: {"bet_id": str, "action": "fill"|"close", "close_price": float, "reason": str}
    """
    from tools.memory import get_open_bets

    open_bets = [b for b in get_open_bets() if b.get("mode") == "paper"]
    if not open_bets:
        return []

    # Build a quick lookup of current market prices by condition_id
    market_prices: dict[str, dict] = {}
    for m in markets:
        mid = m.get("condition_id", "")
        if mid:
            market_prices[mid] = m

    actions = []

    for bet in open_bets:
        mid = bet.get("market_id", "")
        mkt = market_prices.get(mid)
        if not mkt:
            continue

        yes_price = mkt.get("yes_price", 0.5)
        no_price = mkt.get("no_price", 0.5)
        current_price = yes_price if bet["side"] == "YES" else no_price
        bet_id = bet["id"]
        order_type = bet.get("order_type", "market")

        # 1. Limit order pending fill check
        if order_type == "limit" and bet.get("fill_status") == "pending_fill":
            limit_price = bet.get("executed_price", 0.5)
            if bet["side"] == "YES" and yes_price <= limit_price:
                actions.append({"bet_id": bet_id, "action": "fill", "fill_price": yes_price, "reason": "limit_crossed"})
                logger.info("[PAPER] Limit order filled: %s @ %.3f", bet_id, yes_price)
            elif bet["side"] == "NO" and no_price <= limit_price:
                actions.append({"bet_id": bet_id, "action": "fill", "fill_price": no_price, "reason": "limit_crossed"})
                logger.info("[PAPER] Limit order filled: %s @ %.3f", bet_id, no_price)
            continue  # Don't evaluate exit rules until filled

        # 2. Resolution check (price hits 1.0 = resolved)
        if yes_price >= 0.99:
            close_price = 1.0 if bet["side"] == "YES" else 0.0
            actions.append({"bet_id": bet_id, "action": "close", "close_price": close_price, "reason": "market_resolved"})
            logger.info("[PAPER] Market resolved YES: closing %s at %.1f", bet_id, close_price)
            continue
        if no_price >= 0.99:
            close_price = 0.0 if bet["side"] == "YES" else 1.0
            actions.append({"bet_id": bet_id, "action": "close", "close_price": close_price, "reason": "market_resolved"})
            logger.info("[PAPER] Market resolved NO: closing %s at %.1f", bet_id, close_price)
            continue

        # 3. SELL_AT_TARGET exit
        exit_target = bet.get("exit_target_price")
        if bet.get("exit_strategy") == "SELL_AT_TARGET" and exit_target:
            if current_price >= exit_target:
                actions.append({"bet_id": bet_id, "action": "close", "close_price": current_price, "reason": "target_hit"})
                logger.info("[PAPER] Exit target hit: %s @ %.3f (target %.3f)", bet_id, current_price, exit_target)

    return actions
