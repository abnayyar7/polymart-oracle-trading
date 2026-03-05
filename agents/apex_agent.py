"""
apex_agent.py — APEX: Gemini 2.5 Pro final betting decision engine.

Role: Receives markets where SCOUT confidence >= 55%.
      Runs pre-bet safety checklist (6 checks).
      Outputs: BET_YES / BET_NO / SKIP with bet sizing and exit strategy.

Cost: ~$0.002 per call (Gemini 2.5 Pro).
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.json"

APEX_SYSTEM_PROMPT = """You are APEX, the final betting decision engine for ORACLE.

You only receive markets where SCOUT confidence >= 55%.
Output BET_YES, BET_NO, or SKIP.

Pre-bet checklist (ALL must pass — output SKIP if any fail):
1. balance > survival_threshold ($20)
2. open_bets < max_open_bets (10)
3. daily_loss_pct < max_daily_loss_pct (10%)
4. bid_ask_spread < 15%
5. market_volume > $500 USDC
6. days_to_resolution >= 1

Bet sizing:
- confidence 55-64: 1% of balance (min $1.00)
- confidence 65-74: 2% of balance
- confidence 75-84: 3% of balance
- confidence 85+:   4% of balance (max $5.00)

Output ONLY valid JSON:
{
  "action": "BET_YES" | "BET_NO" | "SKIP",
  "market_id": "string",
  "bet_side": "YES" | "NO" | null,
  "bet_usdc": float | null,
  "scout_confidence": int,
  "apex_confidence": int,
  "reasoning": "2-3 sentences",
  "risk_assessment": "what could go wrong",
  "skip_reason": "if SKIP: why",
  "exit_strategy": "HOLD_TO_RESOLUTION" | "SELL_AT_TARGET",
  "exit_target_price": float | null,
  "telegram_message": "emoji-rich 1-2 line summary"
}"""


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _build_apex_prompt(
    market: dict,
    scout_result: dict,
    account_state: dict,
    pattern_summary: str,
    exit_target_multiplier: float = 2.5,
) -> str:
    balance = account_state.get("current_balance", 10.0)
    open_bets = account_state.get("open_bet_count", 0)
    yes_price = market.get("yes_price", 0.5)
    no_price = market.get("no_price", 0.5)
    spread = abs(yes_price + no_price - 1.0)

    # Bet sizing guidance
    confidence = scout_result.get("confidence", 0)
    if confidence >= 85:
        pct = 0.04
    elif confidence >= 75:
        pct = 0.03
    elif confidence >= 65:
        pct = 0.02
    else:
        pct = 0.01
    suggested_bet = max(1.0, min(5.0, round(balance * pct, 2)))

    # Exit target
    side_price = yes_price if scout_result.get("sentiment") == "BULLISH" else no_price
    exit_target = round(side_price * exit_target_multiplier, 4) if side_price > 0 else None
    exit_target_str = f"{exit_target:.3f}" if exit_target else "N/A"

    return f"""=== ACCOUNT STATE ===
Current balance: ${balance:.4f} USDC
Survival threshold: $20.00
Open bets: {open_bets} / 10
(Daily loss % and survival check are yours to validate.)

=== MARKET ===
Market ID: {market.get('condition_id', '')}
Question: {market.get('question', '')}
Category: {market.get('category', 'unknown')}
YES price: {yes_price:.3f}
NO price: {no_price:.3f}
Bid-ask spread: {spread:.3f} ({spread*100:.1f}%)
Volume: ${market.get('volume', 0):,.0f} USDC
Days to resolution: {market.get('days_to_resolution', 0):.1f}

=== SCOUT ANALYSIS ===
Sentiment: {scout_result.get('sentiment')}
Confidence: {confidence}%
Narrative: {scout_result.get('narrative', '')}
Key signals: {json.dumps(scout_result.get('key_signals', []))}
Conflicting signals: {json.dumps(scout_result.get('conflicting_signals', []))}
Reasoning chain: {scout_result.get('reasoning_chain', '')}

=== PATTERN HISTORY ===
{pattern_summary}

=== BET SIZING GUIDANCE ===
Confidence band {confidence}%: {pct*100:.0f}% of balance = ${suggested_bet:.2f} USDC
Max single bet: $5.00

=== EXIT ===
SELL_AT_TARGET exit price (2.5x): {exit_target_str}
HOLD_TO_RESOLUTION: binary outcome

Run your pre-bet checklist. Output your JSON decision."""


_MOCK_APEX_RESULT = {
    "action": "SKIP",
    "bet_side": None,
    "bet_usdc": None,
    "scout_confidence": 68,
    "apex_confidence": 0,
    "reasoning": "[MOCK] Dev mode active — no Gemini call made.",
    "risk_assessment": "[MOCK] N/A",
    "skip_reason": "mock_apex_result=true in dev_flags",
    "exit_strategy": "HOLD_TO_RESOLUTION",
    "exit_target_price": None,
    "telegram_message": "[MOCK] APEX skipped in dev mode",
}


class ApexAgent:
    def __init__(self, config: dict):
        self.cfg = config
        self.gemini_cfg = config.get("gemini", {})
        self.api_key = self.gemini_cfg.get("api_key", "")
        self.model = self.gemini_cfg.get("model", "gemini-2.5-pro")
        self.betting_cfg = config.get("betting", {})
        self.exit_multiplier = self.betting_cfg.get("exit_target_multiplier", 2.5)
        self._client = None

    def _get_client(self):
        if self._client is None:
            import google.generativeai as genai
            if not self.api_key or self.api_key.startswith("YOUR_"):
                raise ValueError("Gemini API key not configured in config.json")
            genai.configure(api_key=self.api_key)
            self._client = genai.GenerativeModel(
                model_name=self.model,
                system_instruction=APEX_SYSTEM_PROMPT,
                generation_config={
                    "temperature": 0.1,
                    "top_p": 0.9,
                    "max_output_tokens": 1024,
                },
            )
        return self._client

    def decide(
        self,
        market: dict,
        scout_result: dict,
        account_state: dict,
        pattern_summary: str = "",
    ) -> dict | None:
        """
        Run APEX decision.
        Returns parsed JSON dict with action/bet/exit fields, or None on failure.
        """
        dev = self.cfg.get("dev_flags", {})

        if dev.get("mock_apex_result", False):
            logger.info("[DEV] mock_apex_result=true — returning hardcoded APEX SKIP (no Gemini call)")
            result = dict(_MOCK_APEX_RESULT)
            result["market_id"] = market.get("condition_id", "")
            result["scout_confidence"] = scout_result.get("confidence", 0)
            return result

        if not dev.get("use_gemini", True):
            logger.info("[DEV] use_gemini=false — skipping APEX entirely, returning mock SKIP")
            result = dict(_MOCK_APEX_RESULT)
            result["market_id"] = market.get("condition_id", "")
            result["scout_confidence"] = scout_result.get("confidence", 0)
            result["skip_reason"] = "use_gemini=false in dev_flags"
            return result

        prompt = _build_apex_prompt(
            market, scout_result, account_state, pattern_summary, self.exit_multiplier
        )
        try:
            client = self._get_client()
            response = client.generate_content(prompt)
            content = response.text.strip()
            result = self._parse_json_response(content)
            if result:
                logger.info(
                    "APEX: %s | action=%s | bet=$%s | apex_conf=%d%%",
                    market.get("question", "")[:50],
                    result.get("action"),
                    result.get("bet_usdc"),
                    result.get("apex_confidence", 0),
                )
            return result
        except ValueError as exc:
            logger.error("APEX config error: %s", exc)
            return None
        except Exception as exc:
            logger.error("APEX decision failed: %s", exc)
            return None

    @staticmethod
    def _parse_json_response(content: str) -> dict | None:
        """Extract JSON from Gemini response."""
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()

        start = content.find("{")
        end = content.rfind("}") + 1
        if start == -1 or end == 0:
            logger.error("APEX: No JSON in response: %s", content[:200])
            return None
        try:
            result = json.loads(content[start:end])
            # Validate required fields
            if "action" not in result:
                logger.error("APEX: Missing 'action' field: %s", result)
                return None
            if result["action"] not in ("BET_YES", "BET_NO", "SKIP"):
                logger.error("APEX: Invalid action '%s'", result["action"])
                return None
            return result
        except json.JSONDecodeError as exc:
            logger.error("APEX: JSON parse error: %s | content: %s", exc, content[:200])
            return None

    def calc_bet_size(self, confidence: int, balance: float) -> float:
        """Calculate bet size based on confidence band and balance."""
        if confidence >= 85:
            pct = 0.04
        elif confidence >= 75:
            pct = 0.03
        elif confidence >= 65:
            pct = 0.02
        else:
            pct = 0.01
        return max(1.0, min(5.0, round(balance * pct, 2)))
