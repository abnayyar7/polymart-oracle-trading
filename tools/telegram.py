"""
telegram.py — All Telegram notification types for ORACLE.

Notification types:
  - startup / shutdown / halt / mode_switch
  - bet placed (paper or live) — includes fee, balance, order type
  - bet closed (won / lost / sold)
  - HERALD breaking signal
  - arbitrage / sniper opportunity
  - daily summary (separate paper + live sections)
  - generic info / error
"""

import json
import html
import logging
import re
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.json"

_SESSION = requests.Session()
_BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"
_UPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"


def _escape_html(text: str) -> str:
    return html.escape(text or "", quote=False)


def _strip_html_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "")


def _redact_secrets(text: str) -> str:
    """Redact secrets from logs (Telegram bot token and common API key patterns)."""
    if not text:
        return ""
    redacted = text
    # Telegram bot token pattern: 123456789:AA...
    redacted = re.sub(
        r"bot\d{6,}:[A-Za-z0-9_-]{20,}",
        "bot<REDACTED>",
        redacted,
        flags=re.IGNORECASE,
    )
    # Generic token-like key/value fragments in error payloads
    redacted = re.sub(
        r"(token|api_key|apikey|secret|passphrase)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}['\"]?",
        r"\1=<REDACTED>",
        redacted,
        flags=re.IGNORECASE,
    )
    return redacted


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Core send
# ---------------------------------------------------------------------------

def send_message(text: str, config: dict | None = None, parse_mode: str = "HTML") -> bool:
    """Send a Telegram message. Returns True on success."""
    cfg = config or load_config()

    if not cfg.get("dev_flags", {}).get("use_telegram", True):
        logger.info("[DEV] use_telegram=false — message suppressed: %s", text[:80])
        return False

    tg = cfg.get("telegram", {})
    token = tg.get("bot_token", "")
    chat_id = tg.get("chat_id", "")

    if not token or not chat_id:
        logger.debug("Telegram not configured — message skipped: %s", text[:80])
        return False

    url = _BASE_URL.format(token=token)
    try:
        r = _SESSION.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
        if r.ok:
            return True

        # Common failure: HTML parse errors from user-generated text containing '<' or '&'.
        # Retry once as plain text so notifications are still delivered.
        if r.status_code == 400 and parse_mode:
            logger.warning(
                "Telegram send 400 (parse_mode=%s): %s | retrying plain text",
                parse_mode,
                _redact_secrets((r.text or "")[:300]),
            )
            plain_text = _strip_html_tags(text)
            r2 = _SESSION.post(
                url,
                json={"chat_id": chat_id, "text": plain_text},
                timeout=10,
            )
            if r2.ok:
                return True
            logger.warning(
                "Telegram plain-text retry failed: HTTP %s | %s",
                r2.status_code,
                _redact_secrets((r2.text or "")[:300]),
            )
            return False

        r.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("Telegram send failed: %s", _redact_secrets(str(exc)))
        return False


# ---------------------------------------------------------------------------
# Startup / shutdown / system
# ---------------------------------------------------------------------------

def notify_startup(config: dict, paper: bool = True):
    mode = "PAPER TRADING" if paper else "LIVE TRADING"
    balance = _get_balance_str(config, "paper" if paper else "live")
    text = (
        f"<b>ORACLE v3.0 — ONLINE</b>\n"
        f"Mode: {mode}\n"
        f"Balance: {balance}\n"
        f"Scan interval: {config.get('betting', {}).get('scan_interval_minutes', 15)} min\n"
        f"HERALD: {'enabled' if config.get('herald', {}).get('enabled') else 'disabled'}"
    )
    send_message(text, config)


def notify_shutdown(config: dict, reason: str = "Manual stop"):
    text = f"<b>ORACLE — OFFLINE</b>\nReason: {reason}"
    send_message(text, config)


def notify_halt(config: dict, reason: str):
    text = f"<b>ORACLE — HALTED</b>\n{_escape_html(reason)}"
    send_message(text, config)


def notify_mode_switch(prev_mode: str, new_mode: str, config: dict):
    """Alert when paper_trading flag changes while ORACLE is running."""
    text = (
        f"<b>MODE SWITCH DETECTED</b>\n"
        f"{prev_mode.upper()} → {new_mode.upper()}\n"
        f"Next scan will execute as <b>{new_mode.upper()}</b>"
    )
    logger.warning("MODE SWITCH: %s → %s", prev_mode, new_mode)
    send_message(text, config)


# ---------------------------------------------------------------------------
# Trade alerts
# ---------------------------------------------------------------------------

def notify_bet_placed(bet: dict, config: dict):
    """
    Trade alert fired on every bet placement.

    Shows: [PAPER]/[LIVE] tag, market, side, entry price, bet size,
    fee paid, SCOUT/APEX confidence, exit strategy, current balance.
    """
    mode = bet.get("mode", "paper").upper()
    order_type = bet.get("order_type", "market").upper()
    side = bet["side"]
    fee = bet.get("fee_paid", 0.0)
    bal = _get_balance_str(config, bet.get("mode", "paper"))

    text = (
        f"<b>[{mode}] BET PLACED ({order_type})</b>\n"
        f"Side: <b>{side}</b>\n"
        f"Market: {bet['market_question'][:80]}\n"
        f"Entry: ${bet['entry_price']:.3f} | Size: ${bet['bet_usdc']:.2f} | Fee: ${fee:.4f}\n"
        f"SCOUT: {bet['scout_confidence']}% | APEX: {bet['apex_confidence']}%\n"
        f"Exit: {bet['exit_strategy']}"
        + (f" @ {bet['exit_target_price']:.3f}" if bet.get("exit_target_price") else "")
        + f"\nBalance: {bal}"
    )
    if bet.get("telegram_message"):
        text += f"\n{bet['telegram_message']}"
    send_message(text, config)


def notify_bet_closed(bet: dict, config: dict):
    mode = bet.get("mode", "paper").upper()
    status = bet["status"].upper()
    pnl = bet.get("pnl", 0) or 0
    fee = bet.get("fee_paid", 0.0)
    bal = _get_balance_str(config, bet.get("mode", "paper"))
    text = (
        f"<b>[{mode}] BET {status}</b>\n"
        f"Market: {bet['market_question'][:80]}\n"
        f"Side: {bet['side']} | P&L: ${pnl:+.4f} | Fee paid: ${fee:.4f}\n"
        f"Reason: {bet.get('close_reason', 'N/A')} | Balance: {bal}"
    )
    send_message(text, config)


# ---------------------------------------------------------------------------
# Opportunity alerts
# ---------------------------------------------------------------------------

def notify_herald_signal(signal: dict, config: dict):
    headlines = "\n".join(f"  • {h}" for h in signal.get("headlines", [])[:3])
    text = (
        f"<b>HERALD — BREAKING SIGNAL</b>\n"
        f"Keyword: <code>{signal['keyword']}</code>\n"
        f"Articles: {signal['article_count']} from {signal['source_count']} sources\n"
        f"Confidence boost: +{signal['boost']}%\n"
        f"Headlines:\n{headlines}"
    )
    send_message(text, config)


def notify_arbitrage(market: dict, config: dict):
    text = (
        f"<b>ARBITRAGE OPPORTUNITY</b>\n"
        f"Market: {market['question'][:80]}\n"
        f"YES: {market['yes_price']:.3f} | NO: {market['no_price']:.3f}\n"
        f"Sum: {market['yes_price'] + market['no_price']:.3f} (< 0.97)\n"
        f"Volume: ${market['volume']:,.0f}"
    )
    send_message(text, config)


def notify_sniper(market: dict, config: dict):
    text = (
        f"<b>SNIPER OPPORTUNITY</b>\n"
        f"Market: {market['question'][:80]}\n"
        f"YES price: {market['yes_price']:.3f} ({market['yes_price']*100:.1f}%)\n"
        f"Volume: ${market['volume']:,.0f} | Days: {market['days_to_resolution']:.0f}\n"
        f"Target: {market['yes_price'] * 2.5:.3f} (2.5x)"
    )
    send_message(text, config)


# ---------------------------------------------------------------------------
# Daily summary
# ---------------------------------------------------------------------------

def notify_daily_summary(summary: dict, config: dict):
    """
    Daily summary with separate paper and live sections.
    Includes fee drag, best/worst bet, HERALD signals ratio.
    """
    lines = [f"<b>ORACLE Daily Summary — {summary['date']}</b>"]

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
        lines.append(f"\n<b>[{mode.upper()}]</b>")
        lines.append(f"Balance: ${s['current_balance']:.4f} (start: ${s['starting_balance']:.4f})")
        lines.append(f"Today: {s['wins']}W/{s['losses']}L | WR: {wr} | P&amp;L: {pnl_sign}${s['today_pnl']:.4f}")
        lines.append(f"Fee drag: ${s['today_fees']:.4f} | All-time fees: ${s['total_fees_alltime']:.4f}")
        if s["best_bet"]:
            lines.append(f"Best bet: {s['best_bet']['question']} (${s['best_bet']['pnl']:+.4f})")
        if s["worst_bet"]:
            lines.append(f"Worst bet: {s['worst_bet']['question']} (${s['worst_bet']['pnl']:+.4f})")
        lines.append(f"Open positions: {s['open_bets']}")

    herald_ratio = (
        f"{summary['herald_bets']}/{summary['herald_signals']}"
        if summary["herald_signals"] > 0
        else "0/0"
    )
    lines.append(
        f"\nHERALD: {summary['herald_signals']} signals fired | "
        f"{summary['herald_bets']} bets triggered ({herald_ratio})"
    )
    lines.append(
        f"All-time: {summary['total_bets_alltime']} bets | "
        f"P&amp;L ${summary['overall_pnl_alltime']:+.4f}"
    )

    send_message("\n".join(lines), config)


def notify_gemini_cost(snapshot: dict, config: dict):
    """Send Gemini usage/cost snapshot to Telegram after a billed call."""
    if not snapshot:
        return

    call = snapshot.get("last_call", {})
    daily = snapshot.get("daily", {})
    monthly = snapshot.get("monthly", {})
    totals = snapshot.get("totals", {})

    text = (
        f"<b>GEMINI COST UPDATE</b>\n"
        f"Model: {call.get('model', 'gemini')} | Tier: {call.get('pricing', {}).get('tier', 'N/A')}\n"
        f"Call tokens — in: {call.get('prompt_tokens', 0):,}, out: {call.get('output_tokens', 0):,}, cache: {call.get('cache_tokens', 0):,}\n"
        f"Call cost: ${call.get('usd_cost', 0.0):.6f}\n"
        f"Today ({snapshot.get('day_key', '')}): ${daily.get('usd_cost', 0.0):.6f} ({daily.get('calls', 0)} calls)\n"
        f"Month ({snapshot.get('month_key', '')}): ${monthly.get('usd_cost', 0.0):.6f} ({monthly.get('calls', 0)} calls)\n"
        f"All-time: ${totals.get('usd_cost', 0.0):.6f} ({totals.get('calls', 0)} calls)"
    )
    send_message(text, config)


# ---------------------------------------------------------------------------
# Generic
# ---------------------------------------------------------------------------

def notify_error(error: str, config: dict):
    text = f"<b>ORACLE ERROR</b>\n<code>{_escape_html(error[:400])}</code>"
    send_message(text, config)


def notify_info(message: str, config: dict):
    text = f"<b>ORACLE</b>\n{_escape_html(message)}"
    send_message(text, config)


def notify_skip(market: dict, reason: str, config: dict):
    logger.debug("SKIP: %s — %s", market.get("question", "")[:50], reason)
    # Not sent to Telegram (too noisy)


# ---------------------------------------------------------------------------
# Incoming command polling
# ---------------------------------------------------------------------------

def poll_commands(config: dict, last_update_id: int | None = None, timeout: int = 0) -> tuple[list[dict], int | None]:
    """
    Poll Telegram updates and return parsed slash commands.
    Returns (commands, newest_update_id).
    """
    tg = config.get("telegram", {})
    token = tg.get("bot_token", "")
    chat_id = str(tg.get("chat_id", "")).strip()

    if not token or not chat_id:
        return [], last_update_id

    params = {
        "timeout": timeout,
        "allowed_updates": ["message"],
    }
    if last_update_id is not None:
        params["offset"] = last_update_id + 1

    try:
        r = _SESSION.get(_UPDATES_URL.format(token=token), params=params, timeout=10)
        r.raise_for_status()
        payload = r.json() or {}
        updates = payload.get("result", []) if payload.get("ok") else []

        commands: list[dict] = []
        newest = last_update_id

        for upd in updates:
            upd_id = upd.get("update_id")
            if isinstance(upd_id, int):
                newest = upd_id if newest is None else max(newest, upd_id)

            msg = upd.get("message") or {}
            text = (msg.get("text") or "").strip()
            incoming_chat_id = str((msg.get("chat") or {}).get("id", "")).strip()

            if incoming_chat_id != chat_id:
                continue
            if not text.startswith("/"):
                continue

            command, args = _parse_command(text)
            commands.append(
                {
                    "update_id": upd_id,
                    "chat_id": incoming_chat_id,
                    "user": (msg.get("from") or {}).get("username", "unknown"),
                    "text": text,
                    "command": command,
                    "args": args,
                }
            )

        return commands, newest
    except Exception as exc:
        logger.warning("Telegram command poll failed: %s", _redact_secrets(str(exc)))
        return [], last_update_id


def _parse_command(text: str) -> tuple[str, str]:
    # Supports command forms like /status and /status@BotName
    first, *rest = text.split(maxsplit=1)
    base = first.split("@", 1)[0].lower()
    args = rest[0] if rest else ""
    return base, args


def command_help_text() -> str:
    return (
        "<b>ORACLE Commands</b>\n"
        "/status — runtime state\n"
        "/balance — paper/live balances\n"
        "/openbets — list open bets\n"
        "/costs — Gemini costs\n"
        "/pause — pause scheduled scans\n"
        "/resume — resume scheduled scans\n"
        "/help — show this help"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_balance_str(config: dict, mode: str = "paper") -> str:
    try:
        from tools.memory import get_mode_balance
        bal = get_mode_balance(mode)
        return f"${bal['current']:.4f} USDC [{mode.upper()}]"
    except Exception:
        return "N/A"
