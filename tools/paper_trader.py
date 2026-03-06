"""Deterministic paper trading engine for ORACLE."""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from config import trading_config as tc
from tools.trade_journal import record_trade_close, record_trade_open

logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
LOGS_DIR = ROOT / "logs"
WALLET_PATH = LOGS_DIR / "paper_wallet.json"
DAILY_STATS_PATH = LOGS_DIR / "paper_stats_daily.json"
ALLTIME_STATS_PATH = LOGS_DIR / "paper_stats_alltime.json"
DAILY_REPORTS_PATH = ROOT / "data" / "daily_reports.json"


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now().isoformat()


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return {}


def _write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _wallet_default() -> dict:
    starting = float(tc.STARTING_BALANCE)
    return {
        "USDC": starting,
        "starting_balance": starting,
        "peak_balance": starting,
        "max_drawdown": 0.0,
        "open_positions": [],
        "trade_log": [],
    }


def _daily_default() -> dict:
    starting = float(tc.STARTING_BALANCE)
    return {
        "date": _today(),
        "balance_start_of_day": starting,
        "balance_end_of_day": starting,
        "trades_taken": 0,
        "wins": 0,
        "losses": 0,
        "pnl": 0.0,
        "fees_paid": 0.0,
        "max_drawdown": 0.0,
        "peak_balance_today": starting,
    }


def _alltime_default() -> dict:
    return {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl": 0.0,
        "total_fees": 0.0,
        "max_drawdown": 0.0,
        "peak_balance": float(tc.STARTING_BALANCE),
    }


def _merge(defaults: dict, existing: dict) -> dict:
    merged = deepcopy(defaults)
    for key, value in (existing or {}).items():
        merged[key] = value
    return merged


def load_wallet() -> dict:
    wallet = _merge(_wallet_default(), _read_json(WALLET_PATH))
    _write_json(WALLET_PATH, wallet)
    return wallet


def save_wallet(wallet: dict):
    _write_json(WALLET_PATH, wallet)


def load_daily_stats() -> dict:
    stats = _merge(_daily_default(), _read_json(DAILY_STATS_PATH))
    if stats.get("date") != _today():
        reset_daily_stats()
        stats = _merge(_daily_default(), _read_json(DAILY_STATS_PATH))
    _write_json(DAILY_STATS_PATH, stats)
    return stats


def save_daily_stats(stats: dict):
    _write_json(DAILY_STATS_PATH, stats)


def load_alltime_stats() -> dict:
    stats = _merge(_alltime_default(), _read_json(ALLTIME_STATS_PATH))
    _write_json(ALLTIME_STATS_PATH, stats)
    return stats


def save_alltime_stats(stats: dict):
    _write_json(ALLTIME_STATS_PATH, stats)


def _compute_drawdown(peak_balance: float, balance: float) -> float:
    if peak_balance <= 0:
        return 0.0
    return max(0.0, (peak_balance - balance) / peak_balance)


def can_trade(wallet: dict, daily_stats: dict, max_open_positions: int = tc.MAX_OPEN_POSITIONS) -> tuple[bool, str]:
    balance = float(wallet.get("USDC", 0.0))
    if balance <= float(tc.SURVIVAL_THRESHOLD_USDC):
        return False, "SURVIVAL_THRESHOLD_REACHED"

    trades_taken = int(daily_stats.get("trades_taken", 0))
    if trades_taken >= int(tc.MAX_TRADES_PER_DAY):
        return False, "MAX_DAILY_TRADES_REACHED"

    start_of_day = float(daily_stats.get("balance_start_of_day", balance))
    if start_of_day > 0:
        daily_loss_pct = max(0.0, (start_of_day - balance) / start_of_day)
        if daily_loss_pct >= float(tc.MAX_DAILY_LOSS_PCT):
            return False, "MAX_DAILY_LOSS_REACHED"

    if len(wallet.get("open_positions", [])) >= int(max_open_positions):
        return False, "MAX_OPEN_POSITIONS_REACHED"

    return True, "OK"


def calculate_position_size(entry_price: float, wallet_balance: float) -> float:
    price = max(0.01, float(entry_price))
    balance = max(0.0, float(wallet_balance))

    risk_per_trade = balance * float(tc.MAX_RISK_PER_TRADE_PCT)
    cost_buffer = 1.0 + float(tc.TAKER_FEE_RATE) + float(tc.SLIPPAGE_BUFFER_PCT)

    max_affordable = balance / cost_buffer
    proposed = min(risk_per_trade, max_affordable)
    proposed = min(proposed, float(tc.MAX_POSITION_USDC))
    proposed = max(proposed, float(tc.MIN_POSITION_USDC))

    total_cost = proposed * cost_buffer
    if total_cost > balance:
        proposed = max(0.0, balance / cost_buffer)

    if proposed < float(tc.MIN_POSITION_USDC):
        return 0.0

    # Keep deterministic and price-aware by ensuring at least one tiny share can be bought.
    shares = proposed / price
    if shares <= 0:
        return 0.0

    return round(proposed, 4)


def place_limit_order(
    market: dict,
    side: str,
    limit_price: float,
    edge: float,
    wallet: dict | None = None,
    daily_stats: dict | None = None,
) -> dict:
    """Place deterministic paper order and update wallet/stats in one flow."""
    active_wallet = wallet or load_wallet()
    active_daily = daily_stats or load_daily_stats()

    approved, reason = can_trade(active_wallet, active_daily)
    if not approved:
        return {"success": False, "reason": reason}

    balance = float(active_wallet.get("USDC", 0.0))
    size_usdc = calculate_position_size(limit_price, balance)
    if size_usdc <= 0:
        return {"success": False, "reason": "POSITION_SIZE_ZERO"}

    fee = round(size_usdc * float(tc.TAKER_FEE_RATE), 6)
    slip = round(size_usdc * float(tc.SLIPPAGE_BUFFER_PCT), 6)
    total_cost = round(size_usdc + fee + slip, 6)

    if total_cost > balance:
        return {"success": False, "reason": "INSUFFICIENT_BALANCE"}

    trade_id = f"paper_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    shares = round(size_usdc / max(0.01, float(limit_price)), 8)

    position = {
        "trade_id": trade_id,
        "market_id": str(market.get("condition_id") or market.get("id") or ""),
        "question": str(market.get("question", "")),
        "side": side,
        "entry_price": round(float(limit_price), 6),
        "size_usdc": size_usdc,
        "shares": shares,
        "edge": round(float(edge), 6),
        "fee_paid": fee,
        "slippage_paid": slip,
        "status": "open",
        "opened_at": _now_iso(),
    }

    active_wallet.setdefault("open_positions", []).append(position)
    active_wallet.setdefault("trade_log", []).append({
        "trade_id": trade_id,
        "event": "OPEN",
        "timestamp": _now_iso(),
        "size_usdc": size_usdc,
        "fee_paid": fee,
        "slippage_paid": slip,
        "edge": round(float(edge), 6),
    })

    active_wallet["USDC"] = round(balance - total_cost, 6)
    active_wallet["peak_balance"] = max(float(active_wallet.get("peak_balance", balance)), active_wallet["USDC"])
    active_wallet["max_drawdown"] = max(
        float(active_wallet.get("max_drawdown", 0.0)),
        _compute_drawdown(float(active_wallet.get("peak_balance", active_wallet["USDC"])), active_wallet["USDC"]),
    )

    active_daily["trades_taken"] = int(active_daily.get("trades_taken", 0)) + 1
    active_daily["fees_paid"] = round(float(active_daily.get("fees_paid", 0.0)) + fee + slip, 6)
    active_daily["balance_end_of_day"] = active_wallet["USDC"]
    active_daily["peak_balance_today"] = max(float(active_daily.get("peak_balance_today", active_wallet["USDC"])), active_wallet["USDC"])
    active_daily["max_drawdown"] = max(
        float(active_daily.get("max_drawdown", 0.0)),
        _compute_drawdown(float(active_daily.get("peak_balance_today", active_wallet["USDC"])), active_wallet["USDC"]),
    )

    save_wallet(active_wallet)
    save_daily_stats(active_daily)

    record_trade_open(
        trade_id=trade_id,
        market_id=position["market_id"],
        market_question=position["question"],
        side=position["side"],
        entry_price=position["entry_price"],
        position_size=position["size_usdc"],
        edge_at_entry=position["edge"],
        opened_at=position["opened_at"],
    )

    return {
        "success": True,
        "trade_id": trade_id,
        "size_usdc": size_usdc,
        "entry_price": round(float(limit_price), 6),
        "fee_paid": fee,
        "slippage_paid": slip,
        "wallet_balance": active_wallet["USDC"],
    }


def close_position(trade_id: str, exit_price: float, resolved_yes: bool | None = None) -> dict:
    wallet = load_wallet()
    daily = load_daily_stats()
    alltime = load_alltime_stats()

    positions = wallet.get("open_positions", [])
    idx = next((i for i, p in enumerate(positions) if p.get("trade_id") == trade_id), -1)
    if idx < 0:
        return {"success": False, "reason": "POSITION_NOT_FOUND"}

    pos = positions.pop(idx)
    entry = float(pos.get("entry_price", 0.5))
    shares = float(pos.get("shares", 0.0))
    side = str(pos.get("side", "YES")).upper()

    if resolved_yes is None:
        proceeds = shares * float(exit_price)
    else:
        payout = 1.0 if ((side == "YES" and resolved_yes) or (side == "NO" and not resolved_yes)) else 0.0
        proceeds = shares * payout

    pnl = round(proceeds - float(pos.get("size_usdc", 0.0)), 6)
    opened_at = str(pos.get("opened_at", "") or "")
    closed_at = _now_iso()

    holding_time_seconds = 0.0
    if opened_at:
        try:
            holding_time_seconds = (datetime.fromisoformat(closed_at) - datetime.fromisoformat(opened_at)).total_seconds()
        except Exception:
            holding_time_seconds = 0.0

    wallet["USDC"] = round(float(wallet.get("USDC", 0.0)) + proceeds, 6)
    wallet["peak_balance"] = max(float(wallet.get("peak_balance", wallet["USDC"])), wallet["USDC"])
    wallet["max_drawdown"] = max(
        float(wallet.get("max_drawdown", 0.0)),
        _compute_drawdown(float(wallet.get("peak_balance", wallet["USDC"])), wallet["USDC"]),
    )

    wallet.setdefault("trade_log", []).append({
        "trade_id": trade_id,
        "event": "CLOSE",
        "timestamp": closed_at,
        "exit_price": round(float(exit_price), 6),
        "pnl": pnl,
    })

    daily["pnl"] = round(float(daily.get("pnl", 0.0)) + pnl, 6)
    daily["balance_end_of_day"] = wallet["USDC"]
    if pnl >= 0:
        daily["wins"] = int(daily.get("wins", 0)) + 1
    else:
        daily["losses"] = int(daily.get("losses", 0)) + 1

    alltime["total_trades"] = int(alltime.get("total_trades", 0)) + 1
    alltime["total_pnl"] = round(float(alltime.get("total_pnl", 0.0)) + pnl, 6)
    if pnl >= 0:
        alltime["wins"] = int(alltime.get("wins", 0)) + 1
    else:
        alltime["losses"] = int(alltime.get("losses", 0)) + 1

    alltime["peak_balance"] = max(float(alltime.get("peak_balance", wallet["USDC"])), wallet["USDC"])
    alltime["max_drawdown"] = max(
        float(alltime.get("max_drawdown", 0.0)),
        _compute_drawdown(float(alltime.get("peak_balance", wallet["USDC"])), wallet["USDC"]),
    )

    save_wallet(wallet)
    save_daily_stats(daily)
    save_alltime_stats(alltime)

    record_trade_close(
        trade_id=trade_id,
        market_id=str(pos.get("market_id", "")),
        market_question=str(pos.get("question", "")),
        side=str(pos.get("side", "")),
        entry_price=entry,
        exit_price=float(exit_price),
        position_size=float(pos.get("size_usdc", 0.0)),
        edge_at_entry=float(pos.get("edge", 0.0)),
        pnl=pnl,
        holding_time_seconds=holding_time_seconds,
        closed_at=closed_at,
    )

    return {"success": True, "trade_id": trade_id, "pnl": pnl, "wallet_balance": wallet["USDC"], "entry_price": entry}


def reset_daily_stats():
    wallet = load_wallet()
    current = load_daily_stats()
    today = _today()

    if current.get("date") != today:
        archive_path = LOGS_DIR / f"paper_stats_{current.get('date')}.json"
        _write_json(archive_path, current)

    fresh = _daily_default()
    fresh["date"] = today
    fresh["balance_start_of_day"] = float(wallet.get("USDC", tc.STARTING_BALANCE))
    fresh["balance_end_of_day"] = float(wallet.get("USDC", tc.STARTING_BALANCE))
    fresh["peak_balance_today"] = float(wallet.get("USDC", tc.STARTING_BALANCE))
    save_daily_stats(fresh)


def generate_daily_report() -> dict:
    wallet = load_wallet()
    daily = load_daily_stats()
    alltime = load_alltime_stats()

    trades = int(daily.get("trades_taken", 0))
    wins = int(daily.get("wins", 0))
    win_rate = (wins / trades) if trades > 0 else 0.0

    report = {
        "date": daily.get("date", _today()),
        "balance_start_of_day": float(daily.get("balance_start_of_day", tc.STARTING_BALANCE)),
        "balance_end_of_day": float(wallet.get("USDC", tc.STARTING_BALANCE)),
        "PnL": round(float(daily.get("pnl", 0.0)), 6),
        "trades_taken": trades,
        "win_rate": round(win_rate, 6),
        "max_drawdown": round(float(daily.get("max_drawdown", 0.0)), 6),
        "alltime_trades": int(alltime.get("total_trades", 0)),
        "alltime_pnl": round(float(alltime.get("total_pnl", 0.0)), 6),
        "alltime_max_drawdown": round(float(alltime.get("max_drawdown", 0.0)), 6),
    }

    out_path = LOGS_DIR / f"paper_daily_report_{report['date']}.json"
    _write_json(out_path, report)

    # Persistent cross-run daily snapshot file for observability.
    snapshot = {
        "date": report["date"],
        "start_balance": report["balance_start_of_day"],
        "end_balance": report["balance_end_of_day"],
        "daily_pnl": report["PnL"],
        "trades_executed": report["trades_taken"],
        "win_rate": report["win_rate"],
        "max_drawdown": report["max_drawdown"],
    }

    daily_reports: list[dict]
    if DAILY_REPORTS_PATH.exists():
        try:
            with open(DAILY_REPORTS_PATH, encoding="utf-8") as f:
                raw = json.load(f)
            daily_reports = raw if isinstance(raw, list) else []
        except Exception as exc:
            logger.warning("Failed reading %s: %s", DAILY_REPORTS_PATH, exc)
            daily_reports = []
    else:
        daily_reports = []

    filtered = [d for d in daily_reports if str(d.get("date", "")) != str(snapshot["date"])]
    filtered.append(snapshot)
    DAILY_REPORTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DAILY_REPORTS_PATH, "w", encoding="utf-8") as f:
        json.dump(filtered, f, indent=2)

    return report
