"""
sentiment.py — Raw signal collection for ORACLE.

Sources (in reliability order):
  1. Binance Public API    — price, volume, RSI-14
  2. CoinCap API           — price fallback
  3. Whale Alert API       — on-chain large transaction detection
  4. StockTwits API        — bullish/bearish sentiment ratio
  5. Reddit RSS + velocity — social momentum
  6. BBC/Reuters/AP RSS    — geopolitical / politics headlines
  7. alternative.me FNG    — baseline crowd sentiment (0-100, updates hourly)
  8. Self-calculated Fear & Greed (derived from above)

DROPPED (do not re-add): CoinGecko, Google Trends / pytrends, CryptoPanic, Twitter/X API
"""

import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

logger = logging.getLogger(__name__)


def _dev(config: dict, flag: str, default: bool = True) -> bool:
    """Return the dev_flags value for `flag`, defaulting to `default` if absent."""
    return config.get("dev_flags", {}).get(flag, default) if config else default

CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.json"

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "ORACLE-SCOUT/3.0"})


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Binance
# ---------------------------------------------------------------------------

def get_binance_price_data(symbol: str = "BTCUSDT", config: dict = None) -> dict:
    """Returns price, 24h change %, volume. Falls back to CoinCap on error."""
    if not _dev(config, "use_binance", True):
        logger.info("[DEV] use_binance=false — skipping Binance, using CoinCap fallback")
        return _coincap_price_fallback(symbol, config)
    base_url = (config or {}).get("binance", {}).get("base_url", "https://api.binance.com/api/v3")
    try:
        r = _SESSION.get(f"{base_url}/ticker/24hr", params={"symbol": symbol}, timeout=8)
        r.raise_for_status()
        d = r.json()
        return {
            "symbol": symbol,
            "price": float(d["lastPrice"]),
            "price_change_pct_24h": float(d["priceChangePercent"]),
            "volume_24h": float(d["quoteVolume"]),
            "source": "binance",
        }
    except Exception as exc:
        logger.warning("Binance price fetch failed (%s), trying CoinCap fallback.", exc)
        return _coincap_price_fallback(symbol, config)


def get_binance_rsi(symbol: str = "BTCUSDT", interval: str = "1h", period: int = 14, config: dict = None) -> float | None:
    """Calculates RSI-14 from Binance kline data. Returns None on failure."""
    if not _dev(config, "use_binance", True):
        logger.info("[DEV] use_binance=false — skipping Binance RSI, returning None")
        return None
    base_url = (config or {}).get("binance", {}).get("base_url", "https://api.binance.com/api/v3")
    try:
        r = _SESSION.get(
            f"{base_url}/klines",
            params={"symbol": symbol, "interval": interval, "limit": period + 1},
            timeout=8,
        )
        r.raise_for_status()
        klines = r.json()
        closes = [float(k[4]) for k in klines]
        rsi = _calc_rsi(closes, period)
        return round(rsi, 2)
    except Exception as exc:
        logger.warning("Binance RSI fetch failed: %s", exc)
        return None


def _calc_rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _coincap_price_fallback(symbol: str, config: dict = None) -> dict:
    """CoinCap price fallback (BTC only for now)."""
    if not _dev(config, "use_coincap", True):
        logger.info("[DEV] use_coincap=false — skipping CoinCap, returning empty price data")
        return {"symbol": symbol, "price": None, "price_change_pct_24h": 0.0, "volume_24h": 0.0, "source": "none"}
    base_url = (config or {}).get("coincap", {}).get("base_url", "https://api.coincap.io/v2")
    coin_map = {"BTCUSDT": "bitcoin", "ETHUSDT": "ethereum"}
    coin_id = coin_map.get(symbol, "bitcoin")
    try:
        r = _SESSION.get(f"{base_url}/assets/{coin_id}", timeout=8)
        r.raise_for_status()
        d = r.json()["data"]
        return {
            "symbol": symbol,
            "price": float(d["priceUsd"]),
            "price_change_pct_24h": float(d.get("changePercent24Hr") or 0),
            "volume_24h": float(d.get("volumeUsd24Hr") or 0),
            "source": "coincap",
        }
    except Exception as exc:
        logger.error("CoinCap fallback also failed: %s", exc)
        return {"symbol": symbol, "price": None, "price_change_pct_24h": 0.0, "volume_24h": 0.0, "source": "none"}


# ---------------------------------------------------------------------------
# Whale Alert
# ---------------------------------------------------------------------------

_WHALE_ASSET_MAP = {
    "BTCUSDT": ["bitcoin", "btc", "xbt"],
    "ETHUSDT": ["ethereum", "eth"],
}


def get_whale_alert_signals(symbol: str = "BTCUSDT", config: dict = None) -> dict:
    """
    Fetch recent large transactions (>= min_transaction_usdc) from Whale Alert API.
    Returns whale_detected bool, transaction count, total USD value, and a
    confidence_boost int (+10 when a relevant whale tx is found).

    Free tier: up to 10 requests/min, results updated every ~30s.
    """
    _empty = {"enabled": False, "whale_detected": False, "tx_count": 0,
              "total_usd": 0.0, "confidence_boost": 0}

    if not _dev(config, "use_whale_alert", True):
        logger.info("[DEV] use_whale_alert=false — skipping Whale Alert")
        return _empty

    cfg = (config or {}).get("whale_alert", {})
    if not cfg.get("enabled", True):
        return _empty

    api_key = cfg.get("api_key", "")
    if not api_key:
        logger.debug("Whale Alert API key not configured, skipping.")
        return _empty

    min_value = cfg.get("min_transaction_usdc", 500_000)
    base_url = cfg.get("base_url", "https://api.whale-alert.io/v1")
    since = int(time.time()) - 300  # last 5 minutes

    relevant_symbols = _WHALE_ASSET_MAP.get(symbol, [])

    try:
        r = _SESSION.get(
            f"{base_url}/transactions",
            params={"api_key": api_key, "min_value": min_value, "start": since},
            timeout=10,
        )
        r.raise_for_status()
        txs = r.json().get("transactions", [])

        relevant = [
            t for t in txs
            if t.get("symbol", "").lower() in relevant_symbols
        ]
        total_usd = sum(t.get("amount_usd", 0) for t in relevant)
        whale_detected = len(relevant) > 0

        logger.info(
            "Whale Alert: %d relevant txs for %s, total $%.0f",
            len(relevant), symbol, total_usd,
        )
        return {
            "enabled": True,
            "whale_detected": whale_detected,
            "tx_count": len(relevant),
            "total_usd": round(total_usd, 2),
            "confidence_boost": 10 if whale_detected else 0,
        }
    except Exception as exc:
        logger.warning("Whale Alert fetch failed: %s", exc)
        return _empty


# ---------------------------------------------------------------------------
# alternative.me Fear & Greed Index
# ---------------------------------------------------------------------------

def get_alternative_fear_and_greed(config: dict = None) -> dict:
    """
    Fetch the current Fear & Greed index from alternative.me.
    No API key required. Updates approximately hourly.

    Returns score (0-100) and label. Used as baseline sentiment context:
      Extreme Fear (<25):  bearish signal
      Extreme Greed (>75): contrarian bearish signal
    """
    _empty = {"enabled": False, "score": 50, "label": "Neutral"}

    if not _dev(config, "use_fear_greed_api", True):
        logger.info("[DEV] use_fear_greed_api=false — returning neutral FNG")
        return _empty

    try:
        r = _SESSION.get(
            "https://api.alternative.me/fng/",
            params={"limit": 1},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json().get("data", [{}])[0]
        score = int(data.get("value", 50))
        label = data.get("value_classification", "Neutral")
        return {"enabled": True, "score": score, "label": label}
    except Exception as exc:
        logger.warning("alternative.me FNG fetch failed: %s", exc)
        return _empty


# ---------------------------------------------------------------------------
# StockTwits Sentiment
# ---------------------------------------------------------------------------

def get_stocktwits_sentiment(symbol: str = "BTC.X", config: dict = None) -> dict:
    """
    Fetch bullish/bearish sentiment ratio from StockTwits.
    No API key required.
    
    Returns sentiment dict with:
      - bullish_pct: percentage of bullish messages
      - bearish_pct: percentage of bearish messages
      - signal: "panic" if bearish > 70% (sniper opportunity)
      - message_count: total messages analyzed
    
    Monitored symbols: BTC.X, ETH.X, SOL.X, XRP.X
    """
    _empty = {
        "symbol": symbol,
        "bullish_pct": 50,
        "bearish_pct": 50,
        "signal": "neutral",
        "message_count": 0,
        "source": "none",
    }
    
    if not _dev(config, "use_stocktwits", True):
        logger.info("[DEV] use_stocktwits=false — skipping StockTwits")
        return _empty
    
    base_url = (config or {}).get("stocktwits", {}).get("base_url", "https://api.stocktwits.com/api/2")
    
    try:
        r = _SESSION.get(
            f"{base_url}/streams/symbol/{symbol}.json",
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        
        messages = data.get("messages", [])
        if not messages:
            return _empty
        
        bullish_count = sum(1 for msg in messages if msg.get("sentiment") == "bullish")
        bearish_count = sum(1 for msg in messages if msg.get("sentiment") == "bearish")
        total = len(messages)
        
        bullish_pct = round((bullish_count / total) * 100, 1) if total > 0 else 50
        bearish_pct = round((bearish_count / total) * 100, 1) if total > 0 else 50
        
        # Panic signal when bearish > 70% (sniper opportunity)
        signal = "panic" if bearish_pct > 70 else "neutral"
        
        logger.info(
            "StockTwits %s: %d messages, %.1f%% bullish, %.1f%% bearish [%s]",
            symbol, total, bullish_pct, bearish_pct, signal,
        )
        
        return {
            "symbol": symbol,
            "bullish_pct": bullish_pct,
            "bearish_pct": bearish_pct,
            "signal": signal,
            "message_count": total,
            "source": "stocktwits",
        }
    except Exception as exc:
        logger.warning("StockTwits fetch failed for %s: %s", symbol, exc)
        return _empty


# ---------------------------------------------------------------------------
# CryptoPanic crowd sentiment
# ---------------------------------------------------------------------------

def get_cryptopanic_signals(config: dict = None) -> dict:
    """
    Fetch latest crypto news + crowd vote sentiment from CryptoPanic API.
    Requires free auth_token from cryptopanic.com/api/v1.
    Returns bullish_ratio (0-1), article_count, and recent titles.
    Gracefully returns empty signals if token not configured.
    """
    _empty = {
        "enabled": False,
        "bullish_ratio": 0.5,
        "bearish_ratio": 0.5,
        "article_count": 0,
        "recent_titles": [],
    }

    if not _dev(config, "use_cryptopanic", True):
        logger.info("[DEV] use_cryptopanic=false — skipping CryptoPanic")
        return _empty

    cfg = (config or {}).get("cryptopanic", {})
    if not cfg.get("enabled", True):
        return _empty

    token = cfg.get("auth_token", "")
    if not token:
        logger.debug("CryptoPanic auth_token not configured, skipping.")
        return _empty

    base_url = cfg.get("base_url", "https://cryptopanic.com/api/v1")
    try:
        r = _SESSION.get(
            f"{base_url}/posts/",
            params={"auth_token": token, "filter": "hot", "public": "true"},
            timeout=10,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return _empty

        bullish = sum(1 for p in results if p.get("votes", {}).get("positive", 0) >
                     p.get("votes", {}).get("negative", 0))
        bearish = sum(1 for p in results if p.get("votes", {}).get("negative", 0) >
                     p.get("votes", {}).get("positive", 0))
        total = len(results)
        bullish_ratio = round(bullish / total, 3) if total > 0 else 0.5
        bearish_ratio = round(bearish / total, 3) if total > 0 else 0.5
        titles = [p.get("title", "") for p in results[:8] if p.get("title")]

        logger.info(
            "CryptoPanic: %d posts, bullish=%.2f bearish=%.2f",
            total, bullish_ratio, bearish_ratio,
        )
        return {
            "enabled": True,
            "bullish_ratio": bullish_ratio,
            "bearish_ratio": bearish_ratio,
            "article_count": total,
            "recent_titles": titles,
        }
    except Exception as exc:
        logger.warning("CryptoPanic fetch failed: %s", exc)
        return _empty


# ---------------------------------------------------------------------------
# Nitter RSS headlines (breaking news from key crypto/news accounts)
# ---------------------------------------------------------------------------

def get_nitter_headlines(config: dict = None, max_per_account: int = 3) -> list[str]:
    """
    Fetch recent posts from Nitter RSS feeds for configured accounts.
    Returns a list of headline strings (up to max_per_account × account count).
    Falls back through nitter instances on failure.
    """
    if not _dev(config, "use_nitter", True):
        logger.info("[DEV] use_nitter=false — skipping Nitter RSS")
        return []

    herald_cfg = (config or {}).get("herald", {})
    accounts: list[str] = herald_cfg.get("nitter_accounts", [])
    if not accounts:
        return []

    nitter_instances = [
        "https://nitter.poast.org",
        "https://nitter.privacydev.net",
        "https://nitter.tiekoetter.com",
    ]
    headlines: list[str] = []

    for account in accounts[:6]:  # cap to 6 accounts to avoid slowing signal collection
        for instance in nitter_instances:
            url = f"{instance}/{account}/rss"
            try:
                feed = feedparser.parse(url, request_headers={"User-Agent": "ORACLE-SCOUT/3.0"})
                for entry in feed.entries[:max_per_account]:
                    title = entry.get("title", "").strip()
                    if title:
                        headlines.append(f"@{account}: {title}")
                break  # success — don't try other instances
            except Exception as exc:
                logger.debug("Nitter fetch failed (%s/%s): %s", instance, account, exc)

    return headlines


# ---------------------------------------------------------------------------
# Twitter / X  [DEPRECATED — replaced with StockTwits]
# ---------------------------------------------------------------------------

def get_market_velocity(query: str, config: dict = None) -> dict:
    """Returns post count as velocity proxy, sourced from Reddit RSS (primary source)."""
    cfg = (config or {}).get("reddit", {})
    all_feeds: list[str] = []
    for feeds in cfg.get("feeds", {}).values():
        all_feeds.extend(feeds)
    window_hours = cfg.get("velocity_window_hours", 24)
    max_posts = cfg.get("max_posts_per_feed", 25)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    q_lower = query.lower()
    count = 0
    try:
        for url in all_feeds:
            try:
                feed = feedparser.parse(url, request_headers={"User-Agent": "ORACLE/3.0"})
                for entry in feed.entries[:max_posts]:
                    title = (entry.get("title") or "").lower()
                    if q_lower in title:
                        pub = _parse_entry_time(entry)
                        if pub and pub >= cutoff:
                            count += 1
            except Exception as exc:
                logger.debug("Reddit feed failed (%s): %s", url, exc)
        return {"source": "reddit", "query": query, "post_count_1h": count}
    except Exception as exc:
        logger.warning("Market velocity fetch failed: %s", exc)
        return {"source": "none", "query": query, "post_count_1h": 0}



def _parse_entry_time(entry) -> datetime | None:
    try:
        import calendar
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            return datetime.fromtimestamp(calendar.timegm(entry.published_parsed), tz=timezone.utc)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Reddit velocity (standalone)
# ---------------------------------------------------------------------------

def get_reddit_velocity(subreddits: list[str] | None = None, config: dict = None) -> dict:
    """Count posts in last 24h across crypto/politics subreddits."""
    if not _dev(config, "use_reddit", True):
        logger.info("[DEV] use_reddit=false — skipping Reddit RSS, returning 0 posts")
        return {"post_count_24h": 0}
    cfg = (config or {}).get("reddit", {})
    all_feeds: list[str] = []
    if subreddits:
        all_feeds = [f"https://www.reddit.com/r/{s}/.rss" for s in subreddits]
    else:
        for feeds in cfg.get("feeds", {}).values():
            all_feeds.extend(feeds)
    window_hours = cfg.get("velocity_window_hours", 24)
    max_posts = cfg.get("max_posts_per_feed", 25)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    post_count = 0
    for url in all_feeds:
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "ORACLE/3.0"})
            for entry in feed.entries[:max_posts]:
                pub = _parse_entry_time(entry)
                if pub and pub >= cutoff:
                    post_count += 1
        except Exception as exc:
            logger.debug("Reddit velocity failed (%s): %s", url, exc)
    return {"post_count_24h": post_count}


# ---------------------------------------------------------------------------
# RSS headlines (geopolitical + politics)
# ---------------------------------------------------------------------------

def get_rss_headlines(feeds: list[str] | None = None, max_per_feed: int = 5, config: dict = None) -> list[str]:
    """Fetch recent headlines from geopolitical/politics RSS feeds."""
    if not _dev(config, "use_news_rss", True):
        logger.info("[DEV] use_news_rss=false — skipping news RSS headlines")
        return []
    if not feeds:
        feeds = [
            "https://feeds.reuters.com/reuters/worldNews",
            "https://feeds.bbci.co.uk/news/world/rss.xml",
            "https://feeds.reuters.com/reuters/politicsNews",
        ]
    headlines = []
    for url in feeds:
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "ORACLE/3.0"})
            for entry in feed.entries[:max_per_feed]:
                headlines.append(entry.get("title", ""))
        except Exception as exc:
            logger.debug("RSS headline fetch failed (%s): %s", url, exc)
    return [h for h in headlines if h]


# ---------------------------------------------------------------------------
# Self-calculated Fear & Greed
# ---------------------------------------------------------------------------

def calc_fear_and_greed(
    price_change_pct: float,
    rsi: float | None,
    reddit_post_count: int,
    fng_score: float = 50.0,
) -> dict:
    """
    Weights:
      price momentum (BTC 24h change): 30%
      RSI-14:                          25%
      Reddit velocity:                 25%
      alternative.me FNG score:        20%
    Score 0-100 → label.
    """
    # Price momentum component (30%)
    # map -10%..+10% change → 0..100
    price_score = max(0.0, min(100.0, 50.0 + price_change_pct * 5))

    # RSI component (25%) — already 0-100, higher RSI = more greed
    rsi_score = rsi if rsi is not None else 50.0

    # Reddit velocity component (25%)
    # Normalise: 0 posts = 0, 100+ posts = 100
    reddit_score = min(100.0, reddit_post_count)

    # alternative.me FNG component (20%) — already 0-100
    fng_component = float(fng_score)

    composite = (
        price_score * 0.30
        + rsi_score * 0.25
        + reddit_score * 0.25
        + fng_component * 0.20
    )
    composite = round(composite, 1)

    if composite >= 75:
        label = "Extreme Greed"
    elif composite >= 55:
        label = "Greed"
    elif composite >= 45:
        label = "Neutral"
    elif composite >= 25:
        label = "Fear"
    else:
        label = "Extreme Fear"

    return {"score": composite, "label": label}


# ---------------------------------------------------------------------------
# Aggregate signal collector
# ---------------------------------------------------------------------------

def collect_signals(market: dict, config: dict, herald_boost: int = 0) -> dict:
    """
    Collect all raw signals for a given market dict (from Polymarket).
    Returns a flat signals dict ready to pass to SCOUT.
    """
    cfg = config
    category = market.get("category", "crypto").lower()

    # Binance price / RSI (primarily for crypto; use BTC as proxy for others)
    symbol = "BTCUSDT"
    if "eth" in market.get("question", "").lower():
        symbol = "ETHUSDT"

    price_data = get_binance_price_data(symbol, cfg)
    rsi = get_binance_rsi(symbol, config=cfg)

    # Reddit velocity
    reddit_data = get_reddit_velocity(config=cfg)
    reddit_count = reddit_data.get("post_count_24h", 0)

    # Whale Alert (crypto markets only)
    whale_data = get_whale_alert_signals(symbol, cfg) if category == "crypto" else \
        {"enabled": False, "whale_detected": False, "tx_count": 0, "total_usd": 0.0, "confidence_boost": 0}

    # alternative.me Fear & Greed (crypto baseline)
    fng_data = get_alternative_fear_and_greed(cfg) if category == "crypto" else \
        {"enabled": False, "score": 50, "label": "Neutral"}

    # StockTwits sentiment (crypto markets only)
    stocktwits_data = {}
    if category == "crypto":
        stw_symbol = "BTC.X"
        if "eth" in market.get("question", "").lower():
            stw_symbol = "ETH.X"
        elif "sol" in market.get("question", "").lower():
            stw_symbol = "SOL.X"
        elif "xrp" in market.get("question", "").lower():
            stw_symbol = "XRP.X"
        stocktwits_data = get_stocktwits_sentiment(stw_symbol, cfg)
    else:
        stocktwits_data = {
            "symbol": "",
            "bullish_pct": 50,
            "bearish_pct": 50,
            "signal": "neutral",
            "message_count": 0,
            "source": "none",
        }

    # Market velocity (Reddit-sourced)
    question_words = market.get("question", "")[:50]
    market_velocity = get_market_velocity(question_words, cfg)

    # Self-calculated Fear & Greed (blends price, RSI, reddit, FNG)
    fg = calc_fear_and_greed(
        price_change_pct=price_data.get("price_change_pct_24h", 0.0),
        rsi=rsi,
        reddit_post_count=reddit_count,
        fng_score=fng_data.get("score", 50.0),
    )

    # CryptoPanic crowd sentiment (crypto markets only)
    cryptopanic_data = get_cryptopanic_signals(cfg) if category == "crypto" else {
        "enabled": False, "bullish_ratio": 0.5, "bearish_ratio": 0.5,
        "article_count": 0, "recent_titles": [],
    }

    # Nitter headlines (breaking news from key accounts)
    nitter_headlines = get_nitter_headlines(cfg)

    # RSS headlines (geopolitical focus for politics markets)
    if category == "politics":
        headline_feeds = cfg.get("herald", {}).get("feeds", {}).get("politics", [])[:3]
    else:
        headline_feeds = cfg.get("herald", {}).get("feeds", {}).get("crypto", [])[:3]
    rss_headlines = get_rss_headlines(feeds=headline_feeds, config=cfg)

    # Merge Nitter + RSS headlines (Nitter first as more real-time)
    all_headlines = (nitter_headlines + rss_headlines)[:12]

    # Combine herald boost + whale alert boost
    total_boost = herald_boost + whale_data.get("confidence_boost", 0)

    return {
        "market_id": market.get("condition_id", market.get("id", "")),
        "market_question": market.get("question", ""),
        "category": category,
        "polymarket_yes_price": market.get("yes_price", 0.5),
        "polymarket_no_price": market.get("no_price", 0.5),
        "market_volume_usdc": market.get("volume", 0.0),
        "days_to_resolution": market.get("days_to_resolution", 0),
        "btc_price": price_data.get("price"),
        "btc_price_change_pct_24h": price_data.get("price_change_pct_24h", 0.0),
        "btc_volume_24h": price_data.get("volume_24h", 0.0),
        "rsi_14": rsi,
        "fear_greed_score": fg["score"],
        "fear_greed_label": fg["label"],
        "fng_api_score": fng_data.get("score", 50),
        "fng_api_label": fng_data.get("label", "Neutral"),
        "whale_detected": whale_data.get("whale_detected", False),
        "whale_tx_count": whale_data.get("tx_count", 0),
        "whale_total_usd": whale_data.get("total_usd", 0.0),
        "stocktwits_symbol": stocktwits_data.get("symbol", ""),
        "stocktwits_bullish_pct": stocktwits_data.get("bullish_pct", 50),
        "stocktwits_bearish_pct": stocktwits_data.get("bearish_pct", 50),
        "stocktwits_signal": stocktwits_data.get("signal", "neutral"),
        "stocktwits_message_count": stocktwits_data.get("message_count", 0),
        "cryptopanic_bullish_ratio": cryptopanic_data.get("bullish_ratio", 0.5),
        "cryptopanic_article_count": cryptopanic_data.get("article_count", 0),
        "cryptopanic_recent_titles": cryptopanic_data.get("recent_titles", []),
        "market_velocity_count": market_velocity.get("post_count_1h", 0),
        "market_velocity_source": market_velocity.get("source", "none"),
        "reddit_post_count_24h": reddit_count,
        "nitter_headlines": nitter_headlines[:6],
        "rss_headlines": all_headlines[:10],
        "herald_boost": herald_boost,
        "herald_active": herald_boost > 0,
        "total_confidence_boost": total_boost,
    }
