"""
polymarket_tools.py — Polymarket CLOB API integration.

Responsibilities:
  - Fetch active markets (filtered by volume, spread, days to resolution)
  - Arbitrage pre-check (YES + NO < $0.97)
  - Low-probability sniper opportunity detection
  - Paper bet execution (logs without real API call)
  - Live bet execution (real CLOB API — guarded by paper_trading flag)

Chain: Polygon Mainnet ONLY (Chain ID 137). Never zkEVM (Chain ID 1101).
"""

import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config" / "config.json"

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "ORACLE/3.0", "Accept": "application/json"})

HTTP_RETRIES = 3
HTTP_BACKOFF_SECONDS = 1.0

ARB_THRESHOLD = 0.94        # YES + NO < this = guaranteed arb after 3% fee round-trip
SNIPER_MAX_YES_PRICE = 0.15  # 1-15%
SNIPER_MIN_VOLUME = 500.0    # must have confirmed volume > $500 USDC
SNIPER_MAX_VOLUME = 5000.0   # < $5k volume (low-attention market sweet spot)
SNIPER_MIN_DAYS = 3          # >= 3 days to resolution
SNIPER_MAX_DAYS = 30         # <= 30 days (long-dated markets too uncertain for sniper)
SNIPER_MAX_MARKET_AGE_H = 24 # < 24h old

FILTER_TAG_WHITELIST = {
    "crypto",
    "bitcoin",
    "ethereum",
    "politics",
    "elections",
    "world",
    "economics",
    "sports",
    "nfl",
    "nba",
    "soccer",
}

LOCAL_PRIMARY_REJECT_KEYWORDS = [
    "nominee for",
    "democratic primary",
    "republican primary",
    "governor primary",
    "senate primary",
    "co-",
    "nh-",
    "ny-",
    "al-",
]

MAJOR_ENTITY_KEYWORDS = [
    "bitcoin", "btc", "trump", "fed", "nato", "ukraine", "china", "iran",
    "biden", "ethereum", "eth", "xrp", "sol", "world cup", "super bowl",
    "nba finals", "champions league", "premier league",
]

HIGH_PROFILE_KEYWORDS = [
    "election", "war", "world cup", "super bowl", "nba finals", "champions league",
    "nfl", "nba", "soccer", "fed", "nato", "ukraine", "china", "iran",
]

MAJOR_SPORTS_KEYWORDS = [
    "world cup", "super bowl", "nfl", "nba", "soccer", "champions league", "premier league",
]


def _text_blob(market: dict) -> str:
    tags = market.get("tags") or []
    return " ".join([
        (market.get("question") or ""),
        (market.get("category") or ""),
        " ".join(str(t) for t in tags),
    ]).lower()


def _is_local_primary_market(market: dict) -> bool:
    text = _text_blob(market)
    return any(term in text for term in LOCAL_PRIMARY_REJECT_KEYWORDS)


def _has_whitelisted_topic(market: dict) -> bool:
    text = _text_blob(market)
    tags = {str(t).strip().lower() for t in (market.get("tags") or [])}

    if tags.intersection(FILTER_TAG_WHITELIST):
        return True

    # Sports are only allowed for major-league/global keywords.
    if "sport" in text and not any(k in text for k in MAJOR_SPORTS_KEYWORDS):
        return False

    return any(term in text for term in FILTER_TAG_WHITELIST)


def _has_major_entity(market: dict) -> bool:
    text = _text_blob(market)
    return any(term in text for term in MAJOR_ENTITY_KEYWORDS)


def _is_high_profile_event(market: dict) -> bool:
    text = _text_blob(market)
    return any(term in text for term in HIGH_PROFILE_KEYWORDS)


def _has_herald_match(market: dict, herald_signals: list[dict] | None = None) -> bool:
    if not herald_signals:
        return False
    text = _text_blob(market)
    for signal in herald_signals:
        keyword = str(signal.get("keyword", "")).strip().lower()
        if keyword and keyword in text:
            return True
    return False


def score_market_relevance(market: dict, herald_signals: list[dict] | None = None) -> tuple[int, bool]:
    """
    Score a market from 0-10+ before SCOUT.
    Returns (score, herald_match).
    """
    score = 0
    herald_match = _has_herald_match(market, herald_signals)

    volume = market.get("volume")
    volume = 0.0 if volume is None else _to_float(volume)
    days = _to_float(market.get("days_to_resolution"))

    if _has_major_entity(market):
        score += 3
    if volume > 10000:
        score += 2
    if herald_match:
        score += 2
    if 1 <= days <= 30:
        score += 2
    if _is_high_profile_event(market):
        score += 1

    if _is_local_primary_market(market):
        score -= 5
    if volume <= 0:
        score -= 5
    if days > 180:
        score -= 3

    return score, herald_match


def prefilter_markets_for_scout(markets: list[dict], herald_signals: list[dict] | None = None) -> tuple[list[dict], dict[str, int]]:
    """
    Apply pre-SCOUT filters and return ordered high-relevance markets.

    Filter order:
      A) Category/topic whitelist and local/state-primary rejection
      B) Strict volume filter (None treated as 0)
      C) Relevance score (>= 4)
      D) Priority sort for downstream processing
    """
    stats = {
        "total": len(markets),
        "reject_filter_a": 0,
        "reject_null_volume": 0,
        "reject_low_volume": 0,
        "reject_score": 0,
        "passed": 0,
    }

    candidates: list[dict] = []

    for market in markets:
        q = (market.get("question") or "")[:80]

        if _is_local_primary_market(market):
            stats["reject_filter_a"] += 1
            logger.debug("PRE-FILTER: %s score=-5 -> REJECT", q)
            continue

        if not _has_whitelisted_topic(market):
            stats["reject_filter_a"] += 1
            logger.debug("PRE-FILTER: %s score=0 -> REJECT", q)
            continue

        raw_volume = market.get("volume")
        if raw_volume is None:
            stats["reject_null_volume"] += 1
            logger.debug("PRE-FILTER: %s score=-5 -> REJECT", q)
            continue

        volume = _to_float(raw_volume)
        if volume < 500:
            stats["reject_low_volume"] += 1
            logger.debug("PRE-FILTER: %s score=-5 -> REJECT", q)
            continue

        score, herald_match = score_market_relevance(market, herald_signals)
        decision = "PASS" if score >= 4 else "REJECT"
        logger.debug("PRE-FILTER: %s score=%d -> %s", q, score, decision)
        if score < 4:
            stats["reject_score"] += 1
            continue

        m = dict(market)
        m["_prefilter_score"] = score
        m["_prefilter_herald_match"] = herald_match
        candidates.append(m)

    # Priority sort:
    # 1) HERALD match
    # 2) Crypto with real volume
    # 3) High-profile politics
    # 4) Major sports
    # 5) Everything else
    def priority_key(market: dict) -> tuple[int, int, float]:
        text = _text_blob(market)
        category = (market.get("category") or "").lower()
        score = int(market.get("_prefilter_score", 0))
        volume = _to_float(market.get("volume"))
        herald_match = bool(market.get("_prefilter_herald_match", False))

        if herald_match:
            priority = 1
        elif category == "crypto" and volume > 0:
            priority = 2
        elif any(name in text for name in ("trump", "biden", "putin", "xi", "modi", "netanyahu")):
            priority = 3
        elif any(k in text for k in MAJOR_SPORTS_KEYWORDS):
            priority = 4
        else:
            priority = 5

        # Lower priority number first; within same bucket, higher score and volume first.
        return (priority, -score, -volume)

    ordered = sorted(candidates, key=priority_key)
    stats["passed"] = len(ordered)
    return ordered, stats


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def fetch_convergence_markets(
    min_price: float = 0.82,
    max_price: float = 0.96,
    max_hours: float = 48.0,
    min_liquidity: float = 500.0,
    limit: int = 100,
) -> list:
    """Fetch markets specifically suited for late-resolution convergence."""
    from datetime import datetime, timezone
    from dateutil.parser import parse as parse_dt

    import requests

    try:
        url = "https://clob.polymarket.com/markets"
        now = datetime.now(timezone.utc)
        candidates = []

        def _extract_price(row: dict) -> float:
            tokens = row.get("tokens", [])
            yes_token = next((t for t in tokens if str(t.get("outcome", "")).upper() == "YES"), None)
            if yes_token and yes_token.get("price") not in (None, ""):
                return float(yes_token.get("price") or 0)
            if row.get("yes_price") not in (None, ""):
                return float(row.get("yes_price") or 0)

            outcomes = row.get("outcomes")
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except Exception:
                    outcomes = None

            op = row.get("outcomePrices")
            if isinstance(op, str):
                try:
                    op = json.loads(op)
                except Exception:
                    op = None

            if isinstance(op, list) and op:
                yes_idx = 0
                if isinstance(outcomes, list) and outcomes:
                    for i, outcome in enumerate(outcomes):
                        if str(outcome).upper() == "YES":
                            yes_idx = i
                            break
                if yes_idx < len(op) and op[yes_idx] not in (None, ""):
                    return float(op[yes_idx] or 0)

            return 0.0

        def _extract_liquidity(row: dict) -> float:
            return float(
                row.get("volume")
                or row.get("volumeNum")
                or row.get("liquidity")
                or row.get("liquidityNum")
                or 0
            )

        def _append_if_match(row: dict) -> None:
            price = _extract_price(row)
            if price <= 0:
                return

            end_date_str = row.get("end_date_iso") or row.get("endDateIso") or row.get("end_date") or row.get("endDate")
            if not end_date_str:
                return

            end_dt = parse_dt(str(end_date_str))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            hours_left = (end_dt - now).total_seconds() / 3600.0

            if not (float(min_price) <= price <= float(max_price)):
                return
            if hours_left < 0 or hours_left > float(max_hours):
                return

            liquidity = _extract_liquidity(row)
            if liquidity < float(min_liquidity):
                return

            market_id = (
                row.get("condition_id")
                or row.get("conditionId")
                or row.get("market_id")
                or row.get("id")
                or ""
            )

            slug = str(row.get("market_slug") or row.get("slug") or "")
            event_slug = str(row.get("event_slug") or "")
            candidates.append(
                {
                    "market_id": str(market_id),
                    "condition_id": str(market_id),
                    "id": str(market_id),
                    "question": str(row.get("question", ""))[:80],
                    "price": price,
                    "hours_left": round(hours_left, 1),
                    "liquidity": liquidity,
                    "end_date_iso": str(end_date_str),
                    "slug": slug,
                    "event_slug": event_slug,
                }
            )

        # Primary source: Gamma API — returns current active markets with future end
        # dates. CLOB sorted ascending by end_date_iso returns 2023/2024 historical
        # markets first (10,000+ records before reaching any current ones), so Gamma
        # is strictly better as the primary feed for convergence scanning.
        gamma_offset = 0
        gamma_batch = 500
        gamma_pages = 0
        total_polled = 0

        while len(candidates) < int(limit) and gamma_pages < 4:
            gamma_pages += 1
            gamma_resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": gamma_batch,
                    "offset": gamma_offset,
                },
                timeout=10,
            )
            gamma_resp.raise_for_status()
            gamma_markets = gamma_resp.json()
            if not isinstance(gamma_markets, list) or not gamma_markets:
                break
            total_polled += len(gamma_markets)
            for m in gamma_markets:
                try:
                    _append_if_match(m)
                except Exception:
                    continue
                if len(candidates) >= int(limit):
                    break
            if len(gamma_markets) < gamma_batch:
                break
            gamma_offset += gamma_batch

        logger.info(
            "[CONVERGENCE] Gamma polled=%d pages=%d filtered=%d (price=%.2f-%.2f hours<=%.0fh liq>=$%.0f)",
            total_polled, gamma_pages, len(candidates),
            float(min_price), float(max_price), float(max_hours), float(min_liquidity),
        )

        # Fallback source: CLOB /markets if Gamma yields no convergence rows.
        # Use descending order so the most recently created/expiring markets come
        # first, avoiding the thousands of 2023-era historical markets at the front
        # of ascending order. Drop the accepting_orders filter — near-expiry markets
        # often have it set to False even when orderable via the CLOB.
        if not candidates:
            next_cursor = ""
            pages = 0
            clob_polled = 0
            while len(candidates) < int(limit) and pages < 5:
                pages += 1
                params = {
                    "active": "true",
                    "closed": "false",
                    "limit": 1000,
                    "order": "end_date_iso",
                    "ascending": "true",
                }
                if next_cursor:
                    params["next_cursor"] = next_cursor

                resp = requests.get(url, params=params, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                clob_markets = data.get("data", data) if isinstance(data, dict) else data
                if not isinstance(clob_markets, list) or not clob_markets:
                    break

                clob_polled += len(clob_markets)
                for m in clob_markets:
                    if not bool(m.get("active", False)):
                        continue
                    if bool(m.get("closed", False)):
                        continue
                    try:
                        _append_if_match(m)
                    except Exception:
                        continue
                    if len(candidates) >= int(limit):
                        break

                next_cursor = ""
                if isinstance(data, dict):
                    next_cursor = str(data.get("next_cursor") or "")
                if not next_cursor:
                    break

            logger.info(
                "[CONVERGENCE] CLOB fallback polled=%d pages=%d filtered=%d",
                clob_polled, pages, len(candidates),
            )

        return sorted(candidates, key=lambda x: x["hours_left"])[: int(limit)]

    except Exception as e:
        logger.warning("fetch_convergence_markets failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Market fetching
# ---------------------------------------------------------------------------

def fetch_market_by_id(market_id: str) -> dict | None:
    """
    Fetch a single market's current state from Gamma API.
    Returns a normalized market dict or None if not found/error.
    """
    if not market_id:
        return None

    try:
        url = f"https://gamma-api.polymarket.com/markets/{market_id}"
        data = _http_get_json_with_retry(url, timeout=10, source=f"gamma_single:{market_id}")
        if not data or not isinstance(data, dict):
            return None

        # Reuse existing parsing logic
        yes_price = _parse_yes_price(data)
        if yes_price is None:
            return None

        now = datetime.now(timezone.utc)
        end_date = data.get("end_date_iso") or data.get("endDate") or data.get("end_date")
        days_remaining = 0.0
        if end_date:
            try:
                end_dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                days_remaining = (end_dt - now).total_seconds() / 86400.0
            except Exception:
                pass

        best_ask = _to_float(data.get("best_ask") or data.get("bestAsk") or 0)
        best_bid = _to_float(data.get("best_bid") or data.get("bestBid") or 0)
        spread = 0.0
        if best_ask > 0 and best_bid > 0:
            spread = (best_ask - best_bid) / best_ask

        volume = _to_float(data.get("volume") or data.get("volumeNum") or 0)
        liquidity = _to_float(data.get("liquidity") or data.get("liquidityNum") or volume)

        return {
            "id": market_id,
            "condition_id": market_id,
            "question": str(data.get("question", "")),
            "outcomes": _extract_outcomes(data),
            "yes_price": round(yes_price, 4),
            "no_price": round(1 - yes_price, 4),
            "spread": round(spread, 4),
            "volume": round(volume, 2),
            "liquidity": round(liquidity, 2),
            "days_remaining": round(days_remaining, 2),
            "days_to_resolution": round(days_remaining, 2),
            "end_date_iso": str(end_date),
            "accepting_orders": bool(data.get("accepting_orders", False)),
            "active": bool(data.get("active", True)),
            "closed": bool(data.get("closed", False)),
            "raw": data,
        }
    except Exception as exc:
        logger.warning("fetch_market_by_id failed for %s: %s", market_id, exc)
        return None


def fetch_markets(config: dict, limit: int = 100) -> list[dict]:

    """
    Fetch markets from multiple Polymarket/Gamma endpoints.
    Returns a deduplicated list of viable markets sorted by volume.
    """
    dev = config.get("dev_flags", {})

    if dev.get("mock_market_data", False):
        logger.info("[DEV] mock_market_data=true — returning hardcoded test markets")
        return _get_demo_markets()

    if not dev.get("use_polymarket_api", True):
        logger.info("[DEV] use_polymarket_api=false — skipping Polymarket API, using demo markets")
        return _get_demo_markets()

    host = config.get("polymarket", {}).get("host", "https://clob.polymarket.com")

    def _mid(row: dict) -> str:
        return str(
            row.get("id")
            or row.get("conditionId")
            or row.get("condition_id")
            or ""
        )

    def _parse_yes_price(row: dict) -> float | None:
        outcome_prices = row.get("outcomePrices")
        if isinstance(outcome_prices, list) and len(outcome_prices) >= 1:
            p = _to_float(outcome_prices[0])
            if p > 0:
                return max(0.0, min(1.0, p))

        if row.get("yes_price") is not None:
            p = _to_float(row.get("yes_price"))
            if p > 0:
                return max(0.0, min(1.0, p))

        tokens = row.get("tokens") or []
        if isinstance(tokens, list) and tokens:
            for tok in tokens:
                if str(tok.get("outcome", "")).upper() == "YES":
                    p = _to_float(tok.get("price"))
                    if p > 0:
                        return max(0.0, min(1.0, p))
            p = _to_float(tokens[0].get("price"))
            if p > 0:
                return max(0.0, min(1.0, p))

        return None

    all_markets: list[dict] = []
    seen_ids: set[str] = set()

    # Source 1: Gamma by category
    categories = ["crypto", "politics", "sports", "world"]
    for cat in categories:
        try:
            url = (
                "https://gamma-api.polymarket.com/markets"
                f"?active=true&closed=false&limit={max(limit, 100)}"
                "&order=volume&ascending=false"
                f"&category={cat}"
            )
            data = _http_get_json_with_retry(url, timeout=10, source=f"gamma:{cat}")
            if data is None:
                continue
            rows = data if isinstance(data, list) else data.get("markets", [])
            for row in rows:
                market_id = _mid(row)
                if not market_id or market_id in seen_ids:
                    continue
                seen_ids.add(market_id)
                all_markets.append(row)
        except Exception as exc:
            logger.warning("Gamma %s fetch failed: %s", cat, exc)

    # Source 2: Gamma top volume overall
    try:
        url = (
            "https://gamma-api.polymarket.com/markets"
            f"?active=true&closed=false&limit={max(limit * 2, 200)}"
            "&order=volume&ascending=false"
        )
        data = _http_get_json_with_retry(url, timeout=10, source="gamma:top")
        if data is not None:
            rows = data if isinstance(data, list) else data.get("markets", [])
            for row in rows:
                market_id = _mid(row)
                if not market_id or market_id in seen_ids:
                    continue
                seen_ids.add(market_id)
                all_markets.append(row)
    except Exception as exc:
        logger.warning("Gamma top volume fetch failed: %s", exc)

    # Source 3: sampling-markets fallback
    try:
        data = _http_get_json_with_retry(f"{host}/sampling-markets", timeout=10, source="sampling-markets")
        if data is not None:
            rows = data.get("data", []) if isinstance(data, dict) else data
            for row in rows:
                market_id = _mid(row)
                if not market_id or market_id in seen_ids:
                    continue
                seen_ids.add(market_id)
                all_markets.append(row)
    except Exception as exc:
        logger.warning("sampling-markets fetch failed: %s", exc)

    # Enrich missing volume values using Gamma map keyed by condition ID.
    try:
        gamma_volume_map = _fetch_gamma_volume_map(max_records=5000)
        for row in all_markets:
            if row.get("volume") is not None or row.get("volumeNum") is not None:
                continue
            market_id = _mid(row)
            if market_id in gamma_volume_map:
                row["volumeNum"] = gamma_volume_map[market_id]
    except Exception as exc:
        logger.warning("Gamma enrichment failed: %s", exc)

    logger.info("Total raw markets before filters: %d", len(all_markets))

    blacklist = [
        "before gta", "before grand theft",
        "before minecraft", "before fortnite",
        "before the movie", "before season",
        "number of tweets", "how many tweets",
        "before album drops", "before film",
    ]
    obscure = [
        "colombian senate", "colombian chamber",
        "will ph win", "will cd win", "will plc win",
        "nominee for co-", "nominee for nh-",
        "nominee for al-", "nominee for va-",
    ]

    viable: list[dict] = []
    now = datetime.now(timezone.utc)

    for row in all_markets:
        try:
            if not row.get("accepting_orders", False):
                continue
            if row.get("closed", False):
                continue

            volume = _to_float(row.get("volume") or row.get("volumeNum") or 0)
            if volume < 1000:
                continue

            end_date = row.get("end_date_iso") or row.get("endDate") or row.get("end_date")
            if not end_date:
                continue
            try:
                end_dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                days_remaining = (end_dt - now).total_seconds() / 86400.0
            except Exception:
                continue
            if days_remaining < 1 or days_remaining > 90:
                continue

            best_ask = _to_float(row.get("best_ask") or row.get("bestAsk") or 0)
            best_bid = _to_float(row.get("best_bid") or row.get("bestBid") or 0)
            if best_ask > 0 and best_bid > 0:
                spread = (best_ask - best_bid) / best_ask
                if spread > 0.15:
                    continue
            else:
                spread = abs(_parse_yes_price(row) + (1.0 - _parse_yes_price(row)) - 1.0)

            question = str(row.get("question", ""))
            q_lower = question.lower()
            if any(term in q_lower for term in blacklist):
                continue
            if any(term in q_lower for term in obscure):
                continue

            yes_price = _parse_yes_price(row)
            if yes_price is None:
                logger.debug("Skipping market missing usable prices: %s", question[:80])
                continue
            if 0.43 <= yes_price <= 0.57:
                continue

            outcomes = _extract_outcomes(row)
            liquidity = _to_float(row.get("liquidity") or row.get("liquidityNum") or row.get("liquidity_num") or volume)
            timestamp = (
                row.get("updatedAt")
                or row.get("updated_at")
                or row.get("createdAt")
                or row.get("created_at")
                or datetime.now(timezone.utc).isoformat()
            )

            market_id = _mid(row)
            viable.append({
                "id": market_id,
                "condition_id": market_id,
                "question": question,
                "outcomes": outcomes,
                "yes_price": round(yes_price, 4),
                "no_price": round(1 - yes_price, 4),
                "spread": round(spread, 4),
                "volume": round(volume, 2),
                "liquidity": round(liquidity, 2),
                "timestamp": str(timestamp),
                "days_remaining": round(days_remaining, 2),
                "days_to_resolution": round(days_remaining, 2),
                "end_date": str(end_date),
                "end_date_iso": str(end_date),
                "tags": row.get("tags", []),
                "category": str(row.get("category", "") or ""),
                "accepting_orders": True,
                "active": bool(row.get("active", True)),
                "closed": False,
                "raw": row,
            })
        except Exception as exc:
            logger.debug("Market parse error: %s", exc)
            continue

    viable.sort(key=lambda item: item.get("volume", 0.0), reverse=True)
    logger.info("Fetched %d viable markets from sampling-markets", len(viable))
    return viable


def _parse_market(raw: dict) -> dict | None:
    try:
        # Extract YES/NO prices from tokens array
        tokens = raw.get("tokens", [])
        yes_price = None
        no_price = None
        for tok in tokens:
            outcome = tok.get("outcome", "").upper()
            price = _to_float(tok.get("price"))
            if outcome == "YES":
                yes_price = price
            elif outcome == "NO":
                no_price = price

        outcome_prices = raw.get("outcomePrices")
        if isinstance(outcome_prices, list) and len(outcome_prices) >= 2:
            if yes_price is None:
                yes_price = _to_float(outcome_prices[0])
            if no_price is None:
                no_price = _to_float(outcome_prices[1])

        # Volume in USDC (not consistently present on CLOB payloads)
        volume_keys = ("volume", "volumeNum", "volume_num", "liquidity", "liquidityNum", "liquidity_num")
        raw_volume = None
        for key in volume_keys:
            if key in raw and raw.get(key) is not None:
                raw_volume = raw.get(key)
                break

        volume = _to_float(raw_volume)
        volume_unknown = raw_volume is None

        # Days to resolution
        end_date_iso = raw.get("endDateIso") or raw.get("end_date_iso") or raw.get("endDate") or ""
        days_to_resolution = _calc_days(end_date_iso)

        if (yes_price is None or no_price is None) and len(tokens) >= 2:
            # Not all markets use YES/NO labels; fallback to first two token prices.
            yes_price = yes_price if yes_price is not None else _to_float(tokens[0].get("price"))
            no_price = no_price if no_price is not None else _to_float(tokens[1].get("price"))

        if yes_price is None or no_price is None or yes_price <= 0 or no_price <= 0:
            return None

        # Spread
        spread = abs(yes_price + no_price - 1.0)

        outcomes = _extract_outcomes(raw)
        liquidity = _to_float(raw.get("liquidity") or raw.get("liquidityNum") or raw.get("liquidity_num") or volume)
        timestamp = (
            raw.get("updatedAt")
            or raw.get("updated_at")
            or raw.get("createdAt")
            or raw.get("created_at")
            or datetime.now(timezone.utc).isoformat()
        )

        return {
            "condition_id": raw.get("conditionId", raw.get("condition_id", "")),
            "question": raw.get("question", ""),
            "category": _infer_category(raw.get("question", ""), raw.get("category", ""), raw.get("tags", [])),
            "outcomes": outcomes,
            "yes_price": round(yes_price, 4),
            "no_price": round(no_price, 4),
            "spread": round(spread, 4),
            "volume": round(volume, 2),
            "liquidity": round(liquidity, 2),
            "timestamp": str(timestamp),
            "volume_unknown": volume_unknown,
            "tags": raw.get("tags", []),
            "days_to_resolution": days_to_resolution,
            "end_date_iso": end_date_iso,
            "created_at": raw.get("createdAt", raw.get("created_at", "")),
            "active": raw.get("active", True),
            "closed": raw.get("closed", False),
            "accepting_orders": raw.get("accepting_orders", False),
            "raw": raw,
        }
    except Exception as exc:
        logger.debug("Market parse failed: %s | %s", exc, str(raw)[:100])
        return None


def _passes_filters(market: dict, betting_cfg: dict, categories: list[str]) -> bool:
    return not _filter_failures(market, betting_cfg, categories)


def _filter_failures(market: dict, betting_cfg: dict, categories: list[str]) -> list[str]:
    min_vol = float(betting_cfg.get("min_market_volume_usdc", 500.0))
    max_spread = float(betting_cfg.get("max_market_spread", 0.15))
    min_days = float(betting_cfg.get("min_days_to_resolution", 1))

    failures: list[str] = []
    if (not market.get("active", True)) or market.get("closed", False) or (not market.get("accepting_orders", False)):
        failures.append("market_state")
    if market.get("volume_unknown", False):
        failures.append("null_volume")
        failures.append("min_volume")
    else:
        volume = market.get("volume")
        if volume is None:
            failures.append("null_volume")
            failures.append("min_volume")
        elif _to_float(volume) < min_vol:
            failures.append("min_volume")
    if market["spread"] > max_spread:
        failures.append("max_spread")
    if market["days_to_resolution"] < min_days:
        failures.append("min_days")
    if categories and market["category"] not in categories:
        failures.append("category")
    return failures


def _fetch_sampling_markets(host: str) -> list[dict]:
    raw = _http_get_json_with_retry(f"{host}/sampling-markets", timeout=15, source="sampling-markets")
    if raw is None:
        return []
    return raw if isinstance(raw, list) else raw.get("data", [])


def _fetch_gamma_volume_map(max_records: int = 5000, page_size: int = 1000) -> dict[str, float]:
    """
    Fetch active market volumes from Polymarket Gamma API.
    Returns map: condition_id -> volumeNum (float).
    """
    gamma_url = "https://gamma-api.polymarket.com/markets"
    volume_map: dict[str, float] = {}
    offset = 0

    while offset < max_records:
        params = {
            "active": "true",
            "closed": "false",
            "limit": page_size,
            "offset": offset,
        }
        batch = _http_get_json_with_retry(gamma_url, params=params, timeout=20, source="gamma-volume")
        if batch is None:
            break
        if not isinstance(batch, list) or not batch:
            break

        for row in batch:
            condition_id = row.get("conditionId") or row.get("condition_id")
            if not condition_id:
                continue
            vol = _to_float(
                row.get("volumeNum")
                or row.get("volume")
                or row.get("volumeClob")
                or row.get("liquidityNum")
                or row.get("liquidity")
            )
            if vol > 0:
                volume_map[str(condition_id)] = vol

        if len(batch) < page_size:
            break
        offset += page_size

    logger.debug("Gamma volume map size: %d", len(volume_map))
    return volume_map


def _inject_gamma_volume(items: list[dict], gamma_volume_map: dict[str, float]) -> int:
    """
    Fill missing CLOB volume fields using Gamma volumes keyed by conditionId.
    Returns number of rows enriched.
    """
    enriched = 0
    for row in items:
        has_any_volume_key = any(
            key in row and row.get(key) is not None
            for key in ("volume", "volumeNum", "volume_num", "liquidity", "liquidityNum", "liquidity_num")
        )
        if has_any_volume_key:
            continue

        condition_id = row.get("conditionId") or row.get("condition_id")
        if not condition_id:
            continue

        vol = gamma_volume_map.get(str(condition_id))
        if vol and vol > 0:
            row["volumeNum"] = vol
            enriched += 1
    return enriched


def _parse_and_filter(items: list[dict], betting_cfg: dict, categories: list[str]) -> list[dict]:
    markets: list[dict] = []

    for item in items:
        parsed = _parse_market(item)
        if not parsed:
            continue

        failures = _filter_failures(parsed, betting_cfg, categories)
        if failures:
            continue

        markets.append(parsed)

    return markets


def _calc_days(end_date_iso: str) -> float:
    if not end_date_iso:
        return 0.0
    try:
        end = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = (end - now).total_seconds() / 86400
        return round(max(delta, 0.0), 2)
    except Exception:
        return 0.0


def _infer_category(question: str, raw_cat: str, tags: list[str] | None = None) -> str:
    q = question.lower()
    raw_cat_lower = raw_cat.lower()
    tags_lower = [t.lower() for t in (tags or [])]

    crypto_terms = ["bitcoin", "btc", "eth", "ethereum", "crypto", "defi", "nft", "coin", "token", "blockchain"]
    if any(t in q for t in crypto_terms) or "crypto" in raw_cat_lower:
        return "crypto"
    if any("crypto" in t for t in tags_lower):
        return "crypto"
    if "politics" in raw_cat_lower or any("politic" in t or "election" in t for t in tags_lower):
        return "politics"
    return "other"


def _to_float(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _extract_outcomes(raw: dict) -> list[str]:
    outcomes: list[str] = []

    raw_outcomes = raw.get("outcomes")
    if isinstance(raw_outcomes, list):
        for item in raw_outcomes:
            label = str(item).strip()
            if label:
                outcomes.append(label)

    tokens = raw.get("tokens") or []
    if isinstance(tokens, list):
        for tok in tokens:
            label = str(tok.get("outcome", "")).strip()
            if label and label not in outcomes:
                outcomes.append(label)

    if outcomes:
        return outcomes

    return ["YES", "NO"]


def _http_get_json_with_retry(
    url: str,
    *,
    params: dict | None = None,
    timeout: int = 10,
    source: str = "http",
) -> dict | list | None:
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            response = _SESSION.get(url, params=params, timeout=timeout)
            if response.status_code == 200:
                return response.json()

            logger.warning(
                "%s returned status %s (attempt %d/%d)",
                source,
                response.status_code,
                attempt,
                HTTP_RETRIES,
            )
        except Exception as exc:
            logger.warning(
                "%s request failed (attempt %d/%d): %s",
                source,
                attempt,
                HTTP_RETRIES,
                exc,
            )

        if attempt < HTTP_RETRIES:
            time.sleep(HTTP_BACKOFF_SECONDS * attempt)

    return None


# ---------------------------------------------------------------------------
# Arbitrage check
# ---------------------------------------------------------------------------

def check_arbitrage(market: dict) -> bool:
    """True if YES + NO < $0.97 — guaranteed profit opportunity."""
    return (market["yes_price"] + market["no_price"]) < ARB_THRESHOLD


# ---------------------------------------------------------------------------
# Sniper opportunity detection
# ---------------------------------------------------------------------------

def find_sniper_opportunities(markets: list[dict]) -> list[dict]:
    """
    Returns markets matching low-probability sniper criteria:
      - YES price 1-15%
      - confirmed volume >= $500 and < $5,000
      - days_to_resolution >= 3 and <= 30
      - accepting_orders = True
      - market age < 24h (if created_at available)
    """
    snipers = []
    now = datetime.now(timezone.utc)
    for m in markets:
        q = m.get("question", "")[:60]
        yes = m["yes_price"]

        if not (0.01 <= yes <= SNIPER_MAX_YES_PRICE):
            logger.debug("SNIPER REJECTED: %s | reason: yes_price %.3f out of range [0.01, 0.15]", q, yes)
            continue

        if not m.get("accepting_orders", False):
            logger.debug("SNIPER REJECTED: %s | reason: not accepting_orders", q)
            continue

        # Volume: require confirmed data, enforce min AND max
        vol = m["volume"]
        if m.get("volume_unknown", False) or vol < SNIPER_MIN_VOLUME:
            logger.debug("SNIPER REJECTED: %s | reason: volume $%.0f below min $%.0f (unknown=%s)",
                         q, vol, SNIPER_MIN_VOLUME, m.get("volume_unknown"))
            continue
        if vol >= SNIPER_MAX_VOLUME:
            logger.debug("SNIPER REJECTED: %s | reason: volume $%.0f >= max $%.0f", q, vol, SNIPER_MAX_VOLUME)
            continue

        # Days: must be in [3, 30]
        days = m["days_to_resolution"]
        if days < SNIPER_MIN_DAYS:
            logger.debug("SNIPER REJECTED: %s | reason: days_to_resolution %.1f < %d", q, days, SNIPER_MIN_DAYS)
            continue
        if days > SNIPER_MAX_DAYS:
            logger.info("SNIPER REJECTED: %s | reason: days_to_resolution %.1f > %d (too long-dated)", q, days, SNIPER_MAX_DAYS)
            continue

        # Age check
        created_str = m.get("created_at", "")
        if created_str:
            try:
                created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                age_hours = (now - created).total_seconds() / 3600
                if age_hours > SNIPER_MAX_MARKET_AGE_H:
                    logger.debug("SNIPER REJECTED: %s | reason: market age %.1fh > %dh", q, age_hours, SNIPER_MAX_MARKET_AGE_H)
                    continue
            except Exception:
                pass

        logger.info("SNIPER CANDIDATE: %s | yes=%.3f | vol=$%.0f | days=%.1f", q, yes, vol, days)
        snipers.append(m)
    return snipers


# ---------------------------------------------------------------------------
# Bet execution
# ---------------------------------------------------------------------------

def execute_bet(
    market: dict,
    side: str,
    bet_usdc: float,
    config: dict,
    exit_target_price: float | None = None,
) -> dict:
    """
    Execute a bet. If paper_trading=True, simulate. Otherwise, call real CLOB API.
    Returns a result dict with success, order_id, executed_price, etc.
    """
    paper = config.get("betting", {}).get("paper_trading", True)
    if paper:
        return _paper_bet(market, side, bet_usdc, exit_target_price)
    else:
        return _live_bet(market, side, bet_usdc, config, exit_target_price)


def _paper_bet(market: dict, side: str, bet_usdc: float, exit_target_price: float | None) -> dict:
    """Simulate bet placement — no real money movement."""
    price = market["yes_price"] if side == "YES" else market["no_price"]
    contracts = round(bet_usdc / price, 4) if price > 0 else 0
    logger.info(
        "[PAPER] BET_%s | %s | $%.2f | price=%.3f | contracts=%.2f",
        side, market["question"][:60], bet_usdc, price, contracts,
    )
    return {
        "success": True,
        "paper": True,
        "order_id": f"PAPER_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
        "market_id": market["condition_id"],
        "side": side,
        "bet_usdc": bet_usdc,
        "executed_price": price,
        "contracts": contracts,
        "exit_target_price": exit_target_price,
    }


def _live_bet(market: dict, side: str, bet_usdc: float, config: dict, exit_target_price: float | None) -> dict:
    """
    Real CLOB API bet execution.
    Requires py-clob-client and web3 (uncomment in requirements.txt).
    Chain ID must be 137 (Polygon Mainnet).
    """
    try:
        # Import guarded — only available when py-clob-client is installed
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType

        poly_cfg = config.get("polymarket", {})
        chain_id = poly_cfg.get("chain_id", 137)
        if chain_id != 137:
            raise ValueError(f"Wrong chain ID {chain_id}. Must be Polygon Mainnet (137).")

        client = ClobClient(
            host=poly_cfg["host"],
            chain_id=chain_id,
            private_key=poly_cfg["private_key"],
            creds=ApiCreds(
                api_key=poly_cfg["api_key"],
                api_secret=poly_cfg["api_secret"],
                api_passphrase=poly_cfg["api_passphrase"],
            ),
        )

        token_id = _get_token_id(market, side)
        price = market["yes_price"] if side == "YES" else market["no_price"]
        order_args = OrderArgs(token_id=token_id, price=price, size=bet_usdc)
        resp = client.create_and_post_order(order_args)

        logger.info("[LIVE] BET_%s | %s | $%.2f | order=%s", side, market["question"][:50], bet_usdc, resp)
        return {
            "success": True,
            "paper": False,
            "order_id": str(resp),
            "market_id": market["condition_id"],
            "side": side,
            "bet_usdc": bet_usdc,
            "executed_price": price,
            "exit_target_price": exit_target_price,
        }
    except ImportError:
        logger.error("py-clob-client not installed. Uncomment in requirements.txt for live trading.")
        return {"success": False, "error": "py-clob-client not installed"}
    except Exception as exc:
        logger.error("Live bet execution failed: %s", exc)
        return {"success": False, "error": str(exc)}


def _get_token_id(market: dict, side: str) -> str:
    tokens = market.get("raw", {}).get("tokens", [])
    for tok in tokens:
        if tok.get("outcome", "").upper() == side:
            return tok.get("token_id", "")
    return ""


# ---------------------------------------------------------------------------
# Demo markets (used when API is unavailable / unconfigured)
# ---------------------------------------------------------------------------

def _get_demo_markets() -> list[dict]:
    """Returns synthetic markets for paper trading development/testing."""
    now = datetime.now(timezone.utc)
    return [
        {
            "condition_id": "demo_btc_100k_2025",
            "question": "Will Bitcoin reach $100,000 by end of 2025?",
            "category": "crypto",
            "yes_price": 0.62,
            "no_price": 0.38,
            "spread": 0.0,
            "volume": 125000.0,
            "days_to_resolution": 45.0,
            "end_date_iso": (now + timedelta(days=45)).isoformat(),
            "created_at": now.isoformat(),
            "active": True,
            "raw": {},
        },
        {
            "condition_id": "demo_eth_5k_2025",
            "question": "Will Ethereum reach $5,000 before June 2025?",
            "category": "crypto",
            "yes_price": 0.31,
            "no_price": 0.69,
            "spread": 0.0,
            "volume": 73000.0,
            "days_to_resolution": 90.0,
            "end_date_iso": (now + timedelta(days=90)).isoformat(),
            "created_at": now.isoformat(),
            "active": True,
            "raw": {},
        },
        {
            "condition_id": "demo_us_election_dem",
            "question": "Will the Democratic candidate win the 2026 midterms?",
            "category": "politics",
            "yes_price": 0.44,
            "no_price": 0.56,
            "spread": 0.0,
            "volume": 210000.0,
            "days_to_resolution": 240.0,
            "end_date_iso": (now + timedelta(days=240)).isoformat(),
            "created_at": now.isoformat(),
            "active": True,
            "raw": {},
        },
    ]
