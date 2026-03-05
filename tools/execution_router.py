"""
execution_router.py — Hot-switchable execution router for ORACLE.

Single source of truth: config/config.json betting.paper_trading flag.

The flag is RE-READ from disk on every call so it can be toggled while
ORACLE is running without a restart. The caller (main.py) is responsible
for detecting and alerting on mode switches between scan cycles.

Usage:
    from tools.execution_router import route, get_current_mode

    result = route(market, side, bet_usdc, config, exit_target_price)
    mode   = get_current_mode()          # "paper" or "live"
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_CONFIG_JSON = Path(__file__).parent.parent / "config" / "config.json"

# Module-level last known mode — used by Oracle to detect switches
_last_known_mode: str | None = None


def get_current_mode() -> str:
    """
    Read paper_trading flag fresh from config.json.
    Returns "paper" or "live".
    Never raises — defaults to "paper" on any read error.
    """
    try:
        with open(_CONFIG_JSON) as f:
            cfg = json.load(f)
        paper = cfg.get("betting", {}).get("paper_trading", True)
    except Exception as exc:
        logger.error("execution_router: failed to read config.json (%s) — defaulting to paper", exc)
        paper = True
    return "paper" if paper else "live"


def route(
    market: dict,
    side: str,
    bet_usdc: float,
    config: dict,
    exit_target_price: float | None = None,
    order_type: str | None = None,
    limit_price: float | None = None,
) -> dict:
    """
    Route a bet to PaperExecutor or LiveExecutor based on the CURRENT flag in config.json.

    order_type defaults:
      paper mode → "market"
      live mode  → "limit"  (earn rebates; falls back to market after 2 cycles)
    """
    global _last_known_mode

    mode = get_current_mode()

    # Record for switch detection
    _last_known_mode = mode

    if mode == "paper":
        from tools.paper_executor import execute as paper_execute
        effective_order_type = order_type or "market"
        result = paper_execute(
            market=market,
            side=side,
            bet_usdc=bet_usdc,
            order_type=effective_order_type,
            limit_price=limit_price,
            exit_target_price=exit_target_price,
        )
    else:
        from tools.live_executor import execute as live_execute
        effective_order_type = order_type or "limit"
        result = live_execute(
            market=market,
            side=side,
            bet_usdc=bet_usdc,
            config=config,
            order_type=effective_order_type,
            limit_price=limit_price,
            exit_target_price=exit_target_price,
        )

    return result


def check_open_positions(markets: list[dict], config: dict) -> list[dict]:
    """
    Delegate per-cycle position checks to the correct executor.
    Always checks both modes so nothing is missed on a mid-run switch.
    """
    actions = []

    from tools.paper_executor import check_open_positions as paper_check
    from tools.live_executor import check_open_positions as live_check

    try:
        actions.extend(paper_check(markets, config))
    except Exception as exc:
        logger.error("Paper position check failed: %s", exc)

    try:
        actions.extend(live_check(markets, config))
    except Exception as exc:
        logger.error("Live position check failed: %s", exc)

    return actions
