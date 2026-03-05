"""
apex_agent.py — APEX: Gemini 2.5 Pro final betting decision engine.

Role: Receives markets where SCOUT confidence >= 55%.
      Runs pre-bet safety checklist (6 checks).
      Outputs: BET_YES / BET_NO / SKIP with bet sizing and exit strategy.

Cost: ~$0.002 per call (Gemini 2.5 Pro).
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from tools.gemini_cost import extract_usage_metadata, update_gemini_cost_tracker
from tools.telegram import notify_gemini_cost

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.json"

APEX_SYSTEM_PROMPT = """You are APEX, the final betting decision engine for ORACLE.

IMPORTANT: This market has already passed ALL quality filters (volume, days, edge, confidence,
coin-flip zone, uncertain resolution, market age). Your job is ONLY to decide direction:
  BET_YES — bet YES on this market
  BET_NO  — bet NO on this market
  SKIP    — skip only if account-level safety checks fail

Pre-bet checklist (account safety only — output SKIP if any fail):
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

EXIT STRATEGY HARD RULES (non-negotiable):
- days_to_resolution <= 7:  You may choose HOLD_TO_RESOLUTION or SELL_AT_TARGET
- days_to_resolution 8-30:  You MUST use SELL_AT_TARGET with exit_target_price = entry_price * 1.5
- days_to_resolution > 30:  Return SKIP immediately (market should not have reached you)

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


def _sanitize_for_llm(text: str) -> str:
    """
    Remove characters that cause JSON encoding issues when Gemini echoes
    market names back in its response fields (e.g. $200k, $35,000).
    Replaces $ % & < > " ' with safe ASCII equivalents.
    """
    return (
        text
        .replace("$", "USD")
        .replace("%", " pct")
        .replace("&", "and")
        .replace("<", "lt")
        .replace(">", "gt")
        .replace('"', "'")
    )


def _build_apex_prompt(
    market: dict,
    scout_result: dict,
    account_state: dict,
    pattern_summary: str,
    exit_target_multiplier: float = 2.5,
    edge_result: dict | None = None,
    posterior_result: dict | None = None,
    suggested_bet_side: str | None = None,
) -> str:
    from tools.edge_calculator import kelly_fraction, quarter_kelly_bet
    today = datetime.now().strftime("%B %d, %Y")
    balance = float(account_state.get("current_balance", 10.0))
    open_bets = int(account_state.get("open_bet_count", 0))
    yes_price = float(market.get("yes_price", 0.5) or 0.5)
    no_price = float(market.get("no_price", 1.0 - yes_price) or (1.0 - yes_price))
    days_remaining = float(market.get("days_remaining", market.get("days_to_resolution", 0)) or 0)
    confidence = int(scout_result.get("confidence", 0) or 0)
    sentiment = scout_result.get("sentiment", "NEUTRAL")

    net_edge = float(edge_result.get("net_edge", 0.0) if edge_result else 0.0)
    implied_prob = float(edge_result.get("implied_prob", 0.5) if edge_result else 0.5)
    odds = 1.0 / implied_prob if implied_prob > 0 else 2.0
    kelly_fraction(net_edge, odds)
    qk_bet = quarter_kelly_bet(net_edge, odds, balance)
    bet_side = edge_result.get("bet_side", suggested_bet_side or "YES") if edge_result else (suggested_bet_side or "YES")

    checklist_pass = (
        balance > 20
        and open_bets < 10
        and float(market.get("volume", 0) or 0) > 500
        and days_remaining >= 1
        and (yes_price < 0.90 or bet_side == "NO")
        and (yes_price > 0.10 or bet_side == "YES")
    )

    return (
        f"TODAY: {today}\n"
        "Use ONLY data below. Do not use training knowledge about current events.\n\n"
        f"MARKET: {_sanitize_for_llm(market.get('question', ''))}\n"
        f"YES price: {yes_price:.3f} | NO price: {no_price:.3f}\n"
        f"Volume: ${float(market.get('volume', 0) or 0):,.0f} | Days remaining: {days_remaining:.1f}\n\n"
        f"SCOUT: {sentiment} {confidence}% confidence\n"
        f"EDGE: {net_edge:.1%} net edge -> bet {bet_side}\n"
        f"KELLY SIZE: ${qk_bet:.2f}\n"
        f"BALANCE: ${balance:.2f} | OPEN BETS: {open_bets}\n"
        f"CHECKLIST: {'ALL PASS' if checklist_pass else 'FAIL - RETURN SKIP'}\n\n"
        "Respond with ONLY this JSON object, nothing else:\n"
        f"{{\"action\":\"BET_YES or BET_NO or SKIP\",\"bet_usdc\":{qk_bet:.2f},\"reasoning\":\"max 15 words\",\"exit_strategy\":\"HOLD_TO_RESOLUTION or SELL_AT_TARGET\"}}"
    )


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
            from google import genai
            if not self.api_key or self.api_key.startswith("YOUR_"):
                raise ValueError("Gemini API key not configured in config.json")
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def _generate_with_google_genai(self, prompt: str):
        from google.genai import types as genai_types
        client = self._get_client()
        return client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=APEX_SYSTEM_PROMPT,
                temperature=0.2,
                top_p=0.9,
                max_output_tokens=256,
            ),
        )

    @staticmethod
    def _extract_response_text(response) -> str:
        """
        Extract text robustly across Gemini SDK variants.
        Handles cases where `response.text` accessor raises and where
        candidate content exists only in parts.
        """
        try:
            text = (response.text or "").strip()
            if text:
                return text
        except Exception as text_exc:
            logger.warning("APEX text extraction failed: %s", text_exc)

        candidates = getattr(response, "candidates", None)
        if not candidates and isinstance(response, dict):
            candidates = response.get("candidates", [])

        for cand in candidates or []:
            content = cand.get("content") if isinstance(cand, dict) else getattr(cand, "content", None)
            if not content:
                continue
            parts = content.get("parts") if isinstance(content, dict) else getattr(content, "parts", None)
            for part in parts or []:
                text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
                if text:
                    return str(text).strip()

        return ""

    @staticmethod
    def _parse_apex_response(response) -> dict:
        """Safely parse APEX response object with MAX_TOKENS and empty-content guards."""
        skip = {
            "action": "SKIP",
            "market_id": "",
            "bet_side": None,
            "bet_usdc": None,
            "scout_confidence": 0,
            "apex_confidence": 0,
            "reasoning": "parse_failure",
            "risk_assessment": "N/A",
            "skip_reason": "parse_failure",
            "exit_strategy": None,
            "exit_target_price": None,
            "telegram_message": "APEX parse failure — market skipped.",
        }

        try:
            candidates = getattr(response, "candidates", None)
            if not candidates:
                logger.error("APEX empty response candidates")
                return dict(skip)

            candidate = candidates[0]
            finish_reason = getattr(candidate, "finish_reason", None)
            if int(finish_reason or 0) == 2:
                logger.error("APEX hit MAX_TOKENS - returning SKIP")
                out = dict(skip)
                out["skip_reason"] = "max_tokens"
                return out

            content = getattr(candidate, "content", None)
            parts = getattr(content, "parts", None) if content else None
            if not parts:
                logger.error("APEX empty response content")
                return dict(skip)

            text = (getattr(parts[0], "text", "") or "").strip()
            if not text:
                logger.error("APEX response part missing text")
                return dict(skip)

            text = text.replace("```json", "").replace("```", "").strip()
            start = text.find("{")
            end = text.rfind("}") + 1
            if start == -1 or end == 0:
                logger.error("APEX no JSON found in: %s", text[:120])
                return dict(skip)

            result = json.loads(text[start:end])
            if result.get("action") not in ("BET_YES", "BET_NO", "SKIP"):
                logger.error("APEX invalid action: %s", result.get("action"))
                return dict(skip)
            return result
        except Exception as exc:
            logger.error("APEX parse error: %s", exc)
            return dict(skip)

    @staticmethod
    def _should_retry_parse(result: dict) -> bool:
        if (result or {}).get("action") != "SKIP":
            return False
        reason = (result or {}).get("skip_reason", "")
        return reason.startswith("parse_error:") or reason in ("parse_failure", "max_tokens")

    @staticmethod
    def _build_compact_retry_prompt(
        market: dict,
        scout_result: dict,
        account_state: dict,
        suggested_bet_side: str | None,
    ) -> str:
        yes_price = float(market.get("yes_price", 0.5) or 0.5)
        no_price = float(market.get("no_price", 0.5) or 0.5)
        spread = abs(yes_price + no_price - 1.0)
        return (
            "Return ONLY one valid JSON object with these exact keys: "
            "action, market_id, bet_side, bet_usdc, scout_confidence, apex_confidence, reasoning, "
            "risk_assessment, skip_reason, exit_strategy, exit_target_price, telegram_message.\n"
            "No markdown. No code fences.\n"
            f"Market ID: {market.get('condition_id', '')}\n"
            f"Question: {_sanitize_for_llm(market.get('question', ''))}\n"
            f"YES: {yes_price:.3f} | NO: {no_price:.3f} | spread: {spread:.3f}\n"
            f"Volume: {market.get('volume', 0)} | Days: {market.get('days_to_resolution', 0)}\n"
            f"Scout sentiment: {scout_result.get('sentiment', 'NEUTRAL')}\n"
            f"Scout confidence: {scout_result.get('confidence', 0)}\n"
            f"Suggested side: {suggested_bet_side or 'NONE'}\n"
            f"Balance: {account_state.get('current_balance', 0)} | Open bets: {account_state.get('open_bet_count', 0)}"
        )

    def decide(
        self,
        market: dict,
        scout_result: dict,
        account_state: dict,
        pattern_summary: str = "",
        edge_result: dict | None = None,
        posterior_result: dict | None = None,
        suggested_bet_side: str | None = None,
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
            market, scout_result, account_state, pattern_summary, self.exit_multiplier,
            edge_result=edge_result, posterior_result=posterior_result,
            suggested_bet_side=suggested_bet_side,
        )
        try:
            response = self._generate_with_google_genai(prompt)

            try:
                usage = extract_usage_metadata(response)
                if usage:
                    snapshot = update_gemini_cost_tracker(self.model, usage)
                    notify_gemini_cost(snapshot, self.cfg)
                    logger.info(
                        "Gemini cost tracked: call=$%.6f | day=$%.6f | total=$%.6f",
                        snapshot["last_call"].get("usd_cost", 0.0),
                        snapshot["daily"].get("usd_cost", 0.0),
                        snapshot["totals"].get("usd_cost", 0.0),
                    )
                else:
                    logger.warning("Gemini response missing usage metadata; cost not tracked for this call.")
            except Exception as track_exc:
                logger.warning("Gemini cost tracking failed: %s", track_exc)

            result = self._parse_apex_response(response)

            if self._should_retry_parse(result):
                logger.warning("APEX parse failed on primary prompt; retrying with compact JSON-only prompt")
                retry_prompt = self._build_compact_retry_prompt(
                    market,
                    scout_result,
                    account_state,
                    suggested_bet_side,
                )
                try:
                    retry_response = self._generate_with_google_genai(retry_prompt)

                    retry_result = self._parse_apex_response(retry_response)
                    if not self._should_retry_parse(retry_result):
                        result = retry_result
                except Exception as retry_exc:
                    logger.warning("APEX retry failed: %s", retry_exc)

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
    def _parse_json_response(content: str) -> dict:
        """
        Extract JSON from Gemini response.
        Always returns a dict — returns a SKIP decision on any parse failure
        so the pipeline never crashes due to a bad Gemini response.
        """
        def _skip_result(reason: str) -> dict:
            logger.warning("APEX: Returning SKIP due to parse failure: %s", reason)
            return {
                "action": "SKIP",
                "market_id": "",
                "bet_side": None,
                "bet_usdc": None,
                "scout_confidence": 0,
                "apex_confidence": 0,
                "reasoning": "Parse error — APEX response could not be decoded.",
                "risk_assessment": "N/A",
                "skip_reason": f"parse_error: {reason}",
                "exit_strategy": "HOLD_TO_RESOLUTION",
                "exit_target_price": None,
                "telegram_message": "APEX parse error — market skipped.",
            }

        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()

        start = content.find("{")
        end = content.rfind("}") + 1
        if start == -1 or end == 0:
            logger.error("APEX: No JSON in response. Full response:\n%s", content)
            return _skip_result("no_json_object")

        try:
            result = json.loads(content[start:end])
        except json.JSONDecodeError as exc:
            logger.error("APEX: JSON parse error: %s\nFull response:\n%s", exc, content)
            return _skip_result(f"json_decode: {exc}")

        # Validate action field
        if "action" not in result:
            logger.error("APEX: Missing 'action' field. Full response:\n%s", content)
            return _skip_result("missing_action_field")
        if result["action"] not in ("BET_YES", "BET_NO", "SKIP"):
            logger.error("APEX: Invalid action '%s'. Full response:\n%s", result["action"], content)
            return _skip_result(f"invalid_action: {result['action']}")

        # Validate bet fields when action is a bet
        if result["action"] in ("BET_YES", "BET_NO"):
            for field in ("bet_side", "bet_usdc", "scout_confidence", "apex_confidence"):
                if field not in result or result[field] is None:
                    logger.error("APEX: BET action missing required field '%s'. Full response:\n%s", field, content)
                    return _skip_result(f"missing_field: {field}")

        return result

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
