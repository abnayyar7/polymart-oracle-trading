"""Deterministic ORACLE main loop.

Pipeline:
market_data -> feature_engineering -> deterministic_probability_model ->
bayesian_probability_update -> edge_calculation -> risk_filter ->
execution_router -> memory + telemetry
"""

from __future__ import annotations

import logging
import logging.handlers
import time
from datetime import datetime
from pathlib import Path

from config.loader import load_config
from config import trading_config as tc
from core.bayesian_engine import bayesian_update
from core.edge_calculator import calculate_edge
from core.market_features import extract_market_features
from core.probability_model import compute_model_probability
from tools.execution_router import execute_trade, get_current_mode
from tools.paper_trader import (
    can_trade,
    generate_daily_report,
    load_daily_stats,
    load_wallet,
    reset_daily_stats,
)
from tools.polymarket_tools import fetch_markets
from tools.telegram import send_message

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

    def run_scan_once(self):
        cycle_start = time.time()
        self._maybe_reset_daily_stats()

        mode = get_current_mode()
        markets = fetch_markets(self.config)
        if not markets:
            logger.warning("No markets fetched this cycle.")
            send_message("ORACLE: no markets fetched this cycle.", self.config)
            return

        placed = 0
        skipped_edge = 0
        skipped_risk = 0

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
            )

            if not edge_out["has_edge"]:
                skipped_edge += 1
                continue

            allowed, reason = self._risk_allows_trade()
            if not allowed:
                skipped_risk += 1
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

        cycle_wallet = load_wallet()
        cycle_runtime = time.time() - cycle_start
        logger.info(
            (
                "Cycle complete | markets_scanned=%d trades_placed=%d trades_skipped_edge=%d "
                "trades_skipped_risk=%d wallet_balance=%.2f open_positions=%d cycle_runtime_seconds=%.2f"
            ),
            len(markets),
            placed,
            skipped_edge,
            skipped_risk,
            float(cycle_wallet.get("USDC", 0.0)),
            len(cycle_wallet.get("open_positions", [])),
            cycle_runtime,
        )
        send_message(
            (
                "ORACLE cycle complete\n"
                f"mode={mode} markets={len(markets)}\n"
                f"placed={placed} skipped_edge={skipped_edge} skipped_risk={skipped_risk}"
            ),
            self.config,
        )

    def run_forever(self):
        logger.info("Starting deterministic ORACLE main loop.")
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
                self.run_scan_once()
            except Exception as exc:
                logger.exception("Unhandled error during scan loop: %s", exc)
                send_message(f"ORACLE scan error: {exc}", self.config)
            time.sleep(max(60, self.scan_interval_minutes * 60))


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


def main():
    config = load_config()
    setup_logging(config)
    oracle = DeterministicOracle(config)
    oracle.run_forever()


if __name__ == "__main__":
    main()
