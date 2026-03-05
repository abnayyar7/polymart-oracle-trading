"""
memory.py — Bet logging, balance tracking, and pattern learning.

Maintains:
  data/bets.json      — all open and closed bets (paper + live)
  data/balance.json   — paper_balance and live_balance tracked separately
  data/patterns.json  — stats by category, confidence band, and mode
  agents/oracle/workspace/BETS.md    — human-readable bet log (paper/live sections)
  agents/oracle/workspace/MEMORY.md  — learned patterns (after 20+ bets)

balance.json shape:
  {
    "paper_balance": float, "paper_starting": float,
    "live_balance": float,  "live_starting": float,
    "history": [...]
  }

Each bet record includes:
  mode: "paper" | "live"
  order_type: "market" | "limit"
  fee_paid: float
  fill_status: "open" | "pending_fill"
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
BETS_JSON = ROOT / "data" / "bets.json"
BALANCE_JSON = ROOT / "data" / "balance.json"
PATTERNS_JSON = ROOT / "data" / "patterns.json"
WORKSPACE = ROOT / "agents" / "oracle" / "workspace"
BETS_MD = WORKSPACE / "BETS.md"
MEMORY_MD = WORKSPACE / "MEMORY.md"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> Any:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as exc:
        logger.error("Failed to read %s: %s", path, exc)
        return None


def _write_json(path: Path, data: Any):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        logger.error("Failed to write %s: %s", path, exc)


def _write_text(path: Path, text: str):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as exc:
        logger.error("Failed to write %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Balance — dual-mode (paper / live)
# ---------------------------------------------------------------------------

def get_balance() -> dict:
    """
    Return the full balance dict.
    Transparently migrates old single-balance format on first read.
    """
    data = _read_json(BALANCE_JSON)
    if data is None:
        data = {"paper_balance": 10.0, "paper_starting": 10.0, "live_balance": 0.0, "live_starting": 0.0, "history": []}
        _write_json(BALANCE_JSON, data)
        return data

    # Migrate old format {"current": ..., "starting": ..., "history": [...]}
    if "current" in data and "paper_balance" not in data:
        data = {
            "paper_balance": data["current"],
            "paper_starting": data.get("starting", data["current"]),
            "live_balance": 0.0,
            "live_starting": 0.0,
            "history": data.get("history", []),
        }
        _write_json(BALANCE_JSON, data)

    return data


def get_mode_balance(mode: str) -> dict:
    """Return {"current": float, "starting": float} for the given mode."""
    data = get_balance()
    return {
        "current": data.get(f"{mode}_balance", 0.0),
        "starting": data.get(f"{mode}_starting", 0.0),
    }


def update_balance(new_balance: float, mode: str = "paper", reason: str = ""):
    data = get_balance()
    key = f"{mode}_balance"
    old = data.get(key, 0.0)
    data[key] = round(new_balance, 4)
    data.setdefault("history", []).append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "old": old,
        "new": round(new_balance, 4),
        "reason": reason,
    })
    _write_json(BALANCE_JSON, data)
    logger.info("[%s] Balance: $%.4f → $%.4f (%s)", mode.upper(), old, new_balance, reason)


def get_account_state(mode: str = "paper") -> dict:
    bal = get_mode_balance(mode)
    open_bets = [b for b in get_open_bets() if b.get("mode") == mode]
    return {
        "mode": mode,
        "current_balance": bal["current"],
        "starting_balance": bal["starting"],
        "open_bet_count": len(open_bets),
        "open_bets": open_bets,
    }


# ---------------------------------------------------------------------------
# Bets
# ---------------------------------------------------------------------------

def get_all_bets() -> list[dict]:
    data = _read_json(BETS_JSON)
    return data if isinstance(data, list) else []


def get_open_bets() -> list[dict]:
    return [b for b in get_all_bets() if b.get("status") == "open"]


def has_open_position(market_id: str) -> bool:
    """Return True if an OPEN bet already exists for this market_id."""
    if not market_id:
        return False

    for bet in get_all_bets():
        if str(bet.get("market_id", "")) != str(market_id):
            continue
        if str(bet.get("status", "")).upper() == "OPEN":
            return True
    return False


def get_closed_bets() -> list[dict]:
    return [b for b in get_all_bets() if b.get("status") in ("won", "lost", "sold")]


def log_bet(
    market_id: str,
    market_question: str,
    side: str,
    bet_usdc: float,
    entry_price: float,
    scout_confidence: int,
    apex_confidence: int,
    category: str,
    exit_strategy: str,
    exit_target_price: float | None,
    telegram_message: str = "",
    paper: bool = True,
    mode: str = "paper",
    fee_paid: float = 0.0,
    order_type: str = "market",
    fill_status: str = "open",
) -> dict:
    """Create a new open bet record."""
    bet = {
        "id": f"bet_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{market_id[:8]}",
        "market_id": market_id,
        "market_question": market_question,
        "side": side,
        "bet_usdc": round(bet_usdc, 4),
        "entry_price": round(entry_price, 4),
        "fee_paid": round(fee_paid, 4),
        "order_type": order_type,
        "fill_status": fill_status,
        "scout_confidence": scout_confidence,
        "apex_confidence": apex_confidence,
        "category": category,
        "exit_strategy": exit_strategy,
        "exit_target_price": exit_target_price,
        "status": "open",
        "mode": mode,
        "paper": paper,  # kept for backward compat
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "closed_at": None,
        "pnl": None,
        "close_reason": None,
        "telegram_message": telegram_message,
        "cycles_pending": 0,
    }
    bets = get_all_bets()
    bets.append(bet)
    _write_json(BETS_JSON, bets)
    logger.info(
        "[%s][%s] Bet logged: %s %s $%.2f @ %.3f (fee=$%.4f)",
        mode.upper(), order_type, side, market_question[:40], bet_usdc, entry_price, fee_paid,
    )
    _update_bets_md()
    return bet


def mark_limit_filled(bet_id: str, fill_price: float):
    """Update a pending_fill limit order to open status once price crosses limit."""
    bets = get_all_bets()
    for bet in bets:
        if bet["id"] == bet_id:
            bet["fill_status"] = "open"
            bet["entry_price"] = round(fill_price, 4)
            logger.info("Limit order filled: %s @ %.3f", bet_id, fill_price)
            break
    _write_json(BETS_JSON, bets)


def close_bet(bet_id: str, close_price: float, close_reason: str = "resolution") -> dict | None:
    """Mark a bet as won/lost/sold and update mode-specific P&L."""
    bets = get_all_bets()
    bet = next((b for b in bets if b["id"] == bet_id), None)
    if not bet:
        logger.error("Bet not found: %s", bet_id)
        return None

    entry = bet["entry_price"]
    side = bet["side"]
    usdc = bet["bet_usdc"]
    mode = bet.get("mode", "paper")

    if side == "YES":
        pnl = round((close_price - entry) / entry * usdc, 4)
        won = close_price > entry
    else:
        pnl = round((entry - close_price) / entry * usdc, 4)
        won = close_price < entry

    bet["status"] = "won" if won else "lost"
    if close_reason == "sold":
        bet["status"] = "sold"
    bet["closed_at"] = datetime.now(timezone.utc).isoformat()
    bet["pnl"] = pnl
    bet["close_reason"] = close_reason
    bet["close_price"] = round(close_price, 4)

    _write_json(BETS_JSON, bets)

    # Update mode-specific balance
    bal = get_mode_balance(mode)
    new_balance = round(bal["current"] + pnl, 4)
    update_balance(new_balance, mode=mode, reason=f"Bet closed: {bet_id} ({bet['status']}, P&L ${pnl})")

    _update_patterns(bet)
    _update_bets_md()
    _update_memory_md()

    logger.info("[%s] Bet closed: %s — %s, P&L $%.4f", mode.upper(), bet_id, bet["status"], pnl)
    return bet


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

def _update_patterns(closed_bet: dict):
    patterns = _read_json(PATTERNS_JSON)
    if not patterns:
        return

    won = closed_bet["status"] in ("won", "sold")
    category = closed_bet.get("category", "crypto")
    confidence = closed_bet.get("scout_confidence", 0)
    pnl = closed_bet.get("pnl", 0.0)
    mode = closed_bet.get("mode", "paper")
    fee = closed_bet.get("fee_paid", 0.0)

    # by_category
    cat = patterns["by_category"].get(category, {"wins": 0, "losses": 0, "total_pnl": 0.0})
    cat["wins" if won else "losses"] += 1
    cat["total_pnl"] = round(cat.get("total_pnl", 0.0) + pnl, 4)
    patterns["by_category"][category] = cat

    # by_confidence_band
    if confidence >= 85:
        band = "85+"
    elif confidence >= 75:
        band = "75-84"
    elif confidence >= 65:
        band = "65-74"
    else:
        band = "55-64"
    band_data = patterns["by_confidence_band"].get(band, {"wins": 0, "losses": 0})
    band_data["wins" if won else "losses"] += 1
    patterns["by_confidence_band"][band] = band_data

    # by_mode
    if "by_mode" not in patterns:
        patterns["by_mode"] = {
            "paper": {"wins": 0, "losses": 0, "total_pnl": 0.0, "total_fees": 0.0},
            "live": {"wins": 0, "losses": 0, "total_pnl": 0.0, "total_fees": 0.0},
        }
    mode_data = patterns["by_mode"].get(mode, {"wins": 0, "losses": 0, "total_pnl": 0.0, "total_fees": 0.0})
    mode_data["wins" if won else "losses"] += 1
    mode_data["total_pnl"] = round(mode_data.get("total_pnl", 0.0) + pnl, 4)
    mode_data["total_fees"] = round(mode_data.get("total_fees", 0.0) + fee, 4)
    patterns["by_mode"][mode] = mode_data

    # totals
    patterns["total_bets"] = patterns.get("total_bets", 0) + 1
    patterns["total_wins"] = patterns.get("total_wins", 0) + (1 if won else 0)
    patterns["total_losses"] = patterns.get("total_losses", 0) + (0 if won else 1)
    patterns["overall_pnl"] = round(patterns.get("overall_pnl", 0.0) + pnl, 4)

    _write_json(PATTERNS_JSON, patterns)


def get_patterns() -> dict:
    return _read_json(PATTERNS_JSON) or {}


def get_pattern_summary_for_prompt(category: str, confidence: int) -> str:
    """Returns a human-readable summary for injection into APEX prompt."""
    patterns = get_patterns()
    total = patterns.get("total_bets", 0)
    if total < 20:
        return f"(Insufficient history: {total}/20 bets needed for learned patterns)"

    lines = []
    cat_data = patterns.get("by_category", {}).get(category, {})
    if cat_data:
        w, l = cat_data.get("wins", 0), cat_data.get("losses", 0)
        pnl = cat_data.get("total_pnl", 0.0)
        wr = w / (w + l) * 100 if (w + l) > 0 else 0
        lines.append(f"Category '{category}': {wr:.0f}% win rate ({w}W/{l}L), P&L ${pnl:.2f}")

    if confidence >= 85:
        band = "85+"
    elif confidence >= 75:
        band = "75-84"
    elif confidence >= 65:
        band = "65-74"
    else:
        band = "55-64"
    band_data = patterns.get("by_confidence_band", {}).get(band, {})
    if band_data:
        w, l = band_data.get("wins", 0), band_data.get("losses", 0)
        wr = w / (w + l) * 100 if (w + l) > 0 else 0
        lines.append(f"Confidence band {band}%: {wr:.0f}% win rate ({w}W/{l}L)")

    return "\n".join(lines) if lines else "No pattern data available."


# ---------------------------------------------------------------------------
# Markdown updaters
# ---------------------------------------------------------------------------

def _update_bets_md():
    all_bets = get_all_bets()
    patterns = get_patterns()

    lines = ["# BETS — Auto-updated by memory.py", ""]

    for mode_label, mode_key in [("Paper Trades", "paper"), ("Live Trades", "live")]:
        lines.append(f"## {mode_label}")
        lines.append("")

        open_bets = [b for b in all_bets if b.get("status") == "open" and b.get("mode") == mode_key]
        closed_bets = [b for b in all_bets if b.get("status") != "open" and b.get("mode") == mode_key]

        lines.append("### Open")
        if open_bets:
            for b in open_bets:
                lines.append(
                    f"- **{b['side']}** | {b['market_question'][:55]} | "
                    f"${b['bet_usdc']} @ {b['entry_price']} | fee=${b.get('fee_paid', 0):.4f} | "
                    f"SC:{b['scout_confidence']}% | {b['exit_strategy']} | {b['opened_at'][:10]}"
                )
        else:
            lines.append("_None._")

        lines.append("")
        lines.append("### Closed")
        if closed_bets:
            for b in sorted(closed_bets, key=lambda x: x.get("closed_at", ""), reverse=True)[:15]:
                pnl_str = f"${b['pnl']:+.4f}" if b["pnl"] is not None else "N/A"
                lines.append(
                    f"- **{b['status'].upper()}** | {b['side']} | {b['market_question'][:45]} | "
                    f"P&L {pnl_str} | fee=${b.get('fee_paid', 0):.4f} | {b.get('closed_at', '')[:10]}"
                )
        else:
            lines.append("_None._")

        mode_data = patterns.get("by_mode", {}).get(mode_key, {})
        w = mode_data.get("wins", 0)
        l = mode_data.get("losses", 0)
        p = mode_data.get("total_pnl", 0.0)
        f = mode_data.get("total_fees", 0.0)
        bal = get_mode_balance(mode_key)
        lines.append("")
        lines.append(f"**{mode_label} summary:** {w}W/{l}L | P&L ${p:+.4f} | Fees ${f:.4f} | Balance ${bal['current']:.4f}")
        lines.append("")

    lines.append("## Overall")
    total = patterns.get("total_bets", 0)
    wins = patterns.get("total_wins", 0)
    losses = patterns.get("total_losses", 0)
    pnl = patterns.get("overall_pnl", 0.0)
    lines.append(f"- Total bets: {total}")
    lines.append(f"- Wins: {wins} | Losses: {losses}")
    lines.append(f"- Overall P&L: ${pnl:+.4f}")

    _write_text(BETS_MD, "\n".join(lines) + "\n")


def _update_memory_md():
    patterns = get_patterns()
    total = patterns.get("total_bets", 0)
    now = datetime.now(timezone.utc).isoformat()

    lines = ["# MEMORY — Auto-updated by memory.py", ""]

    if total < 20:
        lines.append("## Learned Patterns")
        lines.append(f"_Populated after 20+ bets. Currently at {total} bets._")
    else:
        lines.append("## Learned Patterns")
        for cat, data in patterns.get("by_category", {}).items():
            w, l = data.get("wins", 0), data.get("losses", 0)
            pnl = data.get("total_pnl", 0.0)
            wr = w / (w + l) * 100 if (w + l) > 0 else 0
            lines.append(f"- **{cat}**: {wr:.0f}% win rate ({w}W/{l}L), P&L ${pnl:+.4f}")

    lines.append("")
    lines.append("## Win Rate by Category")
    for cat, data in patterns.get("by_category", {}).items():
        w, l = data.get("wins", 0), data.get("losses", 0)
        wr = f"{w/(w+l)*100:.0f}%" if (w + l) > 0 else "N/A"
        lines.append(f"- {cat.capitalize()}: {wr} ({w+l} bets)")

    lines.append("")
    lines.append("## Win Rate by Confidence Band")
    for band, data in patterns.get("by_confidence_band", {}).items():
        w, l = data.get("wins", 0), data.get("losses", 0)
        wr = f"{w/(w+l)*100:.0f}%" if (w + l) > 0 else "N/A"
        lines.append(f"- {band}%: {wr} ({w+l} bets)")

    lines.append("")
    lines.append("## Performance by Mode")
    for mode_key, data in patterns.get("by_mode", {}).items():
        w, l = data.get("wins", 0), data.get("losses", 0)
        pnl = data.get("total_pnl", 0.0)
        fees = data.get("total_fees", 0.0)
        wr = f"{w/(w+l)*100:.0f}%" if (w + l) > 0 else "N/A"
        lines.append(f"- {mode_key.upper()}: {wr} ({w+l} bets) | P&L ${pnl:+.4f} | Fees ${fees:.4f}")

    lines.append("")
    lines.append("## Notable Insights")
    if total >= 20:
        best_band = max(
            patterns.get("by_confidence_band", {}).items(),
            key=lambda x: x[1].get("wins", 0) / max(x[1].get("wins", 0) + x[1].get("losses", 0), 1),
            default=(None, {}),
        )
        if best_band[0]:
            lines.append(f"- Best performing confidence band: {best_band[0]}%")
    else:
        lines.append("_None yet._")

    lines.append("")
    lines.append("## Last Updated")
    lines.append(f"_{now}_")

    _write_text(MEMORY_MD, "\n".join(lines) + "\n")
