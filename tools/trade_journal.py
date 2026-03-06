"""Persistent trade journal helpers for deterministic observability."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
TRADE_JOURNAL_PATH = ROOT / "data" / "trade_journal.json"


def _read_journal() -> list[dict]:
    if not TRADE_JOURNAL_PATH.exists():
        return []
    try:
        with open(TRADE_JOURNAL_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("Failed to read trade journal %s: %s", TRADE_JOURNAL_PATH, exc)
        return []


def _safe_append(entry: dict):
    records = _read_journal()
    records.append(entry)

    TRADE_JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = TRADE_JOURNAL_PATH.with_suffix(".json.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2)
        temp_path.replace(TRADE_JOURNAL_PATH)
    except Exception as exc:
        logger.warning("Failed to persist trade journal entry: %s", exc)


def record_trade_open(
    trade_id: str,
    market_id: str,
    market_question: str,
    side: str,
    entry_price: float,
    position_size: float,
    edge_at_entry: float,
    opened_at: str | None = None,
):
    timestamp = opened_at or datetime.now().isoformat()
    entry = {
        "timestamp": timestamp,
        "trade_id": trade_id,
        "event": "OPEN",
        "market_id": market_id,
        "market_question": market_question,
        "side": side,
        "entry_price": round(float(entry_price), 6),
        "exit_price": None,
        "position_size": round(float(position_size), 6),
        "edge_at_entry": round(float(edge_at_entry), 6),
        "pnl": None,
        "holding_time_seconds": 0.0,
    }
    _safe_append(entry)


def record_trade_close(
    trade_id: str,
    market_id: str,
    market_question: str,
    side: str,
    entry_price: float,
    exit_price: float,
    position_size: float,
    edge_at_entry: float,
    pnl: float,
    holding_time_seconds: float,
    closed_at: str | None = None,
):
    timestamp = closed_at or datetime.now().isoformat()
    entry = {
        "timestamp": timestamp,
        "trade_id": trade_id,
        "event": "CLOSE",
        "market_id": market_id,
        "market_question": market_question,
        "side": side,
        "entry_price": round(float(entry_price), 6),
        "exit_price": round(float(exit_price), 6),
        "position_size": round(float(position_size), 6),
        "edge_at_entry": round(float(edge_at_entry), 6),
        "pnl": round(float(pnl), 6),
        "holding_time_seconds": round(max(0.0, float(holding_time_seconds)), 3),
    }
    _safe_append(entry)
