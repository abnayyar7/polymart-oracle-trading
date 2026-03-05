"""
edge_calculator.py — Edge and Expected Value (EV) calculator.

Translates SCOUT sentiment/confidence into a model probability, then computes
the raw edge against the Polymarket implied probability, applies fee drag, and
calculates EV.

Fee drag thresholds (Polymarket CLOB):
  Limit order:  edge must exceed 3%  (maker rebate ≈ 0%, so net fee ≈ 0%)
  Market order: edge must exceed 6%  (taker fee = 3.15%, round-trip ≈ 6%)

Skip signal: EV <= 0 OR edge after fee drag <= 0.
"""

import logging

logger = logging.getLogger(__name__)

# Minimum edge required after fee drag to justify a bet
LIMIT_MIN_EDGE = 0.03   # 3%
MARKET_MIN_EDGE = 0.06  # 6%

# Taker fee (Polymarket CLOB market orders)
TAKER_FEE = 0.0315


def calculate_edge(
    market: dict,
    scout_result: dict,
    bet_usdc: float = 1.0,
    order_type: str = "limit",
) -> dict:
    """
    Calculate edge, EV, and whether to skip this market.

    Model probability derivation:
      BULLISH with confidence C  → model_prob(YES) = C / 100
      BEARISH with confidence C  → model_prob(NO)  = C / 100
                                    → model_prob(YES) = 1 - C / 100

    Returns dict with:
      side:              "YES" or "NO" (direction suggested by SCOUT)
      model_prob:        float 0-1
      implied_prob:      float 0-1 (from market price)
      raw_edge:          float (model_prob - implied_prob)
      fee_drag:          float
      net_edge:          float (raw_edge - fee_drag)
      ev:                float (net_edge × bet_usdc)
      skip:              bool (True → caller should skip this market)
      skip_reason:       str (set when skip=True)
    """
    sentiment = scout_result.get("sentiment", "NEUTRAL")
    model_prob = scout_result.get("confidence", 50) / 100.0
    yes_price = float(market.get("yes_price", 0.5) or 0.5)
    no_price = market.get("no_price")
    no_price = float(no_price if no_price is not None else (1.0 - yes_price))

    if sentiment == "BULLISH":
        side = "YES"
        implied_prob = yes_price
    elif sentiment == "BEARISH":
        side = "NO"
        implied_prob = no_price
    else:
        result = _skip("NEUTRAL_SENTIMENT")
        result["has_edge"] = False
        result["reason"] = "NEUTRAL_SENTIMENT"
        return result

    raw_edge = model_prob - implied_prob

    fee_drag = TAKER_FEE if order_type == "market" else 0.03
    min_edge = MARKET_MIN_EDGE if order_type == "market" else LIMIT_MIN_EDGE
    net_edge = raw_edge - fee_drag
    ev = round(net_edge * bet_usdc, 4)

    if net_edge <= 0:
        logger.info(
            "EDGE SKIP: %s | side=%s model=%.3f implied=%.3f raw=%.3f net=%.3f",
            market.get("question", "")[:55],
            side,
            model_prob,
            implied_prob,
            raw_edge,
            net_edge,
        )
        result = _skip(
            f"INSUFFICIENT_EDGE: net={net_edge:.3f}",
            side=side,
            model_prob=model_prob,
            implied_prob=implied_prob,
            raw_edge=raw_edge,
            fee_drag=fee_drag,
            net_edge=net_edge,
            ev=ev,
        )
        result["has_edge"] = False
        result["reason"] = f"INSUFFICIENT_EDGE: net={net_edge:.3f}"
        return result

    if net_edge < min_edge:
        result = _skip(
            f"Edge {net_edge*100:.1f}% below {min_edge*100:.0f}% minimum",
            side=side,
            model_prob=model_prob,
            implied_prob=implied_prob,
            raw_edge=raw_edge,
            fee_drag=fee_drag,
            net_edge=net_edge,
            ev=ev,
        )
        result["has_edge"] = False
        result["reason"] = f"INSUFFICIENT_EDGE: net={net_edge:.3f}"
        return result

    result = {
        "side": side,
        "bet_side": side,
        "model_prob": round(model_prob, 4),
        "implied_prob": round(implied_prob, 4),
        "raw_edge": round(raw_edge, 4),
        "fee_drag": round(fee_drag, 4),
        "net_edge": round(net_edge, 4),
        "ev": ev,
        "has_edge": True,
        "reason": "",
        "skip": False,
        "skip_reason": "",
    }

    logger.info(
        "EDGE: %s | side=%s | model=%.3f implied=%.3f | raw=%.3f net=%.3f | EV=%.4f",
        market.get("question", "")[:55],
        side, model_prob, implied_prob, raw_edge, net_edge, ev,
    )
    return result


def kelly_fraction(edge: float, odds: float) -> float:
    """
    Full Kelly formula: f = (edge × odds - (1 - edge)) / odds
    where:
      edge = net_edge (probability advantage)
      odds = decimal odds of winning = 1 / implied_prob

    Returns fraction of bankroll to bet (0.0 if negative/zero).
    """
    if odds <= 0 or edge <= 0:
        return 0.0
    f = (edge * odds - (1.0 - edge)) / odds
    return max(0.0, round(f, 4))


def quarter_kelly_bet(edge: float, odds: float, balance: float, min_bet: float = 1.0, max_bet: float = 5.0) -> float:
    """
    Quarter Kelly bet size with hard caps.
    Returns 0.0 if Kelly fraction is zero or negative (no edge).
    """
    f = kelly_fraction(edge, odds)
    if f <= 0:
        return 0.0
    quarter_f = f / 4.0
    raw_bet = quarter_f * balance
    return round(max(min_bet, min(max_bet, raw_bet)), 2)


def _skip(reason: str, **extra) -> dict:
    base = {
        "side": extra.get("side", ""),
        "bet_side": extra.get("side", ""),
        "model_prob": extra.get("model_prob", 0.0),
        "implied_prob": extra.get("implied_prob", 0.0),
        "raw_edge": extra.get("raw_edge", 0.0),
        "fee_drag": extra.get("fee_drag", 0.0),
        "net_edge": extra.get("net_edge", 0.0),
        "ev": extra.get("ev", 0.0),
        "skip": True,
        "skip_reason": reason,
    }
    logger.debug("EDGE SKIP: %s", reason)
    return base
