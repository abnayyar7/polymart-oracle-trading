"""
HERALD — Breaking news velocity monitor.
Polls RSS feeds every 60 seconds. Fires confidence boost (+5% to +25%)
when 3+ articles spike on the same topic within a 10-minute window.

RSS feeds monitored (configured in config.json herald.feeds):
  Crypto:       CoinDesk, CoinTelegraph, Decrypt, Bitcoin Magazine, The Block
  Geopolitical: Reuters World, BBC World, Al Jazeera, NYT World
  Politics:     NYT Politics, Reuters Politics, BBC Politics
  Reddit:       r/cryptocurrency, r/bitcoin, r/CryptoMarkets (via sentiment.py)

Twitter accounts monitored (via Twitter API when use_twitter=true in dev_flags):
  @whale_alert, @lookonchain, @coindesk, @cointelegraph, @binance, @VitalikButerin
  Configured under herald.twitter_accounts in config.json.
  Actual polling is handled by sentiment.get_twitter_velocity().
"""

import json
import logging
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.json"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


class HeraldAgent:
    def __init__(self, config: dict):
        self.cfg = config.get("herald", {})
        self.enabled = self.cfg.get("enabled", True)
        self.check_interval = self.cfg.get("check_interval_seconds", 60)
        self.velocity_window = self.cfg.get("velocity_window_minutes", 10)
        self.velocity_threshold = self.cfg.get("velocity_threshold_articles", 3)
        self.signal_cooldown_minutes = self.cfg.get("signal_cooldown_minutes", self.velocity_window)
        self.geo_keywords = [kw.lower() for kw in self.cfg.get("geopolitical_keywords", [])]
        self.crypto_keywords = [kw.lower() for kw in self.cfg.get("crypto_keywords", [])]
        self.feeds = self.cfg.get("feeds", {})
        # Twitter accounts monitored via sentiment.get_twitter_velocity() when Twitter API is active
        self.twitter_accounts: list[str] = self.cfg.get("twitter_accounts", [])

        # {keyword: [(timestamp, title, source), ...]}
        self._article_log: dict[str, list[tuple]] = defaultdict(list)
        # Deduplicate feed items across polling cycles.
        # key -> seen_at datetime
        self._seen_entries: dict[str, datetime] = {}
        # Cooldown tracking to avoid repeated notifications for same keyword.
        self._last_signal_fired_at: dict[str, datetime] = {}
        self._lock = threading.Lock()

        # Active signals: {keyword: {"boost": int, "articles": [...], "fired_at": datetime}}
        self.active_signals: dict[str, dict] = {}

        # Callback invoked when a breaking signal fires — set by main.py
        self.on_breaking_signal = None

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        if not self.enabled:
            logger.info("HERALD disabled in config.")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="herald")
        self._thread.start()
        logger.info("HERALD started — polling every %ds.", self.check_interval)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("HERALD stopped.")

    def get_active_signals(self) -> dict:
        """Return a copy of all currently active breaking signals."""
        with self._lock:
            return dict(self.active_signals)

    def get_boost_for_topic(self, topic_keywords: list[str]) -> int:
        """
        Given a list of topic-related keywords, return the max confidence boost
        from any matching active HERALD signal. Returns 0 if none.
        """
        with self._lock:
            max_boost = 0
            for kw in topic_keywords:
                kw_lower = kw.lower()
                if kw_lower in self.active_signals:
                    max_boost = max(max_boost, self.active_signals[kw_lower]["boost"])
            return max_boost

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                self._poll_all_feeds()
                self._expire_old_articles()
                self._detect_velocity_spikes()
            except Exception as exc:
                logger.error("HERALD loop error: %s", exc)
            self._stop_event.wait(self.check_interval)

    def _poll_all_feeds(self):
        for category, urls in self.feeds.items():
            keywords = self.geo_keywords if category in ("geopolitical", "politics") else self.crypto_keywords
            for url in urls:
                self._fetch_feed(url, keywords)

    def _fetch_feed(self, url: str, keywords: list[str]):
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "ORACLE-HERALD/3.0"})
            source = feed.feed.get("title", url)
            for entry in feed.entries[:20]:
                raw_title = entry.get("title", "")
                title = raw_title.lower()
                summary = (entry.get("summary") or "").lower()
                text = title + " " + summary

                published = self._parse_published(entry)
                if published is None:
                    continue

                # Stable entry identity to prevent counting same RSS item every poll.
                entry_key = self._entry_key(entry, source)
                with self._lock:
                    if entry_key in self._seen_entries:
                        continue
                    self._seen_entries[entry_key] = datetime.now(tz=timezone.utc)

                for kw in keywords:
                    if self._keyword_in_text(kw, text):
                        with self._lock:
                            self._article_log[kw].append((published, raw_title, source))
        except Exception as exc:
            logger.debug("HERALD feed fetch failed (%s): %s", url, exc)

    @staticmethod
    def _entry_key(entry, source: str) -> str:
        entry_id = entry.get("id") or entry.get("link") or entry.get("title") or ""
        return f"{source}|{entry_id}".strip()

    @staticmethod
    def _keyword_in_text(keyword: str, text: str) -> bool:
        """Whole-word/phrase match to avoid false positives like 'ban' matching 'bank'."""
        if not keyword:
            return False
        # Use word boundaries around the full escaped keyword.
        pattern = rf"\b{re.escape(keyword)}\b"
        return re.search(pattern, text, flags=re.IGNORECASE) is not None

    def _parse_published(self, entry) -> datetime | None:
        try:
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                import calendar
                ts = calendar.timegm(entry.published_parsed)
                return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            pass
        return datetime.now(tz=timezone.utc)

    def _expire_old_articles(self):
        cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=self.velocity_window)
        seen_cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=max(self.velocity_window * 6, 60))
        with self._lock:
            for kw in list(self._article_log.keys()):
                self._article_log[kw] = [
                    (ts, title, src)
                    for ts, title, src in self._article_log[kw]
                    if ts >= cutoff
                ]
            self._seen_entries = {
                key: ts for key, ts in self._seen_entries.items() if ts >= seen_cutoff
            }

    def _detect_velocity_spikes(self):
        now = datetime.now(tz=timezone.utc)
        with self._lock:
            for kw, articles in self._article_log.items():
                if len(articles) >= self.velocity_threshold:
                    sources = {src for _, _, src in articles}
                    boost = self._calc_boost(len(articles), len(sources))
                    prev = self.active_signals.get(kw, {})
                    last_fired = self._last_signal_fired_at.get(kw)
                    cooldown_elapsed = (
                        last_fired is None
                        or (now - last_fired) >= timedelta(minutes=self.signal_cooldown_minutes)
                    )
                    should_fire = (not prev or prev["boost"] != boost) and cooldown_elapsed
                    if not prev or prev.get("boost") != boost:
                        unique_headlines = []
                        seen_titles = set()
                        for _, title, _ in articles:
                            if title and title not in seen_titles:
                                seen_titles.add(title)
                                unique_headlines.append(title)
                            if len(unique_headlines) >= 5:
                                break
                        signal = {
                            "keyword": kw,
                            "article_count": len(articles),
                            "source_count": len(sources),
                            "boost": boost,
                            "fired_at": now.isoformat(),
                            "headlines": unique_headlines,
                        }
                        self.active_signals[kw] = signal
                        if should_fire:
                            self._last_signal_fired_at[kw] = now
                            logger.info(
                                "HERALD BREAKING: '%s' — %d articles from %d sources, boost +%d%%",
                                kw, len(articles), len(sources), boost,
                            )
                            if self.on_breaking_signal:
                                try:
                                    self.on_breaking_signal(signal)
                                except Exception as exc:
                                    logger.error("HERALD callback error: %s", exc)
                else:
                    # Remove expired signal
                    self.active_signals.pop(kw, None)

    @staticmethod
    def _calc_boost(article_count: int, source_count: int) -> int:
        base = min(article_count * 2, 10)
        diversity = min(source_count * 3, 15)
        return base + diversity  # hard cap at 25


# Convenience: standalone run for testing
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()

    def on_signal(sig):
        print(f"[HERALD] Signal: {sig}")

    herald = HeraldAgent(cfg)
    herald.on_breaking_signal = on_signal
    herald.start()
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        herald.stop()
