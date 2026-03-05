"""
scout_agent.py — SCOUT: Local Ollama sentiment analyst.

Model: qwen2.5:14b (AMD RX 9070 XT, Vulkan backend)
Role: Analyze all raw signals holistically → BULLISH / BEARISH / NEUTRAL + confidence 0-100.

Escalates to APEX only if confidence >= 55 (saves Gemini API cost).
"""

import json
import logging
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.json"

SCOUT_SYSTEM_PROMPT = """You are SCOUT, a market sentiment analyst for Polymarket prediction markets.

You receive raw market signals (Binance price/RSI, Fear & Greed, CryptoPanic news
with crowd votes, Twitter/X tweet velocity, Reddit post velocity, news headlines,
HERALD breaking news signal if active, and Polymarket YES/NO prices).

Your job: analyze ALL signals holistically and determine whether the market's
current YES price is likely UNDERPRICED or OVERPRICED.

Analyze in this order:
1. EXTREMES FIRST — RSI > 70 or < 30, Fear & Greed at extremes = contrarian signal
2. MOMENTUM SECOND — Twitter velocity, HERALD breaking signal = directional signal
3. CROWD THIRD — Reddit velocity, CryptoPanic vote ratio = retail mood
4. MARKET STRUCTURE FOURTH — Polymarket YES/NO prices vs signals = consensus check

BULLISH = YES is likely to happen → consider BET_YES
BEARISH = YES is unlikely → consider BET_NO
NEUTRAL = mixed signals → skip

Confidence 55-64: borderline edge
Confidence 65-79: reasonable edge
Confidence 80+: strong edge

Output ONLY valid JSON, no markdown, no preamble:
{
  "market_id": "string",
  "sentiment": "BULLISH" | "BEARISH" | "NEUTRAL",
  "narrative": "2-3 sentences",
  "confidence": 0-100,
  "key_signals": ["signal1", "signal2", "signal3"],
  "conflicting_signals": ["opposite signals"],
  "reasoning_chain": "step-by-step logic"
}"""


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _build_signals_prompt(signals: dict) -> str:
    """Convert signals dict into a structured text block for SCOUT."""
    return f"""Market: {signals.get('market_question', 'Unknown')}
Market ID: {signals.get('market_id', '')}
Category: {signals.get('category', 'crypto')}

=== Polymarket Prices ===
YES price: {signals.get('polymarket_yes_price', 0.5):.3f}
NO price:  {signals.get('polymarket_no_price', 0.5):.3f}
Volume: ${signals.get('market_volume_usdc', 0):,.0f} USDC
Days to resolution: {signals.get('days_to_resolution', 0):.1f}

=== Binance / Price Data ===
BTC price: ${signals.get('btc_price', 'N/A')}
BTC 24h change: {signals.get('btc_price_change_pct_24h', 0):+.2f}%
BTC 24h volume: ${signals.get('btc_volume_24h', 0):,.0f}
RSI-14: {signals.get('rsi_14', 'N/A')}

=== Fear & Greed (self-calculated) ===
Score: {signals.get('fear_greed_score', 50)} / 100
Label: {signals.get('fear_greed_label', 'Neutral')}

=== CryptoPanic Crowd Sentiment ===
Bullish ratio: {signals.get('cryptopanic_bullish_ratio', 0.5):.2f} (0=all bearish, 1=all bullish)
Article count: {signals.get('cryptopanic_article_count', 0)}
Recent titles: {json.dumps(signals.get('cryptopanic_recent_titles', []), ensure_ascii=False)}

=== Social Velocity ===
Twitter/X tweet count (1h): {signals.get('twitter_tweet_count_1h', 0)} (source: {signals.get('twitter_source', 'none')})
Reddit post count (24h): {signals.get('reddit_post_count_24h', 0)}

=== HERALD Breaking News ===
Active: {signals.get('herald_active', False)}
Confidence boost: +{signals.get('herald_boost', 0)}%

=== Recent News Headlines ===
{chr(10).join(f'- {h}' for h in signals.get('rss_headlines', [])[:8])}

Now analyze all signals holistically and output your JSON decision."""


_MOCK_SCOUT_RESULT = {
    "sentiment": "BULLISH",
    "narrative": "[MOCK] Hardcoded dev result. Signals look moderately bullish based on simulated data.",
    "confidence": 68,
    "key_signals": ["mock_rsi_neutral", "mock_reddit_moderate", "mock_price_stable"],
    "conflicting_signals": ["mock_low_volume"],
    "reasoning_chain": "[MOCK] Dev mode active — no Ollama call made.",
}


class ScoutAgent:
    def __init__(self, config: dict):
        self.config = config
        self.ollama_url = config.get("ollama", {}).get("base_url", "http://localhost:11434")
        self.model = config.get("ollama", {}).get("model", "qwen2.5:14b")
        self.min_confidence = config.get("betting", {}).get("min_confidence", 55)
        self._session = requests.Session()

    def analyze(self, signals: dict) -> dict | None:
        """
        Run SCOUT analysis on collected signals.
        Returns parsed JSON dict, or None if Ollama is unreachable.
        """
        dev = self.config.get("dev_flags", {})

        if dev.get("mock_scout_result", False):
            logger.info("[DEV] mock_scout_result=true — returning hardcoded SCOUT result (BULLISH, conf=68)")
            result = dict(_MOCK_SCOUT_RESULT)
            result["market_id"] = signals.get("market_id", "")
            return result

        if not dev.get("use_ollama", True):
            logger.info("[DEV] use_ollama=false — skipping SCOUT, returning mock NEUTRAL conf=40")
            return {
                "market_id": signals.get("market_id", ""),
                "sentiment": "NEUTRAL",
                "narrative": "[DEV] Ollama disabled.",
                "confidence": 40,
                "key_signals": [],
                "conflicting_signals": [],
                "reasoning_chain": "[DEV] use_ollama=false",
            }

        prompt = _build_signals_prompt(signals)
        try:
            response = self._session.post(
                f"{self.ollama_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": SCOUT_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "stream": False,
                    "options": {
                        "temperature": 0.1,
                        "top_p": 0.9,
                        "num_predict": 512,
                    },
                },
                timeout=120,
            )
            response.raise_for_status()
            content = response.json()["message"]["content"].strip()
            result = self._parse_json_response(content)
            if result:
                result["market_id"] = signals.get("market_id", result.get("market_id", ""))
                logger.info(
                    "SCOUT: %s | %s | confidence=%d%%",
                    signals.get("market_question", "")[:50],
                    result.get("sentiment"),
                    result.get("confidence", 0),
                )
            return result
        except requests.exceptions.ConnectionError:
            logger.error("SCOUT: Ollama not running at %s. Start with: ollama serve", self.ollama_url)
            return None
        except Exception as exc:
            logger.error("SCOUT analysis failed: %s", exc)
            return None

    def should_escalate(self, scout_result: dict) -> bool:
        """True if confidence >= min_confidence AND not NEUTRAL."""
        if not scout_result:
            return False
        confidence = scout_result.get("confidence", 0)
        sentiment = scout_result.get("sentiment", "NEUTRAL")
        return confidence >= self.min_confidence and sentiment != "NEUTRAL"

    @staticmethod
    def _parse_json_response(content: str) -> dict | None:
        """Extract JSON from model output, handling markdown code blocks."""
        # Strip markdown fences if present
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()

        # Find outermost JSON object
        start = content.find("{")
        end = content.rfind("}") + 1
        if start == -1 or end == 0:
            logger.error("SCOUT: No JSON object found in response: %s", content[:200])
            return None

        try:
            return json.loads(content[start:end])
        except json.JSONDecodeError as exc:
            logger.error("SCOUT: JSON parse error: %s | content: %s", exc, content[:200])
            return None
