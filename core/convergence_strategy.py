"""Late-resolution convergence strategy.

Finds markets where price has not yet converged to 1.0 despite near-certain
resolution. This module does not use the probability model; edge is mechanical.
"""

from __future__ import annotations

from datetime import datetime, timezone

from dateutil.parser import parse

MIN_PRICE = 0.75
MAX_PRICE = 0.97
MAX_HOURS_LEFT = 168
MIN_LIQUIDITY = 200
MIN_NET_EDGE = 0.03

_AMBIGUOUS_KEYWORDS = ("likely", "approximately", "around", "unclear")


def find_convergence_trades(markets: list, fee_rate: float = 0.02) -> list:
    """Scan markets for late-resolution convergence opportunities."""
    candidates, _ = find_convergence_trades_with_diagnostics(markets=markets, fee_rate=fee_rate)
    return candidates


def find_convergence_trades_with_diagnostics(markets: list, fee_rate: float = 0.02) -> tuple[list, dict]:
    """Scan markets and return (candidates, diagnostics counters)."""
    candidates: list[dict] = []
    now = datetime.now(timezone.utc)
    diagnostics = {
        "total": 0,
        "missing_end_date": 0,
        "invalid_end_date": 0,
        "price_band_fail": 0,
        "hours_window_fail": 0,
        "liquidity_fail": 0,
        "edge_fail": 0,
        "ambiguous_fail": 0,
        "exceptions": 0,
        "candidates": 0,
    }

    for m in markets:
        diagnostics["total"] += 1
        try:
            price = float(m.get("yes_price") or m.get("price") or 0.0)
            liquidity = float(m.get("liquidity") or m.get("volume") or 0.0)
            end_date_ts = m.get("end_date_iso") or m.get("end_date")
            market_id = str(m.get("condition_id") or m.get("id") or m.get("market_id") or "")
            question = str(m.get("question") or "")

            if not end_date_ts:
                diagnostics["missing_end_date"] += 1
                continue

            if not isinstance(end_date_ts, str):
                diagnostics["invalid_end_date"] += 1
                continue

            end_dt = parse(end_date_ts)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)

            hours_left = (end_dt - now).total_seconds() / 3600.0

            if not (MIN_PRICE <= price <= MAX_PRICE):
                diagnostics["price_band_fail"] += 1
                continue
            if hours_left > MAX_HOURS_LEFT or hours_left < 0:
                diagnostics["hours_window_fail"] += 1
                continue
            if liquidity < MIN_LIQUIDITY:
                diagnostics["liquidity_fail"] += 1
                continue

            fee_est = price * float(fee_rate)
            raw_edge = 1.0 - price
            net_edge = raw_edge - fee_est

            if net_edge < MIN_NET_EDGE:
                diagnostics["edge_fail"] += 1
                continue

            lower_q = question.lower()
            if any(kw in lower_q for kw in _AMBIGUOUS_KEYWORDS):
                diagnostics["ambiguous_fail"] += 1
                continue

            candidates.append(
                {
                    "market_id": market_id,
                    "question": question[:60],
                    "price": price,
                    "hours_left": round(hours_left, 1),
                    "liquidity": liquidity,
                    "raw_edge": round(raw_edge, 4),
                    "fee_est": round(fee_est, 4),
                    "net_edge": round(net_edge, 4),
                    "strategy": "CONVERGENCE",
                    "slug": str(m.get("slug") or ""),
                    "event_slug": str(m.get("event_slug") or ""),
                    "market": m,
                }
            )
        except Exception:
            diagnostics["exceptions"] += 1
            continue

    ordered = sorted(candidates, key=lambda x: x["net_edge"], reverse=True)
    diagnostics["candidates"] = len(ordered)
    return ordered, diagnostics
