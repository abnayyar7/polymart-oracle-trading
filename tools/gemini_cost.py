"""
gemini_cost.py — Gemini token usage + USD cost tracker.

Tracks per-call, daily, monthly, and all-time costs for Gemini Standard pricing.
Pricing source is user-provided table (Mar 2026):
  - Input:  $1.25 / 1M tokens (prompt <= 200k), else $2.50 / 1M
  - Output: $10.00 / 1M tokens (prompt <= 200k), else $15.00 / 1M
  - Context cache: $0.125 / 1M tokens (prompt <= 200k), else $0.25 / 1M
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
GEMINI_COST_JSON = ROOT / "data" / "gemini_cost.json"

PROMPT_TIER_THRESHOLD = 200_000


def _blank_bucket() -> dict:
    return {
        "calls": 0,
        "prompt_tokens": 0,
        "output_tokens": 0,
        "cache_tokens": 0,
        "usd_cost": 0.0,
    }


def _new_state() -> dict:
    return {
        "totals": _blank_bucket(),
        "daily": {},
        "monthly": {},
        "last_call": None,
        "updated_at": None,
    }


def _read_state() -> dict:
    try:
        if not GEMINI_COST_JSON.exists():
            return _new_state()
        with open(GEMINI_COST_JSON, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _new_state()
        data.setdefault("totals", _blank_bucket())
        data.setdefault("daily", {})
        data.setdefault("monthly", {})
        data.setdefault("last_call", None)
        data.setdefault("updated_at", None)
        return data
    except Exception as exc:
        logger.warning("Gemini cost tracker read failed: %s", exc)
        return _new_state()


def _write_state(state: dict):
    try:
        GEMINI_COST_JSON.parent.mkdir(parents=True, exist_ok=True)
        with open(GEMINI_COST_JSON, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as exc:
        logger.warning("Gemini cost tracker write failed: %s", exc)


def extract_usage_metadata(response) -> dict | None:
    """
    Extract token metadata from Gemini response object.
    Returns dict with prompt/output/cache/total tokens, or None if unavailable.
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage_metadata")
    if usage is None:
        return None

    def _get(field: str, default: int = 0) -> int:
        if isinstance(usage, dict):
            value = usage.get(field, default)
        else:
            value = getattr(usage, field, default)
        try:
            return int(value or 0)
        except Exception:
            return default

    prompt_tokens = _get("prompt_token_count")
    output_tokens = _get("candidates_token_count")
    cache_tokens = _get("cached_content_token_count")
    total_tokens = _get("total_token_count")

    if output_tokens <= 0 and total_tokens > 0 and prompt_tokens > 0:
        output_tokens = max(total_tokens - prompt_tokens, 0)

    return {
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "cache_tokens": cache_tokens,
        "total_tokens": total_tokens,
    }


def _pricing_for_prompt(prompt_tokens: int) -> dict:
    if prompt_tokens <= PROMPT_TIER_THRESHOLD:
        return {
            "tier": "<=200k",
            "input_per_m": 1.25,
            "output_per_m": 10.0,
            "cache_per_m": 0.125,
        }
    return {
        "tier": ">200k",
        "input_per_m": 2.50,
        "output_per_m": 15.0,
        "cache_per_m": 0.25,
    }


def _cost_usd(prompt_tokens: int, output_tokens: int, cache_tokens: int) -> tuple[float, dict]:
    pricing = _pricing_for_prompt(prompt_tokens)
    prompt_cost = (prompt_tokens / 1_000_000) * pricing["input_per_m"]
    output_cost = (output_tokens / 1_000_000) * pricing["output_per_m"]
    cache_cost = (cache_tokens / 1_000_000) * pricing["cache_per_m"]
    total = round(prompt_cost + output_cost + cache_cost, 6)
    return total, {
        "prompt_cost_usd": round(prompt_cost, 6),
        "output_cost_usd": round(output_cost, 6),
        "cache_cost_usd": round(cache_cost, 6),
        "pricing": pricing,
    }


def _apply_bucket(bucket: dict, prompt_tokens: int, output_tokens: int, cache_tokens: int, usd_cost: float):
    bucket["calls"] = int(bucket.get("calls", 0)) + 1
    bucket["prompt_tokens"] = int(bucket.get("prompt_tokens", 0)) + int(prompt_tokens)
    bucket["output_tokens"] = int(bucket.get("output_tokens", 0)) + int(output_tokens)
    bucket["cache_tokens"] = int(bucket.get("cache_tokens", 0)) + int(cache_tokens)
    bucket["usd_cost"] = round(float(bucket.get("usd_cost", 0.0)) + float(usd_cost), 6)


def update_gemini_cost_tracker(model: str, usage: dict) -> dict:
    """
    Persist one Gemini call usage and return a snapshot for notifications.
    """
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    cache_tokens = int(usage.get("cache_tokens", 0) or 0)
    total_tokens = int(usage.get("total_tokens", prompt_tokens + output_tokens) or 0)

    usd_cost, parts = _cost_usd(prompt_tokens, output_tokens, cache_tokens)

    state = _read_state()
    now = datetime.now(timezone.utc)
    day_key = now.strftime("%Y-%m-%d")
    month_key = now.strftime("%Y-%m")

    daily = state["daily"].setdefault(day_key, _blank_bucket())
    monthly = state["monthly"].setdefault(month_key, _blank_bucket())

    _apply_bucket(state["totals"], prompt_tokens, output_tokens, cache_tokens, usd_cost)
    _apply_bucket(daily, prompt_tokens, output_tokens, cache_tokens, usd_cost)
    _apply_bucket(monthly, prompt_tokens, output_tokens, cache_tokens, usd_cost)

    state["last_call"] = {
        "timestamp": now.isoformat(),
        "model": model,
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "cache_tokens": cache_tokens,
        "total_tokens": total_tokens,
        "usd_cost": usd_cost,
        **parts,
    }
    state["updated_at"] = now.isoformat()

    _write_state(state)

    return {
        "last_call": state["last_call"],
        "daily": state["daily"][day_key],
        "monthly": state["monthly"][month_key],
        "totals": state["totals"],
        "day_key": day_key,
        "month_key": month_key,
    }


def get_gemini_cost_snapshot() -> dict:
    """Return the current Gemini cost snapshot without mutating state."""
    state = _read_state()
    now = datetime.now(timezone.utc)
    day_key = now.strftime("%Y-%m-%d")
    month_key = now.strftime("%Y-%m")

    daily = state.get("daily", {}).get(day_key, _blank_bucket())
    monthly = state.get("monthly", {}).get(month_key, _blank_bucket())
    totals = state.get("totals", _blank_bucket())

    return {
        "last_call": state.get("last_call"),
        "daily": daily,
        "monthly": monthly,
        "totals": totals,
        "day_key": day_key,
        "month_key": month_key,
    }
