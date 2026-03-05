"""
performance.py — P&L tracking, daily summary, and halt checks.

Halt conditions (either triggers a full stop):
  1. active-mode balance <= survival_threshold ($20 USDC)
  2. daily_loss_pct >= max_daily_loss_pct (10%) for the active mode
"""

import json
import logging
from datetime import datetime, date, timezone
from pathlib import Path

from tools.memory import get_balance, get_mode_balance, get_all_bets, get_patterns

logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config" / "config.json"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Halt check
# ---------------------------------------------------------------------------

def should_halt(config: dict, mode: str = "paper") -> tuple[bool, str]:
    """
    Returns (should_halt: bool, reason: str).
    Checks the balance and daily loss for the given trading mode.
    """
    betting_cfg = config.get("betting", {})
    survival_threshold = betting_cfg.get("survival_threshold_usdc", 20.0)
    max_daily_loss_pct = betting_cfg.get("max_daily_loss_pct", 10.0)

    bal = get_mode_balance(mode)
    current = bal["current"]
    starting = bal["starting"]

    if current <= survival_threshold:
        reason = (
            f"HALT [{mode.upper()}]: Balance ${current:.2f} <= survival threshold "
            f"${survival_threshold:.2f}. Trading stopped."
        )
        logger.warning(reason)
        return True, reason

    daily_loss_pct = _calc_daily_loss_pct(starting, mode)
    if daily_loss_pct >= max_daily_loss_pct:
        reason = (
            f"HALT [{mode.upper()}]: Daily loss {daily_loss_pct:.1f}% >= "
            f"max {max_daily_loss_pct:.1f}%. Resuming tomorrow."
        )
        logger.warning(reason)
        return True, reason

    return False, ""


def _calc_daily_loss_pct(starting_balance: float, mode: str = "paper") -> float:
    """Calculate today's loss as a percentage of starting balance for a given mode."""
    today = date.today().isoformat()
    bets = get_all_bets()
    todays_pnl = 0.0
    for bet in bets:
        if bet.get("mode") != mode:
            continue
        closed_at = bet.get("closed_at") or ""
        if closed_at.startswith(today) and bet.get("pnl") is not None:
            todays_pnl += bet["pnl"]
    if todays_pnl >= 0:
        return 0.0
    return round(abs(todays_pnl) / max(starting_balance, 0.01) * 100, 2)


# ---------------------------------------------------------------------------
# Daily summary (per mode + combined)
# ---------------------------------------------------------------------------

def get_daily_summary(config: dict, herald_signals: int = 0, herald_bets: int = 0) -> dict:
    """
    Returns a comprehensive daily summary dict covering both paper and live modes.
    herald_signals/herald_bets are counters maintained by the Oracle class.
    """
    today = date.today().isoformat()
    bets = get_all_bets()
    patterns = get_patterns()

    def _mode_stats(mode: str) -> dict:
        today_bets = [b for b in bets if (b.get("opened_at") or "").startswith(today) and b.get("mode") == mode]
        today_closed = [b for b in today_bets if b.get("status") != "open"]
        pnl_list = [b.get("pnl") or 0 for b in today_closed]
        fee_list = [b.get("fee_paid") or 0 for b in today_closed]
        wins = [b for b in today_closed if b.get("status") == "won"]
        losses = [b for b in today_closed if b.get("status") == "lost"]
        best = max(today_closed, key=lambda b: b.get("pnl") or 0, default=None)
        worst = min(today_closed, key=lambda b: b.get("pnl") or 0, default=None)
        bal = get_mode_balance(mode)
        mode_patterns = patterns.get("by_mode", {}).get(mode, {})
        return {
            "bets_placed": len(today_bets),
            "closed": len(today_closed),
            "wins": len(wins),
            "losses": len(losses),
            "today_pnl": round(sum(pnl_list), 4),
            "today_fees": round(sum(fee_list), 4),
            "current_balance": bal["current"],
            "starting_balance": bal["starting"],
            "total_pnl_alltime": mode_patterns.get("total_pnl", 0.0),
            "total_fees_alltime": mode_patterns.get("total_fees", 0.0),
            "best_bet": {"question": best["market_question"][:50], "pnl": best["pnl"]} if best else None,
            "worst_bet": {"question": worst["market_question"][:50], "pnl": worst["pnl"]} if worst else None,
            "open_bets": len([b for b in bets if b.get("status") == "open" and b.get("mode") == mode]),
        }

    return {
        "date": today,
        "paper": _mode_stats("paper"),
        "live": _mode_stats("live"),
        "total_bets_alltime": patterns.get("total_bets", 0),
        "overall_pnl_alltime": patterns.get("overall_pnl", 0.0),
        "herald_signals": herald_signals,
        "herald_bets": herald_bets,
    }


def format_daily_summary(summary: dict) -> str:
    """Format multi-mode daily summary as a plain text string (for logging)."""
    lines = [f"ORACLE Daily Summary — {summary['date']}"]

    for mode in ("paper", "live"):
        s = summary[mode]
        if s["bets_placed"] == 0 and s["current_balance"] == 0.0:
            continue
        wr = (
            f"{s['wins']/(s['wins']+s['losses'])*100:.0f}%"
            if (s["wins"] + s["losses"]) > 0
            else "N/A"
        )
        pnl_sign = "+" if s["today_pnl"] >= 0 else ""
        lines.append(f"\n[{mode.upper()}]")
        lines.append(f"  Balance: ${s['current_balance']:.4f} (start: ${s['starting_balance']:.4f})")
        lines.append(f"  Today: {s['wins']}W/{s['losses']}L | WR: {wr} | P&L: {pnl_sign}${s['today_pnl']:.4f}")
        lines.append(f"  Fee drag today: ${s['today_fees']:.4f}")
        if s["best_bet"]:
            lines.append(f"  Best:  {s['best_bet']['question']} (${s['best_bet']['pnl']:+.4f})")
        if s["worst_bet"]:
            lines.append(f"  Worst: {s['worst_bet']['question']} (${s['worst_bet']['pnl']:+.4f})")
        lines.append(f"  Open positions: {s['open_bets']}")

    herald_ratio = (
        f"{summary['herald_bets']}/{summary['herald_signals']}"
        if summary["herald_signals"] > 0
        else "0/0"
    )
    lines.append(f"\nHERALD signals fired: {summary['herald_signals']} | Bets triggered: {summary['herald_bets']} ({herald_ratio})")
    lines.append(f"All-time: {summary['total_bets_alltime']} bets | P&L ${summary['overall_pnl_alltime']:+.4f}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Open bet exit check (used by paper_executor; kept here for compatibility)
# ---------------------------------------------------------------------------

def check_exit_targets(open_bets: list[dict]) -> list[dict]:
    """Returns bets flagged SELL_AT_TARGET — caller evaluates against live price."""
    return [
        b for b in open_bets
        if b.get("exit_strategy") == "SELL_AT_TARGET" and b.get("exit_target_price") is not None
    ]
