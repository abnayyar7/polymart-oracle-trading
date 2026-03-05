"""
market_matcher.py — Match HERALD breaking signals to Polymarket markets.

Given a HERALD signal keyword, scores all fetched open markets by relevance
using exact keyword matching, category alignment, and fuzzy expansion terms.
Returns top N most relevant markets for priority processing by SCOUT/APEX.

Matching algorithm (score 0-100):
  1. Exact keyword match in market question  → +30 pts
  2. Category alignment (keyword → category) → +15 pts
  3. Each expansion term match               → +5 pts each
"""

import logging
import re

logger = logging.getLogger(__name__)


# Maps a HERALD keyword → related terms likely to appear in Polymarket question text
KEYWORD_EXPANSIONS: dict[str, list[str]] = {
    # Geopolitical
    "election":      ["voting", "president", "candidate", "poll", "vote", "ballot",
                      "primary", "senator", "congress", "midterm", "runoff"],
    "war":           ["conflict", "military", "invasion", "attack", "ceasefire",
                      "troops", "nato", "ukraine", "russia", "offensive"],
    "coup":          ["government", "military", "overthrow", "junta", "regime", "takeover"],
    "sanctions":     ["russia", "iran", "china", "trade", "embargo", "tariff", "export"],
    "assassination": ["killed", "attack", "leader", "president", "prime minister", "shot"],
    "ceasefire":     ["peace", "war", "conflict", "truce", "agreement", "deal"],
    "nuclear":       ["missile", "iran", "north korea", "weapon", "bomb", "warhead"],
    "summit":        ["g7", "g20", "meeting", "agreement", "deal", "nato", "un"],
    "impeachment":   ["congress", "senate", "president", "trial", "removal", "vote"],
    "crisis":        ["emergency", "collapse", "default", "debt", "financial"],
    "protest":       ["demonstration", "uprising", "rally", "riot", "march"],
    # Crypto
    "ban":           ["regulation", "sec", "legislation", "restrict", "prohibit", "illegal"],
    "bitcoin":       ["btc", "crypto", "cryptocurrency", "digital asset", "blockchain"],
    "etf":           ["fund", "sec", "approval", "bitcoin etf", "crypto etf", "spot"],
    "sec":           ["regulation", "lawsuit", "enforcement", "compliance", "gensler"],
    "hack":          ["exploit", "breach", "stolen", "attack", "defi", "protocol", "vulnerability"],
    "regulation":    ["law", "policy", "congress", "sec", "cftc", "bill", "legislation"],
    "crash":         ["price", "bear", "dump", "correction", "fall", "drop", "collapse"],
    "halving":       ["bitcoin", "btc", "supply", "mining", "reward", "block"],
    "defi":          ["protocol", "hack", "tvl", "yield", "staking", "liquidity"],
    "stablecoin":    ["usdc", "usdt", "tether", "peg", "depeg", "circle"],
    "liquidation":   ["margin", "futures", "leverage", "forced", "cascade"],
    "whale":         ["large", "holder", "transfer", "wallet", "accumulation"],
    "adoption":      ["institutional", "corporate", "treasury", "etf", "mainstream"],
    "institutional": ["blackrock", "fidelity", "pension", "fund", "treasury", "corporate"],
    # Sports
    "championship":  ["nba", "nfl", "mlb", "nhl", "finals", "playoffs", "title", "world series"],
    "tournament":    ["grand slam", "world cup", "open", "championship", "wimbledon", "masters"],
    "world cup":     ["soccer", "football", "fifa", "qualifying", "final", "group stage"],
    "super bowl":    ["nfl", "football", "championship", "afc", "nfc"],
    "olympic":       ["gold", "medal", "paris", "games", "athlete", "podium"],
    "playoffs":      ["nba", "nfl", "mlb", "nhl", "series", "conference", "bracket"],
    "injury":        ["out", "return", "surgery", "season", "game", "player"],
    "transfer":      ["trade", "sign", "contract", "team", "player", "deal"],
    # Entertainment
    "oscar":         ["academy award", "best picture", "film", "movie", "nominated"],
    "emmy":          ["television", "series", "show", "nominated", "award", "streaming"],
    "grammy":        ["music", "album", "artist", "award", "nominated", "record"],
    "box office":    ["film", "movie", "opening", "weekend", "billion", "record"],
    "streaming":     ["netflix", "disney", "hbo", "subscribers", "platform", "show"],
    # Science / Tech
    "fda":           ["drug", "approval", "clinical", "therapy", "pharmaceutical", "vaccine"],
    "pandemic":      ["outbreak", "virus", "covid", "disease", "health", "WHO"],
    "vaccine":       ["fda", "approval", "clinical", "trial", "immunity", "booster"],
    "launch":        ["rocket", "spacex", "nasa", "satellite", "mission", "orbit"],
    "ai":            ["artificial intelligence", "openai", "model", "gpt", "regulation", "chatgpt"],
    "climate":       ["emissions", "carbon", "paris", "temperature", "warming", "COP"],
    "earthquake":    ["disaster", "magnitude", "richter", "tsunami", "damage"],
    "hurricane":     ["storm", "disaster", "category", "landfall", "evacuation"],
    "outbreak":      ["virus", "pandemic", "disease", "WHO", "health", "spread"],
}

# Maps HERALD keyword → Polymarket category labels likely to contain relevant markets
KEYWORD_TO_CATEGORIES: dict[str, list[str]] = {
    "election":      ["politics"],
    "war":           ["politics"],
    "coup":          ["politics"],
    "sanctions":     ["politics"],
    "assassination": ["politics"],
    "ceasefire":     ["politics"],
    "nuclear":       ["politics"],
    "summit":        ["politics"],
    "impeachment":   ["politics"],
    "crisis":        ["politics", "crypto"],
    "protest":       ["politics"],
    "ban":           ["crypto", "politics"],
    "bitcoin":       ["crypto"],
    "etf":           ["crypto"],
    "sec":           ["crypto", "politics"],
    "hack":          ["crypto"],
    "regulation":    ["crypto", "politics"],
    "crash":         ["crypto"],
    "halving":       ["crypto"],
    "defi":          ["crypto"],
    "stablecoin":    ["crypto"],
    "liquidation":   ["crypto"],
    "whale":         ["crypto"],
    "adoption":      ["crypto"],
    "institutional": ["crypto"],
    "championship":  ["sports"],
    "tournament":    ["sports"],
    "world cup":     ["sports"],
    "super bowl":    ["sports"],
    "olympic":       ["sports"],
    "playoffs":      ["sports"],
    "injury":        ["sports"],
    "transfer":      ["sports"],
    "oscar":         ["entertainment"],
    "emmy":          ["entertainment"],
    "grammy":        ["entertainment"],
    "box office":    ["entertainment"],
    "streaming":     ["entertainment"],
    "fda":           ["science"],
    "pandemic":      ["science"],
    "vaccine":       ["science"],
    "launch":        ["science"],
    "ai":            ["science", "politics"],
    "climate":       ["science", "politics"],
    "earthquake":    ["science"],
    "hurricane":     ["science"],
    "outbreak":      ["science"],
}


def _score_market(market: dict, keyword: str, expansions: list[str], categories: list[str]) -> int:
    """Score a single market 0-100 for relevance to a HERALD signal keyword."""
    question = market.get("question", "").lower()
    score = 0

    # Exact keyword match in question text
    if re.search(rf"\b{re.escape(keyword.lower())}\b", question):
        score += 30

    # Category alignment
    market_cat = market.get("category", "").lower()
    if market_cat and market_cat in categories:
        score += 15

    # Fuzzy expansion term matches
    for term in expansions:
        if re.search(rf"\b{re.escape(term.lower())}\b", question):
            score += 5

    return min(score, 100)


def match_markets_to_signal(
    signal: dict,
    all_markets: list[dict],
    top_n: int = 3,
    min_score: int = 5,
) -> list[dict]:
    """
    Score all markets against a HERALD signal keyword and return the top_n most relevant.

    Args:
        signal:      HERALD signal dict (must have 'keyword' key).
        all_markets: Full list of markets returned by fetch_markets().
        top_n:       Maximum number of matched markets to return.
        min_score:   Minimum score threshold; markets below this are ignored.

    Returns:
        List of market dicts (sorted by score desc), each with '_herald_score' added.
        Empty list if no markets score >= min_score.
    """
    keyword = signal.get("keyword", "").strip()
    if not keyword or not all_markets:
        return []

    kw_lower = keyword.lower()
    expansions = KEYWORD_EXPANSIONS.get(kw_lower, [])
    categories = KEYWORD_TO_CATEGORIES.get(kw_lower, [])

    scored: list[dict] = []
    for market in all_markets:
        score = _score_market(market, kw_lower, expansions, categories)
        if score >= min_score:
            m = dict(market)
            m["_herald_score"] = score
            scored.append(m)

    if not scored:
        logger.info("HERALD match: no markets found for keyword '%s'", keyword)
        return []

    scored.sort(key=lambda m: m["_herald_score"], reverse=True)
    top = scored[:top_n]
    logger.info(
        "HERALD match: keyword='%s' → %d markets matched (top %d): %s",
        keyword,
        len(scored),
        len(top),
        [m.get("question", "")[:45] for m in top],
    )
    return top
