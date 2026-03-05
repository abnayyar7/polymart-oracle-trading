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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config" / "config.json"

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "ORACLE/3.0", "Accept": "application/json"})

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


# ---------------------------------------------------------------------------
# Market fetching
# ---------------------------------------------------------------------------

def fetch_markets(config: dict, limit: int = 50) -> list[dict]:
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

    def _parse_yes_price(row: dict) -> float:
        outcome_prices = row.get("outcomePrices")
        if isinstance(outcome_prices, list) and outcome_prices:
            return max(0.0, min(1.0, _to_float(outcome_prices[0])))

        if row.get("yes_price") is not None:
            return max(0.0, min(1.0, _to_float(row.get("yes_price"))))

        tokens = row.get("tokens") or []
        if isinstance(tokens, list) and tokens:
            for tok in tokens:
                if str(tok.get("outcome", "")).upper() == "YES":
                    return max(0.0, min(1.0, _to_float(tok.get("price"))))
            return max(0.0, min(1.0, _to_float(tokens[0].get("price"))))

        return 0.5

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
            r = _SESSION.get(url, timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
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
        r = _SESSION.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
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
        r = _SESSION.get(f"{host}/sampling-markets", timeout=10)
        if r.status_code == 200:
            data = r.json()
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
            if 0.43 <= yes_price <= 0.57:
                continue

            market_id = _mid(row)
            viable.append({
                "id": market_id,
                "condition_id": market_id,
                "question": question,
                "yes_price": round(yes_price, 4),
                "no_price": round(1 - yes_price, 4),
                "spread": round(spread, 4),
                "volume": round(volume, 2),
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
        yes_price = no_price = 0.5
        for tok in tokens:
            outcome = tok.get("outcome", "").upper()
            price = float(tok.get("price", 0.5))
            if outcome == "YES":
                yes_price = price
            elif outcome == "NO":
                no_price = price

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

        if (yes_price == 0.5 and no_price == 0.5) and len(tokens) >= 2:
            # Not all markets use YES/NO labels; fallback to first two token prices.
            yes_price = _to_float(tokens[0].get("price")) or 0.5
            no_price = _to_float(tokens[1].get("price")) or 0.5

        # Spread
        spread = abs(yes_price + no_price - 1.0)

        return {
            "condition_id": raw.get("conditionId", raw.get("condition_id", "")),
            "question": raw.get("question", ""),
            "category": _infer_category(raw.get("question", ""), raw.get("category", ""), raw.get("tags", [])),
            "yes_price": round(yes_price, 4),
            "no_price": round(no_price, 4),
            "spread": round(spread, 4),
            "volume": round(volume, 2),
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
    r = _SESSION.get(f"{host}/sampling-markets", timeout=15)
    r.raise_for_status()
    raw = r.json()
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
        r = _SESSION.get(gamma_url, params=params, timeout=20)
        r.raise_for_status()
        batch = r.json()
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
