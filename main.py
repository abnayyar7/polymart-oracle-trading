"""Deterministic ORACLE main loop.

Pipeline:
market_data -> feature_engineering -> deterministic_probability_model ->
bayesian_probability_update -> edge_calculation -> risk_filter ->
execution_router -> memory + telemetry
"""

from __future__ import annotations

import atexit
import logging
import logging.handlers
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from config.loader import load_config
from config import trading_config as tc
from core.bayesian_engine import bayesian_update
from core.convergence_strategy import find_convergence_trades
from core.edge_calculator import calculate_edge
from core.execution_router import increment_cycle_skip_reason, init_cycle_skip_reasons, log_cycle_summary
from core.market_features import extract_market_features
from core.probability_model import compute_model_probability
from tools.execution_router import execute_trade, get_current_mode
from tools.exit_manager import log_shadow_exits
from tools.paper_trader import (
    can_trade,
    check_and_resolve_positions,
    generate_daily_report,
    get_manual_cleanup_needed,
    load_daily_stats,
    load_wallet,
    place_limit_order,
    reset_daily_stats,
)
from tools.polymarket_tools import fetch_convergence_markets, fetch_markets
from tools.telegram import (
    command_help_text,
    escape_html,
    poll_commands,
    send_message,
)

ROOT = Path(__file__).parent
logger = logging.getLogger(__name__)


class DeterministicOracle:
    def __init__(self, config: dict):
        self.config = config
        self.scan_interval_minutes = int(config.get("betting", {}).get("scan_interval_minutes", 15))
        self.model_weights = config.get("model_weights", {})
        self.bayes_strength = float(config.get("betting", {}).get("bayes_strength", 0.35))

        # Rolling local history for deterministic momentum/volatility calculation.
        self._price_history: dict[str, list[float]] = {}
        self._volume_history: dict[str, list[float]] = {}
        self._last_day = datetime.now().strftime("%Y-%m-%d")
        self._last_full_scan = 0.0
        self._pending_exits: dict[str, float] = {} # trade_id -> timestamp

    def _maybe_reset_daily_stats(self):
        day_now = datetime.now().strftime("%Y-%m-%d")
        if day_now == self._last_day:
            return

        report = generate_daily_report()
        _send_daily_report_telegram(report)
        reset_daily_stats()
        self._last_day = day_now

    def _update_market_history(self, market: dict):
        market_id = str(market.get("condition_id") or market.get("id") or "")
        if not market_id:
            return

        price_list = self._price_history.setdefault(market_id, [])
        volume_list = self._volume_history.setdefault(market_id, [])

        price_list.append(float(market.get("yes_price", 0.5)))
        volume_list.append(float(market.get("volume", 0.0) or 0.0))

        if len(price_list) > 30:
            self._price_history[market_id] = price_list[-30:]
        if len(volume_list) > 30:
            self._volume_history[market_id] = volume_list[-30:]

    def _risk_allows_trade(self) -> tuple[bool, str]:
        wallet = load_wallet()
        daily = load_daily_stats()
        return can_trade(wallet=wallet, daily_stats=daily)

    def _get_adaptive_interval(self, wallet: dict) -> int:
        """Return sleep interval in seconds based on open position state."""
        from datetime import datetime, timezone

        open_positions = wallet.get("open_positions", [])
        if not open_positions:
            return self.scan_interval_minutes * 60

        now = datetime.now(timezone.utc)
        for pos in open_positions:
            end_date = pos.get("end_date_iso") or pos.get("end_date")
            if end_date:
                try:
                    dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
                    hours_left = (dt - now).total_seconds() / 3600
                    if hours_left <= 1:
                        return 20
                    if hours_left <= 24:
                        return 60
                except Exception:
                    pass

        return 90

    def _monitor_open_positions(self):
        """Fast loop: fetch current prices for open positions and check exits."""
        from tools.polymarket_tools import fetch_market_by_id
        wallet = load_wallet()
        open_positions = wallet.get("open_positions", [])
        
        # Clean up pending exits (TTL = 10 minutes)
        now = time.time()
        expired = [tid for tid, ts in self._pending_exits.items() if now - ts > 600]
        for tid in expired:
            logger.info("[MONITOR] TTL expired for pending exit %s, clearing cooldown.", tid)
            del self._pending_exits[tid]

        if not open_positions:
            self._pending_exits.clear()
            return

        # Also clean up IDs no longer in open_positions (position closed/missing)
        current_trade_ids = {p.get("trade_id") for p in open_positions if p.get("trade_id")}
        for tid in list(self._pending_exits.keys()):
            if tid not in current_trade_ids:
                logger.debug("[MONITOR] Clearing pending exit %s (position closed/missing)", tid)
                del self._pending_exits[tid]

        logger.info("[MONITOR] Checking %d open positions...", len(open_positions))
        current_market_data = []
        for pos in open_positions:
            mid = pos.get("market_id")
            if mid:
                mkt = fetch_market_by_id(mid)
                if mkt:
                    current_market_data.append(mkt)

        if not current_market_data:
            return

        # 1. Log shadow exits for research
        log_shadow_exits(open_positions, current_market_data)

        # 2. Run active exit checks (if auto_exit enabled in config)
        from tools.exit_manager import run_exit_checks
        exit_actions = run_exit_checks(current_market_data, self.config)
        
        for action in exit_actions:
            if action["action"] == "close":
                trade_id = action["bet_id"]
                
                # IDEMPOTENCY/COOLDOWN: Skip if we already have an active exit attempt for this position
                if trade_id in self._pending_exits:
                    logger.debug("[MONITOR] Skip repeat exit attempt for %s (already pending)", trade_id)
                    continue

                # Execute the sell (paper or live)
                from tools.paper_trader import close_position
                
                self._pending_exits[trade_id] = time.time()
                res = close_position(
                    trade_id=trade_id,
                    exit_price=action["close_price"]
                )
                
                if res.get("success"):
                    logger.info("[EXIT] Executed %s: %s", action["exit_code"], action["reason"])
                    send_message(
                        (
                            f"ORACLE EXIT EXECUTED\n"
                            f"market={res.get('question', trade_id)[:90]}\n"
                            f"reason={action['reason']}\n"
                            f"pnl={float(res.get('pnl', 0)):+.4f} USDC"
                        ),
                        self.config
                    )
                else:
                    # If failed, remove from pending so we can retry next cycle
                    if trade_id in self._pending_exits:
                        del self._pending_exits[trade_id]
                    logger.warning("[EXIT] Close failed for %s: %s", trade_id, res.get("reason"))

    def run_scan_once(self):
        cycle_start = time.time()
        self._maybe_reset_daily_stats()

        # Settle any positions that have resolved on-chain before entering new ones.
        resolved = check_and_resolve_positions()
        for r in resolved:
            logger.info(
                "[RESOLVE] settled trade_id=%s pnl=%+.4f balance=%.4f",
                r.get("trade_id", "?"), float(r.get("pnl", 0)), float(r.get("wallet_balance", 0)),
            )
            send_message(
                (
                    "ORACLE position settled\n"
                    f"trade_id={r.get('trade_id','?')}\n"
                    f"pnl={float(r.get('pnl',0)):+.4f} USDC\n"
                    f"balance={float(r.get('wallet_balance',0)):.4f} USDC"
                ),
                self.config,
            )

        # ALWAYS monitor open positions for exits every cycle (20-90s)
        self._monitor_open_positions()

        # Only perform heavy full market scan every 15 minutes
        now = time.time()
        if now - self._last_full_scan < (self.scan_interval_minutes * 60):
            # Skip full scan unless 15m passed
            return

        self._last_full_scan = now
        logger.info("[SCAN] Starting full market scan cycle.")

        mode = get_current_mode()
        markets = fetch_markets(self.config)
        if not markets:
            logger.warning("No markets fetched this cycle.")
            send_message("ORACLE: no markets fetched this cycle.", self.config)
            return

        placed = 0
        skip_reasons = init_cycle_skip_reasons()

        for market in markets:
            self._update_market_history(market)
            market_id = str(market.get("condition_id") or market.get("id") or "")

            features = extract_market_features(
                market=market,
                price_history=self._price_history.get(market_id, []),
                volume_history=self._volume_history.get(market_id, []),
            )

            model_out = compute_model_probability(features, self.model_weights)
            model_probability = float(model_out["model_probability"])

            posterior_out = bayesian_update(
                model_probability=model_probability,
                market_price_probability=float(features["market_probability"]),
                bayes_strength=self.bayes_strength,
            )
            posterior_probability = float(posterior_out["posterior_probability"])

            edge_out = calculate_edge(
                posterior_probability=posterior_probability,
                market_probability=float(features["market_probability"]),
                min_net_edge=float(tc.MIN_NET_EDGE),
                market_id=market_id,
            )

            if not edge_out["has_edge"]:
                increment_cycle_skip_reason(skip_reasons, "EDGE_FAIL")
                continue

            allowed, reason = self._risk_allows_trade()
            if not allowed:
                increment_cycle_skip_reason(skip_reasons, "RISK_FAIL")
                logger.info("Risk filter rejected trade: %s", reason)
                continue

            side = "YES" if edge_out["edge"] > 0 else "NO"
            market["_computed_edge"] = edge_out["edge"]

            result = execute_trade(
                market=market,
                side=side,
                edge=edge_out["edge"],
                config=self.config,
            )
            if result.get("success"):
                placed += 1
                increment_cycle_skip_reason(skip_reasons, "TRADE")
                logger.info(
                    "TRADE placed | mode=%s market=%s side=%s edge=%.4f posterior=%.4f",
                    mode,
                    market.get("question", "")[:80],
                    side,
                    edge_out["edge"],
                    posterior_probability,
                )
                send_message(
                    (
                        "ORACLE trade placed\n"
                        f"mode={mode} side={side}\n"
                        f"market={market.get('question', '')[:90]}\n"
                        f"edge={edge_out['edge']:.4f} posterior={posterior_probability:.4f}"
                    ),
                    self.config,
                )

        convergence_raw = fetch_convergence_markets(
            min_price=0.75,
            max_price=0.97,
            max_hours=168,
            min_liquidity=200,
        )
        from core.convergence_strategy import find_convergence_trades_with_diagnostics
        convergence_candidates, conv_diag = find_convergence_trades_with_diagnostics(convergence_raw)
        logger.info("[CONVERGENCE] filtered=%d candidates=%d diags=%s", len(convergence_raw), len(convergence_candidates), conv_diag)
        for c in convergence_candidates[:5]:
            logger.info(
                "[CONVERGENCE] %s price=%.3f hours=%s net_edge=%+.3f",
                c["question"][:50], c["price"], c["hours_left"], c["net_edge"],
            )
        if not convergence_candidates:
            logger.info("[CONVERGENCE] no candidates this cycle")

        # Paper execute best eligible convergence candidate (one per cycle max).
        if convergence_candidates and mode == "paper":
            allowed, reason = self._risk_allows_trade()
            if not allowed:
                increment_cycle_skip_reason(skip_reasons, "RISK_FAIL")
                logger.info("CONVERGENCE SKIP | reason=%s", reason)
            else:
                # Build dedup set from all open, pending, and manual_cleanup positions.
                _conv_wallet = load_wallet()
                open_market_ids = {
                    str(p.get("market_id", ""))
                    for p in (
                        _conv_wallet.get("open_positions", [])
                        + _conv_wallet.get("pending_limit_orders", [])
                        + _conv_wallet.get("manual_cleanup_needed", [])
                    )
                    if p.get("market_id")
                }

                for candidate in convergence_candidates:
                    cand_market_id = str(candidate.get("market_id") or "")

                    if cand_market_id in open_market_ids:
                        logger.info(
                            "[CONVERGENCE] SKIP | reason=ALREADY_OPEN market=%s",
                            candidate["question"][:60],
                        )
                        increment_cycle_skip_reason(skip_reasons, "RISK_FAIL")
                        continue

                    logger.info(
                        "[CONVERGENCE] EXECUTE | market=%s net_edge=%+.3f",
                        candidate["question"][:60],
                        float(candidate["net_edge"]),
                    )
                    convergence_market = dict(candidate.get("market", {}))
                    convergence_market["condition_id"] = str(convergence_market.get("condition_id") or cand_market_id)
                    convergence_market["id"] = str(convergence_market.get("id") or cand_market_id)
                    convergence_market["question"] = str(candidate.get("question", convergence_market.get("question", "")))
                    convergence_market["slug"] = str(candidate.get("slug") or convergence_market.get("slug") or "")
                    convergence_market["event_slug"] = str(candidate.get("event_slug") or convergence_market.get("event_slug") or "")
                    convergence_market["_strategy"] = "CONVERGENCE"
                    convergence_result = place_limit_order(
                        market=convergence_market,
                        side="YES",
                        limit_price=float(candidate["price"]),
                        edge=float(candidate["net_edge"]),
                    )
                    if convergence_result.get("success"):
                        placed += 1
                        increment_cycle_skip_reason(skip_reasons, "TRADE")
                        logger.info(
                            "CONVERGENCE TRADE placed | market=%s price=%.4f net_edge=%.4f",
                            candidate["question"],
                            float(candidate["price"]),
                            float(candidate["net_edge"]),
                        )
                        send_message(
                            (
                                "ORACLE convergence trade placed\n"
                                f"market={candidate['question'][:90]}\n"
                                f"side=YES price={float(candidate['price']):.4f} net_edge={float(candidate['net_edge']):.4f}"
                            ),
                            self.config,
                        )
                        break  # one convergence trade per cycle max
                    else:
                        increment_cycle_skip_reason(skip_reasons, "RISK_FAIL")
                        logger.info(
                            "CONVERGENCE SKIP | market=%s reason=%s",
                            candidate["question"],
                            convergence_result.get("reason", "UNKNOWN"),
                        )

        cycle_wallet = load_wallet()
        cycle_runtime = time.time() - cycle_start
        log_cycle_summary(
            skip_reasons=skip_reasons,
            total=len(markets),
            wallet=float(cycle_wallet.get("USDC", 0.0)),
        )
        logger.info(
            "Cycle runtime | seconds=%.2f open_positions=%d",
            cycle_runtime,
            len(cycle_wallet.get("open_positions", [])),
        )
        send_message(
            (
                "ORACLE cycle complete\n"
                f"mode={mode} markets={len(markets)}\n"
                f"TRADE={skip_reasons['TRADE']} EDGE_FAIL={skip_reasons['EDGE_FAIL']} "
                f"LIQ_FAIL={skip_reasons['LIQ_FAIL']} RISK_FAIL={skip_reasons['RISK_FAIL']}"
            ),
            self.config,
        )

        # Research mode: log hypothetical exits without executing any close orders.
        log_shadow_exits(
            open_positions=cycle_wallet.get("open_positions", []),
            current_markets=markets,
        )

    def _command_listener_loop(self):
        """Background daemon thread: polls Telegram for slash commands and responds."""
        logger.info("[TELEGRAM] command listener started")
        last_update_id: int | None = None
        while True:
            try:
                commands, last_update_id = poll_commands(
                    self.config, last_update_id=last_update_id, timeout=0
                )
                for cmd in commands:
                    command = cmd.get("command", "")
                    if command in ("/help", "/start"):
                        send_message(command_help_text(), self.config)
                    elif command == "/status":
                        mode = get_current_mode()
                        wallet = load_wallet()
                        daily = load_daily_stats()
                        send_message(
                            f"<b>ORACLE STATUS</b>\n"
                            f"mode={mode}\n"
                            f"balance=${wallet.get('USDC', 0.0):.4f} USDC\n"
                            f"open_positions={len(wallet.get('open_positions', []))}\n"
                            f"trades_today={daily.get('trades_taken', 0)}",
                            self.config,
                        )
                    elif command == "/balance":
                        wallet = load_wallet()
                        send_message(
                            f"<b>BALANCE</b>\n"
                            f"USDC=${wallet.get('USDC', 0.0):.4f}\n"
                            f"starting=${wallet.get('starting_balance', 0.0):.4f}\n"
                            f"peak=${wallet.get('peak_balance', 0.0):.4f}\n"
                            f"max_drawdown={wallet.get('max_drawdown', 0.0)*100:.2f}%",
                            self.config,
                        )
                    elif command == "/cleanup":
                        items = get_manual_cleanup_needed()
                        if not items:
                            send_message(
                                "<b>MANUAL CLEANUP</b>\nNo markets currently require manual cleanup.",
                                self.config,
                            )
                        else:
                            lines = [
                                "<b>MANUAL CLEANUP NEEDED</b>",
                                f"Count: {len(items)}",
                            ]
                            for idx, item in enumerate(items[:15], start=1):
                                question = str(item.get("question", ""))[:80]
                                clob_url = str(item.get("clob_url", ""))
                                trade_id = str(item.get("trade_id", ""))
                                lines.append(
                                    f"{idx}. {escape_html(question)}\n"
                                    f"trade_id={trade_id}\n"
                                    f"{escape_html(clob_url)}"
                                )
                            if len(items) > 15:
                                lines.append(f"...and {len(items) - 15} more")
                            send_message("\n".join(lines), self.config)
                    elif command == "/costs":
                        send_message(
                            "<b>COSTS</b>\nGemini cost tracking: see logs/oracle.log for API usage.",
                            self.config,
                        )
                    else:
                        # Always reply so Telegram users are never left without feedback.
                        send_message(
                            (
                                f"Unknown command: <code>{command or '/?'}</code>\n"
                                "Use /help to see supported commands."
                            ),
                            self.config,
                        )
            except Exception as exc:
                logger.warning("[TELEGRAM] command listener error: %s", exc)
            time.sleep(5)

    def run_forever(self):
        logger.info("Starting deterministic ORACLE main loop.")
        
        # Log effective exit monitoring config
        betting_cfg = self.config.get("betting", {})
        auto_exit = betting_cfg.get("auto_exit", False)
        threshold = betting_cfg.get("exit_score_threshold", 70)
        logger.info("[CONFIG] Exit Monitor: auto_exit=%s threshold=%d", auto_exit, threshold)

        listener_thread = threading.Thread(
            target=self._command_listener_loop, daemon=True, name="telegram-cmd-listener"
        )
        listener_thread.start()

        send_message(
            (
                "ORACLE deterministic loop online\n"
                f"scan_interval_minutes={self.scan_interval_minutes}\n"
                f"mode={get_current_mode()}"
            ),
            self.config,
        )
        while True:
            try:
                logger.info("Starting scan cycle.")
                self.run_scan_once()
            except Exception as exc:
                logger.exception("Unhandled error during scan loop: %s", exc)
                send_message(f"ORACLE scan error: {exc}", self.config)
            wallet = load_wallet()
            interval_seconds = self._get_adaptive_interval(wallet)
            logger.info(
                "Next scan in %.0fs (open_positions=%d)",
                interval_seconds,
                len(wallet.get("open_positions", [])),
            )
            time.sleep(interval_seconds)


def _send_daily_report_telegram(report: dict):
    msg = (
        "ORACLE Daily Report\n"
        f"Date: {report.get('date')}\n"
        f"balance_start_of_day: ${report.get('balance_start_of_day', 0.0):.2f}\n"
        f"balance_end_of_day: ${report.get('balance_end_of_day', 0.0):.2f}\n"
        f"PnL: ${report.get('PnL', 0.0):+.2f}\n"
        f"trades_taken: {report.get('trades_taken', 0)}\n"
        f"win_rate: {report.get('win_rate', 0.0) * 100:.1f}%\n"
        f"max_drawdown: {report.get('max_drawdown', 0.0) * 100:.2f}%"
    )
    send_message(msg)


def setup_logging(config: dict):
    level = getattr(logging, str(config.get("logging", {}).get("level", "INFO")).upper(), logging.INFO)
    log_file = ROOT / str(config.get("logging", {}).get("file", "logs/oracle.log"))
    log_file.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    if getattr(root_logger, "_oracle_deterministic_logging", False):
        root_logger.setLevel(level)
        return

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
    root_logger.setLevel(level)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root_logger.addHandler(console)

    rotating = logging.handlers.RotatingFileHandler(log_file, maxBytes=10_485_760, backupCount=5, encoding="utf-8")
    rotating.setFormatter(fmt)
    root_logger.addHandler(rotating)

    root_logger._oracle_deterministic_logging = True


def _acquire_pid_lock() -> Path:
    """Write a PID file and exit immediately if another instance is already running."""
    logs_dir = ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    pid_file = logs_dir / "oracle.pid"

    if pid_file.exists():
        try:
            existing_pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            existing_pid = None

        if existing_pid is not None:
            # On Windows, os.kill with signal 0 raises OSError if the PID is dead.
            try:
                os.kill(existing_pid, 0)
                # Process is alive — refuse to start.
                print(
                    f"[ORACLE] ERROR: another instance is already running (PID {existing_pid}). "
                    f"Kill it first or delete {pid_file}.",
                    file=sys.stderr,
                )
                sys.exit(1)
            except OSError:
                # Stale PID file — previous process is dead, safe to overwrite.
                pass

    pid_file.write_text(str(os.getpid()))
    atexit.register(lambda: pid_file.unlink(missing_ok=True))
    return pid_file


def main():
    _acquire_pid_lock()
    config = load_config()
    setup_logging(config)
    logger.info("PID lock acquired (PID %d)", os.getpid())
    oracle = DeterministicOracle(config)
    try:
        oracle.run_forever()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received; shutting down ORACLE gracefully.")
        send_message("ORACLE stopped: manual interrupt (Ctrl+C).", config)


if __name__ == "__main__":
    main()
