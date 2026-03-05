"""
main.py — ORACLE master orchestration loop.

Pipeline (every 15 minutes, or immediately on HERALD breaking signal):
  1. Halt check (balance / daily loss)
  2. Fetch markets from Polymarket
  3. Find sniper opportunities
  4. For each market:
     a. Arbitrage pre-check → BET_BOTH_SIDES if YES+NO < $0.97
     b. Inject HERALD boost if active
     c. Collect signals (sentiment.py)
     d. SCOUT analysis → skip if confidence < 55
     e. APEX decision → BET_YES / BET_NO / SKIP
     f. Execute bet (paper or real)
     g. Log + Telegram alert
  5. Daily summary at midnight UTC
"""

import logging
import logging.handlers
import time
from datetime import datetime, timezone
from pathlib import Path

import schedule

from config.loader import load_config
from agents.apex_agent import ApexAgent
from agents.scout_agent import ScoutAgent
from tools.herald import HeraldAgent
from tools.memory import (
    get_account_state,
    get_open_bets,
    get_pattern_summary_for_prompt,
    log_bet,
    update_balance,
    close_bet,
)
from tools.performance import format_daily_summary, get_daily_summary, should_halt
from tools.polymarket_tools import (
    check_arbitrage,
    fetch_markets,
    find_sniper_opportunities,
)
from tools.execution_router import route, check_open_positions, get_current_mode
from tools.sentiment import collect_signals
from tools.gemini_cost import get_gemini_cost_snapshot
from tools.telegram import (
    command_help_text,
    notify_arbitrage,
    notify_bet_placed,
    notify_daily_summary,
    notify_halt,
    notify_herald_signal,
    notify_info,
    notify_mode_switch,
    notify_shutdown,
    notify_sniper,
    notify_startup,
    poll_commands,
    send_message,
)

ROOT = Path(__file__).parent

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(config: dict):
    log_cfg = config.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO"))
    log_file = ROOT / log_cfg.get("file", "logs/oracle.log")
    log_file.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()

    # Idempotent setup: avoid duplicate handlers when setup_logging is called twice.
    if getattr(root_logger, "_oracle_logging_configured", False):
        root_logger.setLevel(level)
        return

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger.setLevel(level)

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    root_logger.addHandler(ch)

    # Rotating file handler
    fh = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=log_cfg.get("max_bytes", 10485760),
        backupCount=log_cfg.get("backup_count", 5),
        encoding="utf-8",
    )
    fh.setFormatter(formatter)
    root_logger.addHandler(fh)
    root_logger._oracle_logging_configured = True


logger = logging.getLogger("oracle.main")


# ---------------------------------------------------------------------------
# Core scan cycle
# ---------------------------------------------------------------------------

class Oracle:
    def __init__(self, config: dict):
        self.config = config
        self.betting_cfg = config.get("betting", {})

        # Active mode tracked for hot-switch detection
        self._active_mode = get_current_mode()

        self.scout = ScoutAgent(config)
        self.apex = ApexAgent(config)
        self.herald = HeraldAgent(config)

        # Flag set by HERALD callback to trigger immediate scan
        self._herald_triggered = False
        self._paused = False
        self._telegram_update_offset: int | None = None

        # HERALD signal/bet counters reset daily
        self._herald_signals_today = 0
        self._herald_bets_today = 0

        self.herald.on_breaking_signal = self._on_herald_signal

    def _on_herald_signal(self, signal: dict):
        logger.info("HERALD triggered scan — keyword: %s, boost: +%d%%", signal["keyword"], signal["boost"])
        notify_herald_signal(signal, self.config)
        self._herald_triggered = True
        self._herald_signals_today += 1

    def _check_mode_switch(self):
        """Detect and alert if paper_trading flag was toggled while running."""
        current_mode = get_current_mode()
        if current_mode != self._active_mode:
            prev = self._active_mode
            self._active_mode = current_mode
            logger.warning("MODE SWITCH DETECTED: %s → %s", prev, current_mode)
            notify_mode_switch(prev, current_mode, self.config)

    def run_scan(self):
        """Execute one full scan cycle."""
        if self._paused:
            logger.info("Scan skipped: ORACLE paused via Telegram command.")
            return

        logger.info("=== ORACLE SCAN START ===")
        self._herald_triggered = False

        # Detect mode switch before each cycle
        self._check_mode_switch()

        # Log current mode and balance at cycle start
        bal = get_account_state(mode=self._active_mode)
        logger.info(
            "Mode: %s | Balance: $%.4f | Open bets: %d",
            self._active_mode.upper(),
            bal.get("current_balance", 0.0),
            len(bal.get("open_bets", [])),
        )

        # 1. Halt check
        halt, reason = should_halt(self.config, mode=self._active_mode)
        if halt:
            notify_halt(self.config, reason)
            logger.warning("Scan aborted: %s", reason)
            return

        # 2. Fetch markets
        markets = fetch_markets(self.config)
        if not markets:
            logger.warning("No viable markets returned.")
            return

        # 3. Check open positions (fill limit orders, resolve closed markets)
        self._process_position_actions(check_open_positions(markets, self.config))

        # 4. Sniper pre-scan
        snipers = find_sniper_opportunities(markets)
        if snipers:
            logger.info("Sniper opportunities found: %d", len(snipers))
            for s in snipers[:3]:
                notify_sniper(s, self.config)

        # 5. Process each market
        for market in markets:
            try:
                self._process_market(market)
            except Exception as exc:
                logger.error("Market processing error (%s): %s", market.get("question", "")[:50], exc)

        logger.info("=== ORACLE SCAN COMPLETE ===")

    def _process_market(self, market: dict):
        question = market.get("question", "")[:60]

        # 4a. Arbitrage pre-check
        if self.betting_cfg.get("arbitrage_check", True) and check_arbitrage(market):
            logger.info("ARB: %s", question)
            notify_arbitrage(market, self.config)
            self._execute_arb(market)
            return

        # 4b. HERALD boost
        all_keywords = (
            self.config.get("herald", {}).get("crypto_keywords", [])
            + self.config.get("herald", {}).get("geopolitical_keywords", [])
        )
        topic_keywords = [kw for kw in all_keywords if kw.lower() in question.lower()]
        herald_boost = self.herald.get_boost_for_topic(topic_keywords)

        # 4c. Collect signals
        signals = collect_signals(market, self.config, herald_boost=herald_boost)

        # 4d. SCOUT analysis
        scout_result = self.scout.analyze(signals)
        if not scout_result:
            logger.debug("SCOUT returned no result for: %s", question)
            return

        confidence = scout_result.get("confidence", 0)
        sentiment = scout_result.get("sentiment", "NEUTRAL")

        if not self.scout.should_escalate(scout_result):
            logger.info(
                "SCOUT SKIP (conf=%d%%, %s): %s", confidence, sentiment, question
            )
            return

        logger.info("SCOUT ESCALATE (conf=%d%%, %s): %s", confidence, sentiment, question)

        # 4e. APEX decision
        account_state = get_account_state(mode=self._active_mode)
        pattern_summary = get_pattern_summary_for_prompt(
            market.get("category", "crypto"), confidence
        )
        apex_result = self.apex.decide(market, scout_result, account_state, pattern_summary)

        if not apex_result:
            logger.warning("APEX returned no result for: %s", question)
            return

        action = apex_result.get("action", "SKIP")

        if action == "SKIP":
            logger.info("APEX SKIP (%s): %s", apex_result.get("skip_reason", "N/A"), question)
            return

        # 4f. Execute bet
        side = apex_result.get("bet_side")
        bet_usdc = apex_result.get("bet_usdc")
        if not side or not bet_usdc:
            logger.error("APEX returned invalid bet params: %s", apex_result)
            return

        result = route(
            market, side, bet_usdc, self.config,
            exit_target_price=apex_result.get("exit_target_price"),
        )
        if not result.get("success"):
            logger.error("Bet execution failed: %s", result.get("error"))
            return

        # 4g. Log + Telegram
        entry_price = result.get("executed_price", market.get("yes_price" if side == "YES" else "no_price", 0.5))
        mode = result.get("mode", self._active_mode)
        bet_record = log_bet(
            market_id=market["condition_id"],
            market_question=market["question"],
            side=side,
            bet_usdc=bet_usdc,
            entry_price=entry_price,
            scout_confidence=scout_result.get("confidence", 0),
            apex_confidence=apex_result.get("apex_confidence", 0),
            category=market.get("category", "crypto"),
            exit_strategy=apex_result.get("exit_strategy", "HOLD_TO_RESOLUTION"),
            exit_target_price=apex_result.get("exit_target_price"),
            telegram_message=apex_result.get("telegram_message", ""),
            mode=mode,
            fee_paid=result.get("fee_paid", 0.0),
            order_type=result.get("order_type", "market"),
            fill_status=result.get("fill_status", "open"),
        )
        notify_bet_placed(bet_record, self.config)
        self._herald_bets_today += 1 if self._herald_triggered else 0

        # Deduct bet from balance (paper only; live balance is updated on fill)
        if mode == "paper":
            account = get_account_state(mode="paper")
            new_balance = account["current_balance"] - bet_usdc
            update_balance(new_balance, mode="paper", reason=f"Paper bet placed: {bet_record['id']}")

    def _execute_arb(self, market: dict):
        """Place both sides of an arbitrage opportunity."""
        for side in ("YES", "NO"):
            result = route(market, side, 1.0, self.config)
            if result.get("success"):
                entry_price = result.get("executed_price", 0.5)
                mode = result.get("mode", self._active_mode)
                bet_record = log_bet(
                    market_id=market["condition_id"],
                    market_question=market["question"],
                    side=side,
                    bet_usdc=1.0,
                    entry_price=entry_price,
                    scout_confidence=90,
                    apex_confidence=90,
                    category=market.get("category", "crypto"),
                    exit_strategy="HOLD_TO_RESOLUTION",
                    exit_target_price=None,
                    telegram_message=f"Arbitrage: {side} side",
                    mode=mode,
                    fee_paid=result.get("fee_paid", 0.0),
                    order_type=result.get("order_type", "market"),
                    fill_status=result.get("fill_status", "open"),
                )
                notify_bet_placed(bet_record, self.config)
                if mode == "paper":
                    account = get_account_state(mode="paper")
                    update_balance(account["current_balance"] - 1.0, mode="paper", reason=f"Arb paper bet: {side}")

    def _process_position_actions(self, actions: list[dict]):
        """Handle actions returned by check_open_positions (fills, resolutions, escalations)."""
        for action in actions:
            act = action.get("action")
            bet_id = action.get("bet_id", "?")

            if act == "fill":
                from tools.memory import mark_limit_filled
                mark_limit_filled(bet_id, action.get("fill_price", 0.0))
                logger.info("Limit order filled: %s @ %.3f", bet_id, action.get("fill_price", 0.0))

            elif act == "close":
                pnl = action.get("pnl", 0.0)
                close_bet(
                    bet_id=bet_id,
                    status=action.get("status", "won"),
                    pnl=pnl,
                    close_reason=action.get("reason", "resolved"),
                )
                logger.info("Position closed: %s | P&L: %+.4f | reason: %s", bet_id, pnl, action.get("reason"))

            elif act == "escalate_to_market":
                logger.info("Escalating limit → market: %s", bet_id)
                # Re-route as a market order
                from tools.memory import get_all_bets
                all_bets = get_all_bets()
                bet = next((b for b in all_bets if b["id"] == bet_id), None)
                if bet:
                    result = route(
                        {"condition_id": bet["market_id"], "question": bet["market_question"],
                         "yes_price": bet["entry_price"], "no_price": 1 - bet["entry_price"]},
                        bet["side"], bet["bet_usdc"], self.config,
                        order_type="market",
                    )
                    if result.get("success"):
                        from tools.memory import mark_limit_filled
                        mark_limit_filled(bet_id, result.get("executed_price", bet["entry_price"]))
                    else:
                        logger.error("Market escalation failed for %s: %s", bet_id, result.get("error"))

            else:
                logger.debug("Unknown position action '%s' for bet %s", act, bet_id)

    def run_daily_summary(self):
        summary = get_daily_summary(
            self.config,
            herald_signals=self._herald_signals_today,
            herald_bets=self._herald_bets_today,
        )
        text = format_daily_summary(summary)
        logger.info("Daily summary:\n%s", text)
        notify_daily_summary(summary, self.config)
        # Reset daily counters
        self._herald_signals_today = 0
        self._herald_bets_today = 0

    def _poll_telegram_commands(self):
        commands, newest = poll_commands(self.config, self._telegram_update_offset, timeout=0)
        if newest is not None:
            self._telegram_update_offset = newest
        for cmd in commands:
            self._handle_telegram_command(cmd)

    def _handle_telegram_command(self, cmd: dict):
        command = cmd.get("command", "")
        logger.info("Telegram command received: %s", command)

        if command in ("/help", "/start"):
            send_message(command_help_text(), self.config)
            return

        if command == "/status":
            mode = self._active_mode.upper()
            account = get_account_state(mode=self._active_mode)
            text = (
                f"<b>ORACLE STATUS</b>\n"
                f"Mode: {mode}\n"
                f"Paused: {'YES' if self._paused else 'NO'}\n"
                f"Balance: ${account.get('current_balance', 0.0):.4f}\n"
                f"Open bets: {account.get('open_bet_count', 0)}\n"
                f"HERALD signals today: {self._herald_signals_today}"
            )
            send_message(text, self.config)
            return

        if command == "/balance":
            paper = get_account_state(mode="paper")
            live = get_account_state(mode="live")
            text = (
                f"<b>ORACLE BALANCES</b>\n"
                f"Paper: ${paper.get('current_balance', 0.0):.4f} (start ${paper.get('starting_balance', 0.0):.4f})\n"
                f"Live: ${live.get('current_balance', 0.0):.4f} (start ${live.get('starting_balance', 0.0):.4f})"
            )
            send_message(text, self.config)
            return

        if command == "/openbets":
            mode = self._active_mode
            open_bets = [b for b in get_open_bets() if b.get("mode") == mode]
            if not open_bets:
                send_message(f"<b>OPEN BETS [{mode.upper()}]</b>\nNone.", self.config)
                return

            lines = [f"<b>OPEN BETS [{mode.upper()}]</b> ({len(open_bets)})"]
            for b in open_bets[:8]:
                lines.append(
                    f"- {b.get('side')} | ${b.get('bet_usdc', 0):.2f} @ {b.get('entry_price', 0):.3f} | "
                    f"{b.get('market_question', '')[:55]}"
                )
            if len(open_bets) > 8:
                lines.append(f"... and {len(open_bets) - 8} more")
            send_message("\n".join(lines), self.config)
            return

        if command == "/costs":
            snap = get_gemini_cost_snapshot()
            last = snap.get("last_call") or {}
            text = (
                f"<b>GEMINI COSTS</b>\n"
                f"Today ({snap.get('day_key', '')}): ${snap.get('daily', {}).get('usd_cost', 0.0):.6f}\n"
                f"Month ({snap.get('month_key', '')}): ${snap.get('monthly', {}).get('usd_cost', 0.0):.6f}\n"
                f"All-time: ${snap.get('totals', {}).get('usd_cost', 0.0):.6f}"
            )
            if last:
                text += (
                    f"\nLast call: ${last.get('usd_cost', 0.0):.6f} "
                    f"(in {last.get('prompt_tokens', 0):,}, out {last.get('output_tokens', 0):,})"
                )
            send_message(text, self.config)
            return

        if command == "/pause":
            self._paused = True
            send_message("<b>ORACLE</b>\nScheduled scans paused. Use /resume to continue.", self.config)
            return

        if command == "/resume":
            self._paused = False
            send_message("<b>ORACLE</b>\nScheduled scans resumed.", self.config)
            return

        send_message("<b>Unknown command.</b>\nUse /help", self.config)

    def _log_dev_mode_summary(self):
        """Print a clear summary of which dev_flags are active/disabled."""
        dev = self.config.get("dev_flags", {})
        if not dev:
            return

        enabled = [k for k, v in dev.items() if v is True]
        disabled = [k.replace("use_", "") for k, v in dev.items() if v is False and k.startswith("use_")]
        mocked = [k.replace("mock_", "") for k, v in dev.items() if v is True and k.startswith("mock_")]

        logger.info(
            "[DEV MODE] Active flags: %s",
            ", ".join(f"{k}=true" for k in enabled) or "none",
        )
        if disabled:
            logger.info("[DEV MODE] Disabled: %s", ", ".join(disabled))
        if mocked:
            logger.info("[DEV MODE] Mocked: %s", ", ".join(mocked))

    def start(self):
        setup_logging(self.config)
        logger.info("ORACLE v3.0 starting. Mode: %s", self._active_mode.upper())
        self._log_dev_mode_summary()
        notify_startup(self.config, paper=(self._active_mode == "paper"))

        # Start HERALD background thread (skipped if use_herald=false)
        if self.config.get("dev_flags", {}).get("use_herald", True):
            self.herald.start()
        else:
            logger.info("[DEV] use_herald=false — HERALD background thread not started")

        # Schedule scan every N minutes
        interval = self.betting_cfg.get("scan_interval_minutes", 15)
        schedule.every(interval).minutes.do(self.run_scan)

        # Daily summary at midnight UTC
        schedule.every().day.at("00:00").do(self.run_daily_summary)

        # Run an immediate first scan
        self.run_scan()

        logger.info("ORACLE running. Scan interval: %d minutes.", interval)
        try:
            while True:
                self._poll_telegram_commands()
                schedule.run_pending()

                # HERALD can trigger an immediate scan
                if self._herald_triggered:
                    logger.info("Immediate HERALD scan triggered.")
                    self.run_scan()

                time.sleep(5)
        except KeyboardInterrupt:
            logger.info("ORACLE shutting down (KeyboardInterrupt).")
            notify_shutdown(self.config, reason="Manual stop (KeyboardInterrupt)")
        finally:
            if self.config.get("dev_flags", {}).get("use_herald", True):
                self.herald.stop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    config = load_config()
    setup_logging(config)
    oracle = Oracle(config)
    oracle.start()
