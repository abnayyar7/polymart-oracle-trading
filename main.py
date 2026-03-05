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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    get_patterns,
    has_open_position,
    log_bet,
    update_balance,
    close_bet,
)
from tools.performance import format_daily_summary, get_daily_summary, should_halt
from tools.polymarket_tools import (
    check_arbitrage,
    fetch_markets,
    find_sniper_opportunities,
    prefilter_markets_for_scout,
)
from tools.execution_router import route, get_current_mode
from tools.exit_manager import run_exit_checks
from tools.edge_calculator import calculate_edge
from tools.bayesian_engine import update_posterior
from tools.stats_guard import get_guarded_summary
from tools.market_gate import check_market
from tools.market_matcher import match_markets_to_signal
from tools.sentiment import collect_signals
from tools.gemini_cost import get_gemini_cost_snapshot
from tools.telegram import (
    command_help_text,
    notify_apex_decision,
    notify_arbitrage,
    notify_bet_placed,
    notify_cycle_summary,
    notify_daily_summary,
    notify_halt,
    notify_herald_signal,
    notify_info,
    notify_mode_switch,
    notify_scout_analyzing,
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

_analysis_cache: dict[str, dict] = {}
CACHE_TTL_HOURS = 4


def is_cached(market_id: str) -> bool:
    """Return True if market was analyzed recently and should be skipped."""
    if not market_id:
        return False
    entry = _analysis_cache.get(market_id)
    if not entry:
        return False

    ts = entry.get("timestamp")
    if not isinstance(ts, datetime):
        _analysis_cache.pop(market_id, None)
        return False

    age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
    if age_hours > CACHE_TTL_HOURS:
        _analysis_cache.pop(market_id, None)
        return False
    return True


def cache_result(market_id: str, result: str, reason: str = "", question: str = ""):
    if not market_id:
        return
    _analysis_cache[market_id] = {
        "result": result,
        "reason": reason,
        "question": question,
        "timestamp": datetime.now(timezone.utc),
    }


def invalidate_cache_for_topic(keyword: str):
    """Invalidate cached analyses that match a HERALD topic keyword."""
    kw = str(keyword or "").strip().lower()
    if not kw:
        return

    to_delete: list[str] = []
    for market_id, entry in _analysis_cache.items():
        q = str(entry.get("question", "")).lower()
        if kw in q:
            to_delete.append(market_id)

    for market_id in to_delete:
        _analysis_cache.pop(market_id, None)

    if to_delete:
        logger.info("Cache invalidated %d markets for keyword: %s", len(to_delete), keyword)


# ---------------------------------------------------------------------------
# Core scan cycle
# ---------------------------------------------------------------------------

class Oracle:
    def __init__(self, config: dict):
        self.config = config
        self.betting_cfg = config.get("betting", {})
        self.scout_parallel_workers = int(self.betting_cfg.get("scout_parallel_workers", 3))
        self.cycle_timeout_seconds = int(self.betting_cfg.get("cycle_timeout_seconds", 14 * 60))

        # Active mode tracked for hot-switch detection
        self._active_mode = get_current_mode()

        self.scout = ScoutAgent(config)
        self.apex = ApexAgent(config)
        self.herald = HeraldAgent(config)

        # Flag set by HERALD callback to trigger immediate scan
        self._herald_triggered = False
        # The signal that triggered the current scan (keyword + boost info)
        self._active_herald_signal: dict | list[dict] | None = None
        self._paused = False
        self._telegram_update_offset: int | None = None

        # HERALD signal/bet counters reset daily
        self._herald_signals_today = 0
        self._herald_bets_today = 0

        # Per-cycle counters (reset at start of each run_scan)
        self._cycle_bets = 0
        self._cycle_skips = 0
        self._markets_to_apex = 0
        self._gate_stats: dict[str, int] = {}

        self.herald.on_breaking_signal = self._on_herald_signal

    def _on_herald_signal(self, signal: dict):
        logger.info("HERALD triggered scan — keyword: %s, boost: +%d%%", signal["keyword"], signal["boost"])
        notify_herald_signal(signal, self.config)
        invalidate_cache_for_topic(signal.get("keyword", ""))
        self._herald_triggered = True
        self._active_herald_signal = signal
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
        cycle_start = time.monotonic()
        self._herald_triggered = False

        # Reset per-cycle counters
        self._cycle_bets = 0
        self._cycle_skips = 0
        self._markets_to_apex = 0
        self._gate_stats = {}

        # Capture and clear the active HERALD signal before any await/fetch.
        # Runtime guard: this may be a dict, list, or None.
        active_signal = self._active_herald_signal
        self._active_herald_signal = None

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

        # 3. Check open positions (fill limit orders, resolve closed markets, exit triggers)
        self._process_position_actions(
            run_exit_checks(markets, self.config, herald_agent=self.herald)
        )

        # 4. Sniper pre-scan
        snipers = find_sniper_opportunities(markets)
        if snipers:
            logger.info("Sniper opportunities found: %d", len(snipers))
            for s in snipers[:3]:
                notify_sniper(s, self.config)

        # 5. HERALD market matching — prioritize markets relevant to the breaking signal
        markets_to_scan = markets
        active_signal_for_matching = None
        if isinstance(active_signal, dict):
            active_signal_for_matching = active_signal
        elif isinstance(active_signal, list) and active_signal:
            active_signal_for_matching = active_signal[0]

        if active_signal_for_matching:
            matched = match_markets_to_signal(active_signal_for_matching, markets)
            if matched:
                notify_scout_analyzing(active_signal_for_matching, matched, self.config)
                # Process matched markets first, then remaining (dedup by condition_id)
                matched_ids = {m["condition_id"] for m in matched}
                remaining = [m for m in markets if m["condition_id"] not in matched_ids]
                markets_to_scan = matched + remaining
                logger.info(
                    "HERALD prioritized %d matched markets for keyword '%s'",
                    len(matched), active_signal_for_matching.get("keyword"),
                )
            else:
                logger.info(
                    "No markets matched HERALD keyword '%s' — processing all %d markets",
                    active_signal_for_matching.get("keyword", "?"), len(markets),
                )

        # 6. Pre-SCOUT filtering + prioritization
        herald_signals_raw = self.herald.get_active_signals()
        if isinstance(herald_signals_raw, dict):
            herald_signals = [herald_signals_raw]
        elif isinstance(herald_signals_raw, list):
            herald_signals = [s for s in herald_signals_raw if isinstance(s, dict)]
        else:
            herald_signals = []

        if active_signal is None:
            herald_signals = herald_signals
        elif isinstance(active_signal, dict):
            herald_signals = [active_signal] + herald_signals
        elif isinstance(active_signal, list):
            herald_signals = [s for s in active_signal if isinstance(s, dict)] + herald_signals
        else:
            herald_signals = herald_signals

        prefiltered_markets, filter_stats = prefilter_markets_for_scout(markets_to_scan, herald_signals)
        logger.info(
            "PRE-FILTER SUMMARY: total=%d pass=%d reject_filter_a=%d reject_null_volume=%d reject_low_volume=%d reject_score=%d",
            filter_stats["total"],
            filter_stats["passed"],
            filter_stats["reject_filter_a"],
            filter_stats["reject_null_volume"],
            filter_stats["reject_low_volume"],
            filter_stats["reject_score"],
        )

        scout_queue: list[dict] = []
        for market in prefiltered_markets:
            try:
                market_id = market.get("condition_id", "")
                if has_open_position(market_id):
                    logger.info(
                        "DUPLICATE SKIP: Already have open position on %s",
                        market.get("question", "")[:80],
                    )
                    continue

                if is_cached(market_id):
                    logger.debug("CACHE SKIP: %s", market.get("question", "")[:80])
                    continue

                if self.betting_cfg.get("arbitrage_check", True) and check_arbitrage(market):
                    logger.info("ARB: %s", market.get("question", "")[:60])
                    notify_arbitrage(market, self.config)
                    self._execute_arb(market)
                    continue
                scout_queue.append(market)
            except Exception as exc:
                logger.error("Arbitrage pre-check error (%s): %s", market.get("question", "")[:50], exc)

        # 7. Parallel SCOUT batches with cycle timeout guard
        analyzed_count = 0
        total_candidates = len(scout_queue)
        timed_out = False
        workers = max(1, self.scout_parallel_workers)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            for idx in range(0, total_candidates, workers):
                elapsed = time.monotonic() - cycle_start
                if elapsed > self.cycle_timeout_seconds:
                    timed_out = True
                    logger.warning(
                        "CYCLE TIMEOUT: analyzed %d/%d markets",
                        analyzed_count,
                        total_candidates,
                    )
                    break

                batch = scout_queue[idx: idx + workers]
                logger.info("SCOUT PARALLEL: analyzing %d markets simultaneously", len(batch))
                futures = [executor.submit(self._collect_signals_and_scout, market) for market in batch]

                batch_results: list[tuple[dict, dict, dict | None]] = []
                for future in as_completed(futures):
                    try:
                        batch_results.append(future.result())
                    except Exception as exc:
                        logger.error("Parallel SCOUT worker failed: %s", exc)

                analyzed_count += len(batch_results)
                for market, signals, scout_result in batch_results:
                    try:
                        self._process_market_with_scout(market, signals, scout_result)
                    except Exception as exc:
                        logger.error("Market processing error (%s): %s", market.get("question", "")[:50], exc)

        if timed_out:
            # On timeout, do not enqueue new SCOUT work; just run exit checks and close cycle.
            self._process_position_actions(
                run_exit_checks(markets, self.config, herald_agent=self.herald)
            )

        # 7. Cycle summary
        final_bal = get_account_state(mode=self._active_mode)
        notify_cycle_summary(
            {
                "markets_scanned": len(markets_to_scan),
                "markets_prefiltered": len(prefiltered_markets),
                "markets_analyzed": analyzed_count,
                "markets_candidates": total_candidates,
                "markets_to_apex": self._markets_to_apex,
                "signals_fired": len(self.herald.get_active_signals()),
                "bets_placed": self._cycle_bets,
                "skipped": self._cycle_skips,
                "gate_stats": dict(self._gate_stats),
                "balance": final_bal.get("current_balance", 0.0),
            },
            self.config,
        )

        logger.info("=== ORACLE SCAN COMPLETE ===")

    def _collect_signals_and_scout(self, market: dict) -> tuple[dict, dict, dict | None]:
        question = market.get("question", "")[:60]

        # HERALD boost — check all keyword categories, not just crypto + geo
        herald_cfg = self.config.get("herald", {})
        all_keywords: list[str] = []
        for kw_key in (
            "crypto_keywords", "geopolitical_keywords",
            "sports_keywords", "entertainment_keywords", "science_keywords",
        ):
            all_keywords.extend(herald_cfg.get(kw_key, []))
        topic_keywords = [kw for kw in all_keywords if kw.lower() in question.lower()]
        herald_boost = self.herald.get_boost_for_topic(topic_keywords)

        # Collect signals + SCOUT analysis inside worker thread.
        logger.info("Sending market to SCOUT: %s", question)
        signals = collect_signals(market, self.config, herald_boost=herald_boost)
        scout_result = self.scout.analyze(signals)
        return market, signals, scout_result

    def _process_market_with_scout(self, market: dict, signals: dict, scout_result: dict | None):
        question = market.get("question", "")[:60]
        if not scout_result:
            logger.debug("SCOUT returned no result for: %s", question)
            return

        confidence = scout_result.get("confidence", 0)
        sentiment = scout_result.get("sentiment", "NEUTRAL")
        logger.info("SCOUT confidence: %d%% for %s", confidence, question)

        if not self.scout.should_escalate(scout_result):
            logger.info(
                "SCOUT SKIP (conf=%d%%, %s): %s", confidence, sentiment, question
            )
            cache_result(market.get("condition_id", ""), "SCOUT_SKIP", sentiment, market.get("question", ""))
            return

        logger.info("SCOUT ESCALATE (conf=%d%%, %s): %s", confidence, sentiment, question)

        # 4e. Edge calculation — skip if no positive expected value
        edge_result = calculate_edge(market, scout_result)
        if edge_result.get("skip"):
            logger.info(
                "EDGE SKIP (%s): %s", edge_result.get("skip_reason", "no edge"), question
            )
            self._cycle_skips += 1
            cache_result(
                market.get("condition_id", ""),
                "EDGE_SKIP",
                edge_result.get("skip_reason", "no_edge"),
                market.get("question", ""),
            )
            return

        # 4e-ii. Market gate — deterministic quality filter before Gemini
        gate_ok, gate_reason = check_market(market, scout_result)
        if not gate_ok:
            rule = gate_reason.split(":")[0]
            self._gate_stats[rule] = self._gate_stats.get(rule, 0) + 1
            self._cycle_skips += 1
            cache_result(market.get("condition_id", ""), "GATE_SKIP", gate_reason, market.get("question", ""))
            return

        self._markets_to_apex += 1

        # 4f. Bayesian posterior update
        herald_active = signals.get("herald_active", False)
        posterior_result = update_posterior(market, scout_result, signals, herald_active=herald_active)

        # 4g. APEX decision
        logger.info("Escalating to APEX: %s", question)
        account_state = get_account_state(mode=self._active_mode)
        pattern_summary = get_guarded_summary(
            get_patterns(),
            market.get("category", "crypto"),
            confidence,
        )
        suggested_bet_side = edge_result.get("side")
        apex_result = self.apex.decide(
            market, scout_result, account_state, pattern_summary,
            edge_result=edge_result, posterior_result=posterior_result,
            suggested_bet_side=suggested_bet_side,
        )

        if not apex_result:
            logger.warning("APEX returned no result for: %s", question)
            return

        action = apex_result.get("action", "SKIP")

        if action == "SKIP":
            logger.info("APEX SKIP (%s): %s", apex_result.get("skip_reason", "N/A"), question)
            self._cycle_skips += 1
            notify_apex_decision(market, scout_result, apex_result, account_state, self.config)
            cache_result(
                market.get("condition_id", ""),
                "APEX_SKIP",
                apex_result.get("skip_reason", "N/A"),
                market.get("question", ""),
            )
            return

        # 4h. Execute bet
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
        self._cycle_bets += 1
        self._herald_bets_today += 1 if self._herald_triggered else 0
        cache_result(
            market.get("condition_id", ""),
            "BET_PLACED",
            side,
            market.get("question", ""),
        )

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
                closed = close_bet(
                    bet_id=bet_id,
                    close_price=action.get("close_price", 0.0),
                    close_reason=action.get("reason", "resolved"),
                )
                if closed:
                    from tools.telegram import notify_bet_closed
                    notify_bet_closed(closed, self.config)
                    logger.info(
                        "Position closed: %s | status=%s | P&L: %+.4f | reason: %s",
                        bet_id, closed.get("status"), closed.get("pnl", 0.0), action.get("reason"),
                    )

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

        if command == "/bets":
            from tools.memory import get_all_bets
            mode = self._active_mode
            all_bets = get_all_bets()
            recent = [b for b in reversed(all_bets) if b.get("mode") == mode][:10]
            if not recent:
                send_message(f"<b>RECENT BETS [{mode.upper()}]</b>\nNone yet.", self.config)
                return
            lines = [f"<b>RECENT BETS [{mode.upper()}]</b> (last {len(recent)})"]
            for b in recent:
                status = b.get("status", "open").upper()
                pnl_str = f"${b['pnl']:+.4f}" if b.get("pnl") is not None else "open"
                lines.append(
                    f"- [{status}] {b.get('side')} | {pnl_str} | "
                    f"{b.get('market_question', '')[:50]}"
                )
            send_message("\n".join(lines), self.config)
            return

        if command == "/pnl":
            from tools.memory import get_patterns, get_mode_balance
            patterns = get_patterns()
            paper_bal = get_mode_balance("paper")
            live_bal = get_mode_balance("live")
            total = patterns.get("total_bets", 0)
            wins = patterns.get("total_wins", 0)
            losses = patterns.get("total_losses", 0)
            pnl = patterns.get("overall_pnl", 0.0)
            wr = f"{wins/(wins+losses)*100:.0f}%" if (wins + losses) > 0 else "N/A"
            paper_mode = patterns.get("by_mode", {}).get("paper", {})
            live_mode = patterns.get("by_mode", {}).get("live", {})
            text = (
                f"<b>ORACLE P&amp;L SUMMARY</b>\n"
                f"Total bets: {total} | W/L: {wins}/{losses} | WR: {wr}\n"
                f"Overall P&amp;L: ${pnl:+.4f}\n\n"
                f"[PAPER] Balance: ${paper_bal['current']:.4f} | "
                f"P&amp;L: ${paper_mode.get('total_pnl', 0.0):+.4f} | "
                f"Fees: ${paper_mode.get('total_fees', 0.0):.4f}\n"
                f"[LIVE]  Balance: ${live_bal['current']:.4f} | "
                f"P&amp;L: ${live_mode.get('total_pnl', 0.0):+.4f} | "
                f"Fees: ${live_mode.get('total_fees', 0.0):.4f}"
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

    def _run_health_check(self) -> dict:
        """
        Test all configured components and return a health-status dict.
        Values: True = OK, False = FAILING, None = disabled via dev_flags.
        """
        import requests as _req
        dev = self.config.get("dev_flags", {})
        health: dict[str, bool | None] = {}

        # HERALD
        health["herald"] = self.config.get("herald", {}).get("enabled", True)

        # SCOUT — Ollama reachability
        if dev.get("use_ollama", True):
            try:
                ollama_base = self.config.get("ollama", {}).get("base_url", "http://localhost:11434")
                r = _req.get(f"{ollama_base}/api/tags", timeout=5)
                health["scout"] = r.status_code == 200
            except Exception:
                health["scout"] = False
        else:
            health["scout"] = None  # disabled

        # APEX — Gemini API key configured
        if dev.get("use_gemini", True):
            key = self.config.get("gemini", {}).get("api_key", "")
            health["gemini"] = bool(key and not key.startswith("YOUR_"))
        else:
            health["gemini"] = None  # disabled

        # Polymarket CLOB reachability
        if dev.get("use_polymarket_api", True):
            try:
                host = self.config.get("polymarket", {}).get("host", "https://clob.polymarket.com")
                r = _req.get(f"{host}/markets", params={"active": "true", "limit": 1}, timeout=10)
                health["polymarket"] = r.status_code == 200
            except Exception:
                health["polymarket"] = False
        else:
            health["polymarket"] = None  # disabled

        # Log summary
        for component, status in health.items():
            if status is False:
                logger.error("HEALTH CHECK FAILED: %s", component.upper())
            elif status is None:
                logger.info("HEALTH CHECK DISABLED: %s", component.upper())
            else:
                logger.info("HEALTH CHECK OK: %s", component.upper())

        return health

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

        # Health check — test all components before starting
        health = self._run_health_check()
        notify_startup(self.config, paper=(self._active_mode == "paper"), health=health)

        # Block on critical failures (only when the component is actually enabled)
        dev = self.config.get("dev_flags", {})
        if health.get("scout") is False and dev.get("use_ollama", True):
            logger.error("ORACLE HALT: Ollama is not running. Start it with: ollama serve")
            return
        if health.get("gemini") is False and dev.get("use_gemini", True):
            logger.error("ORACLE HALT: Gemini API key is not configured in config.json")
            return

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
