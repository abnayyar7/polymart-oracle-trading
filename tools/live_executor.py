"""
live_executor.py — Real Polymarket CLOB order placement.

Strategy:
  - Place LIMIT orders by default (earn maker rebates, avoid 3.15% taker fee)
  - Track pending limit orders across cycles in data/bets.json
  - Fall back to MARKET order if limit not filled within 2 scan cycles
  - All trades logged with mode: "live" in data/bets.json

Requires: py-clob-client>=0.18.0, web3>=6.0.0 (uncomment in requirements.txt)
Chain:    Polygon Mainnet ONLY — Chain ID 137. Never zkEVM (1101).
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

TAKER_FEE_PCT = 0.0315    # 3.15% taker fee on market orders
LIMIT_REBATE_PCT = 0.0    # Effective net fee for limit orders
LIMIT_TIMEOUT_CYCLES = 2  # Fall back to market after this many unfilled cycles


def execute(
    market: dict,
    side: str,
    bet_usdc: float,
    config: dict,
    order_type: str = "limit",
    limit_price: float | None = None,
    exit_target_price: float | None = None,
) -> dict:
    """
    Place a real CLOB order.

    Attempts a limit order at current best price first.
    If order_type is forced to "market", places immediately at taker fee.

    Returns execution result dict.
    """
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, OrderArgs
    except ImportError:
        logger.error(
            "[LIVE] py-clob-client not installed. "
            "Uncomment py-clob-client and web3 in requirements.txt for live trading."
        )
        return {"success": False, "error": "py-clob-client not installed", "mode": "live"}

    poly_cfg = config.get("polymarket", {})
    chain_id = poly_cfg.get("chain_id", 137)
    if chain_id != 137:
        logger.error("[LIVE] Wrong chain ID %d — must be Polygon Mainnet (137).", chain_id)
        return {"success": False, "error": f"Wrong chain ID {chain_id}", "mode": "live"}

    private_key = poly_cfg.get("private_key", "")
    if not private_key:
        logger.error("[LIVE] Polymarket private_key not set in secrets.")
        return {"success": False, "error": "private_key not configured", "mode": "live"}

    price = market["yes_price"] if side == "YES" else market["no_price"]
    fill_price = limit_price if limit_price is not None else price
    fee = 0.0 if order_type == "limit" else round(bet_usdc * TAKER_FEE_PCT, 4)

    try:
        client = ClobClient(
            host=poly_cfg["host"],
            chain_id=chain_id,
            private_key=private_key,
            creds=ApiCreds(
                api_key=poly_cfg["api_key"],
                api_secret=poly_cfg["api_secret"],
                api_passphrase=poly_cfg["api_passphrase"],
            ),
        )

        token_id = _get_token_id(market, side)
        order_args = OrderArgs(token_id=token_id, price=fill_price, size=bet_usdc)
        resp = client.create_and_post_order(order_args)
        order_id = str(resp)

        logger.info(
            "[LIVE][%s] BET_%s | %s | $%.2f | price=%.3f | fee=$%.4f | order=%s",
            order_type.upper(), side, market.get("question", "")[:50],
            bet_usdc, fill_price, fee, order_id,
        )

        return {
            "success": True,
            "mode": "live",
            "order_type": order_type,
            "order_id": order_id,
            "market_id": market.get("condition_id", ""),
            "side": side,
            "bet_usdc": bet_usdc,
            "fee_paid": fee,
            "effective_usdc": round(bet_usdc - fee, 4),
            "executed_price": fill_price,
            "contracts": round((bet_usdc - fee) / fill_price, 4) if fill_price > 0 else 0,
            "exit_target_price": exit_target_price,
            "fill_status": "pending_fill" if order_type == "limit" else "open",
        }

    except Exception as exc:
        logger.error("[LIVE] Order placement failed: %s", exc)
        return {"success": False, "error": str(exc), "mode": "live"}


def check_open_positions(markets: list[dict], config: dict) -> list[dict]:
    """
    Per-cycle check of live open positions.

    For limit orders that have been pending >= LIMIT_TIMEOUT_CYCLES:
      → Return action "escalate_to_market" for caller to re-submit as market order.

    Returns list of action dicts.
    """
    from tools.memory import get_open_bets

    open_bets = [b for b in get_open_bets() if b.get("mode") == "live"]
    if not open_bets:
        return []

    actions = []
    for bet in open_bets:
        if bet.get("order_type") != "limit":
            continue
        if bet.get("fill_status") != "pending_fill":
            continue
        cycles_pending = bet.get("cycles_pending", 0) + 1
        if cycles_pending >= LIMIT_TIMEOUT_CYCLES:
            actions.append({
                "bet_id": bet["id"],
                "action": "escalate_to_market",
                "reason": f"Limit unfilled after {cycles_pending} cycles",
                "market_id": bet["market_id"],
                "side": bet["side"],
                "bet_usdc": bet["bet_usdc"],
            })
            logger.info(
                "[LIVE] Limit timeout — escalating to market: %s (cycles=%d)",
                bet["id"], cycles_pending,
            )
        else:
            # Bump cycles_pending counter in memory
            from tools.memory import get_all_bets, _write_json, BETS_JSON
            all_bets = get_all_bets()
            for b in all_bets:
                if b["id"] == bet["id"]:
                    b["cycles_pending"] = cycles_pending
            _write_json(BETS_JSON, all_bets)

    return actions


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_token_id(market: dict, side: str) -> str:
    tokens = market.get("raw", {}).get("tokens", [])
    for tok in tokens:
        if tok.get("outcome", "").upper() == side:
            return tok.get("token_id", "")
    return ""
