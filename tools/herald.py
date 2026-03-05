"""
HERALD — Breaking news velocity monitor.
Polls RSS feeds every 60 seconds. Fires confidence boost (+5% to +25%)
when 3+ articles spike on the same topic within a 10-minute window.

RSS feeds monitored (configured in config.json herald.feeds):
  Geopolitical: Reuters World, AP News, BBC World, Al Jazeera, The Guardian, NYT World, UN News
  Politics:     NYT Politics, Reuters Politics, BBC Politics, Politico, The Hill, Whitehouse.gov
  Crypto:       CoinDesk, CoinTelegraph, Decrypt, Bitcoin Magazine, The Block, Reddit crypto subs
  Entertainment: Variety, Deadline, Reddit r/entertainment, r/movies
  Sports:       ESPN, BBC Sport, Reddit r/sports
  Science/Tech: Ars Technica, MIT Tech Review, Wired, Reddit r/science, r/technology

Additional data sources:
  GDELT API:    Queries geopolitical event database every cycle (no key required)
  Wikipedia:    Monitors recent page edits for burst activity on watched topics

Nitter RSS:   whale_alert, lookonchain, VitalikButerin, binance, cointelegraph, coindesk
Telegram public channels (when configured):
  @whale_alert_io, @cointelegraph, @binance_announcements, @coindesk
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
        self.sports_keywords = [kw.lower() for kw in self.cfg.get("sports_keywords", [])]
        self.entertainment_keywords = [kw.lower() for kw in self.cfg.get("entertainment_keywords", [])]
        self.science_keywords = [kw.lower() for kw in self.cfg.get("science_keywords", [])]
        self.feeds = self.cfg.get("feeds", {})
        self.gdelt_cfg = self.cfg.get("gdelt", {})
        self.wiki_cfg = self.cfg.get("wikipedia", {})
        # Nitter RSS accounts monitored for breaking news
        self.nitter_accounts: list[str] = self.cfg.get("nitter_accounts", [])
        # Telegram public channels monitored when enabled
        self.telegram_channels: list[str] = self.cfg.get("telegram_channels", [])
        self.telegram_api_id: str = (config or {}).get("telegram", {}).get("api_id", "")
        self.telegram_api_hash: str = (config or {}).get("telegram", {}).get("api_hash", "")

        # {keyword: [(timestamp, title, source), ...]}
        self._article_log: dict[str, list[tuple]] = defaultdict(list)
        # Deduplicate feed items across polling cycles.
        # key -> seen_at datetime
        self._seen_entries: dict[str, datetime] = {}
        # URL-based dedup (cross-source): same URL from two feeds = same article
        self._seen_urls: set[str] = set()
        # Headline-based dedup: same story syndicated with slightly different entry IDs
        self._seen_headlines: set[str] = set()
        # Reset URL/headline dedup sets every 60 minutes to avoid memory growth
        self._seen_url_reset_at: datetime = datetime.now(tz=timezone.utc)
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

    def get_active_signals(self) -> list[dict]:
        """Return active breaking signals as a list of signal dicts."""
        with self._lock:
            return [s for s in self.active_signals.values() if isinstance(s, dict)]

    def get_active_signal(self) -> list[dict]:
        """Compatibility alias that always returns list-form active signals."""
        return self.get_active_signals()

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
                self._poll_nitter_feeds()
                self._poll_telegram_channels()
                self._poll_gdelt()
                self._poll_wikipedia_changes()
                self._expire_old_articles()
                self._detect_velocity_spikes()
            except Exception as exc:
                logger.error("HERALD loop error: %s", exc)
            self._stop_event.wait(self.check_interval)

    def _poll_all_feeds(self):
        category_keywords = {
            "geopolitical": self.geo_keywords,
            "politics": self.geo_keywords,
            "crypto": self.crypto_keywords,
            "sports": self.sports_keywords,
            "entertainment": self.entertainment_keywords,
            "science": self.science_keywords,
        }
        for category, urls in self.feeds.items():
            keywords = category_keywords.get(category, self.geo_keywords)
            for url in urls:
                self._fetch_feed(url, keywords)

    def _poll_nitter_feeds(self):
        """Poll Nitter RSS feeds for configured accounts with fallback instances."""
        nitter_instances = [
            "https://nitter.poast.org",
            "https://nitter.privacydev.net",
            "https://nitter.tiekoetter.com",
        ]
        for account in self.nitter_accounts:
            for instance in nitter_instances:
                url = f"{instance}/{account}/rss"
                try:
                    self._fetch_feed(url, self.crypto_keywords, source_override=f"Nitter: @{account}")
                    break  # Success, no need to try fallback instances
                except Exception as exc:
                    logger.debug("Nitter feed failed (%s): %s", url, exc)
                    continue  # Try next instance

    def _poll_telegram_channels(self):
        """Poll Telegram public channels if API credentials configured."""
        if not self.telegram_api_id or not self.telegram_api_hash:
            return
        if not any(self.telegram_channels):
            return
        try:
            from telethon import TelegramClient
            from telethon.errors import SessionPasswordNeededError
        except ImportError:
            logger.warning("Telethon not installed, skipping Telegram polling")
            return
        
        try:
            # Create a minimal client for reading public channels
            # No phone number needed for public channel reading
            client = TelegramClient(
                "herald_session",
                int(self.telegram_api_id),
                self.telegram_api_hash,
            )
            
            with client:
                for channel_name in self.telegram_channels:
                    try:
                        # Remove @ prefix if present
                        clean_name = channel_name.lstrip("@")
                        entity = client.get_entity(clean_name)
                        # Fetch last 20 messages
                        messages = client.get_messages(entity, limit=20)
                        for msg in messages:
                            if msg and msg.text:
                                text = msg.text.lower()
                                for kw in self.crypto_keywords:
                                    if self._keyword_in_text(kw, text):
                                        pub = msg.date if msg.date else datetime.now(tz=timezone.utc)
                                        with self._lock:
                                            self._article_log[kw].append(
                                                (pub, msg.text[:200], f"Telegram: {channel_name}")
                                            )
                    except Exception as exc:
                        logger.debug("Telegram channel fetch failed (%s): %s", channel_name, exc)
        except Exception as exc:
            logger.debug("Telegram polling error: %s", exc)

    def _poll_gdelt(self):
        """Query GDELT v2 DOC API for top geopolitical articles. No API key required.
        If a keyword appears across 3+ GDELT articles in the velocity window, inject
        into the article log to trigger the standard velocity-spike detection."""
        if not self.gdelt_cfg.get("enabled", True):
            return
        try:
            import requests
            params = {
                "mode": "artlist",
                "maxrecords": self.gdelt_cfg.get("max_records", 10),
                "format": "json",
                "timespan": self.gdelt_cfg.get("timespan", "15min"),
                "sourcelang": "English",
            }
            resp = requests.get(
                self.gdelt_cfg.get("endpoint", "https://api.gdeltproject.org/api/v2/doc/doc"),
                params=params,
                timeout=10,
            )
            if resp.status_code != 200:
                return
            articles = resp.json().get("articles", [])
            all_keywords = (
                self.geo_keywords
                + self.crypto_keywords
                + self.sports_keywords
                + self.entertainment_keywords
                + self.science_keywords
            )
            now = datetime.now(tz=timezone.utc)
            for art in articles:
                title = art.get("title", "")
                domain = art.get("domain", "GDELT")
                source = f"GDELT:{domain}"
                title_lower = title.lower()
                for kw in all_keywords:
                    if self._keyword_in_text(kw, title_lower):
                        entry_key = f"gdelt|{kw}|{title[:80]}"
                        with self._lock:
                            if entry_key in self._seen_entries:
                                continue
                            self._seen_entries[entry_key] = now
                            self._article_log[kw].append((now, title, source))
        except Exception as exc:
            logger.debug("GDELT poll failed: %s", exc)

    def _poll_wikipedia_changes(self):
        """Monitor Wikipedia recent changes for edit bursts on watched topics.
        A burst of 5+ edits to pages matching a watch keyword within the velocity
        window is treated as a breaking signal and injected into the article log."""
        if not self.wiki_cfg.get("enabled", True):
            return
        try:
            import requests
            params = {
                "action": "query",
                "list": "recentchanges",
                "rcnamespace": 0,
                "rclimit": self.wiki_cfg.get("rc_limit", 50),
                "format": "json",
                "rcprop": "title|timestamp",
            }
            resp = requests.get(
                self.wiki_cfg.get("endpoint", "https://en.wikipedia.org/w/api.php"),
                params=params,
                timeout=10,
            )
            if resp.status_code != 200:
                return
            changes = resp.json().get("query", {}).get("recentchanges", [])
            cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=self.velocity_window)
            watch_keywords = [kw.lower() for kw in self.wiki_cfg.get("watch_keywords", [])]
            # Count recent edits per watch keyword
            kw_edit_counts: dict[str, int] = defaultdict(int)
            for change in changes:
                title_lower = change.get("title", "").lower()
                ts_str = change.get("timestamp", "")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except Exception:
                    ts = datetime.now(tz=timezone.utc)
                if ts < cutoff:
                    continue
                for kw in watch_keywords:
                    if kw in title_lower:
                        kw_edit_counts[kw] += 1
            # Inject a signal for any keyword with a burst of edits
            burst_threshold = self.wiki_cfg.get("edit_burst_threshold", 5)
            now = datetime.now(tz=timezone.utc)
            for kw, count in kw_edit_counts.items():
                if count >= burst_threshold:
                    entry_key = f"wiki|{kw}|{now.strftime('%Y%m%d%H%M')}"
                    with self._lock:
                        if entry_key not in self._seen_entries:
                            self._seen_entries[entry_key] = now
                            headline = f"Wikipedia edit burst: {count} edits on '{kw}' pages"
                            self._article_log[kw].append((now, headline, "Wikipedia"))
                            logger.info("HERALD Wikipedia burst: %d edits on '%s' pages", count, kw)
        except Exception as exc:
            logger.debug("Wikipedia poll failed: %s", exc)

    def _fetch_feed(self, url: str, keywords: list[str], source_override: str = ""):
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "ORACLE-HERALD/3.0"})
            source = source_override or feed.feed.get("title", url)
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

                # Normalize headline for text-based dedup
                headline_key = " ".join(raw_title.split()).lower()
                # URL-based dedup key (cross-source: same story on two feeds)
                entry_url = (entry.get("link") or "").strip()

                with self._lock:
                    if entry_key in self._seen_entries:
                        continue
                    # Reject if we've seen this exact URL from another source/feed
                    if entry_url and entry_url in self._seen_urls:
                        continue
                    # Reject if headline text is a near-duplicate of an already-seen article
                    if headline_key and headline_key in self._seen_headlines:
                        continue
                    self._seen_entries[entry_key] = datetime.now(tz=timezone.utc)
                    if entry_url:
                        self._seen_urls.add(entry_url)
                    if headline_key:
                        self._seen_headlines.add(headline_key)

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
        now = datetime.now(tz=timezone.utc)
        cutoff = now - timedelta(minutes=self.velocity_window)
        seen_cutoff = now - timedelta(minutes=max(self.velocity_window * 6, 60))
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
            # Reset URL/headline dedup sets every 60 minutes to bound memory usage
            if (now - self._seen_url_reset_at) >= timedelta(minutes=60):
                self._seen_urls.clear()
                self._seen_headlines.clear()
                self._seen_url_reset_at = now
                logger.debug("HERALD URL/headline dedup sets reset.")

    def _detect_velocity_spikes(self):
        now = datetime.now(tz=timezone.utc)
        with self._lock:
            for kw, articles in self._article_log.items():
                if len(articles) >= self.velocity_threshold:
                    sources = {src for _, _, src in articles}
                    # Require 2+ distinct source domains — single-source echo ≠ breaking news
                    domains = {self._extract_source_domain(src) for src in sources}
                    if len(domains) < 2:
                        logger.debug(
                            "HERALD: '%s' has %d articles but only 1 source domain (%s) — skipping",
                            kw, len(articles), next(iter(domains), "?"),
                        )
                        self.active_signals.pop(kw, None)
                        continue
                    boost = self._calc_boost(len(articles), len(domains))
                    prev = self.active_signals.get(kw, {})
                    last_fired = self._last_signal_fired_at.get(kw)
                    cooldown_elapsed = (
                        last_fired is None
                        or (now - last_fired) >= timedelta(minutes=self.signal_cooldown_minutes)
                    )
                    should_fire = (not prev or prev["boost"] != boost) and cooldown_elapsed
                    if not prev or prev.get("boost") != boost:
                        unique_headlines = []
                        seen_titles: set[str] = set()
                        for _, title, _ in articles:
                            norm = " ".join(title.split()).lower()
                            if title and norm not in seen_titles:
                                seen_titles.add(norm)
                                unique_headlines.append(title)
                            if len(unique_headlines) >= 5:
                                break
                        signal = {
                            "keyword": kw,
                            "article_count": len(unique_headlines),
                            "source_count": len(domains),
                            "boost": boost,
                            "fired_at": now.isoformat(),
                            "headlines": unique_headlines,
                        }
                        self.active_signals[kw] = signal
                        if should_fire:
                            self._last_signal_fired_at[kw] = now
                            logger.info(
                                "HERALD BREAKING: '%s' — %d unique articles from %d sources, boost +%d%%",
                                kw, len(unique_headlines), len(domains), boost,
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
    def _extract_source_domain(source: str) -> str:
        """Normalize a source string to a domain label for diversity counting.
        Two articles from 'Reuters World News' and 'Reuters Politics' both
        map to 'reuters' — counted as one domain."""
        if not source:
            return "unknown"
        lower = source.lower()
        if lower.startswith("gdelt:"):
            # "GDELT:reuters.com" → "reuters"
            domain = source[6:].strip()
            return domain.split(".")[0].lower() if domain else "gdelt"
        if lower.startswith("nitter:"):
            return source  # each account is a distinct source
        if lower.startswith("telegram:"):
            return source  # each channel is a distinct source
        if source == "Wikipedia":
            return "wikipedia"
        # RSS feed titles: first meaningful word (e.g. "Reuters World News" → "reuters")
        words = lower.split()
        return words[0] if words else "unknown"

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
