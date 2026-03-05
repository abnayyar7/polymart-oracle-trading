"""
polymarket_tools.py — Polymarket CLOB API integration.

Responsibilities:
  - Fetch active markets (filtered by volume, spread, days to resolution)
  - Arbitrage pre-check (YES + NO < $0.97)
  - Low-probability sniper opportunity detection
  - Paper bet execution (logs without real API call)
  - Live bet execution (real CLOB API — guarded by paper_trading flag)

Chain: Polygon Mainnet ONLY (Chain ID 137). Never zkEVM (Chain ID 1101).
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config" / "config.json"

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "ORACLE/3.0", "Accept": "application/json"})

ARB_THRESHOLD = 0.97        # YES + NO < this = guaranteed arb
SNIPER_MAX_YES_PRICE = 0.15  # 1-15%
SNIPER_MAX_VOLUME = 5000.0   # < $5k volume
SNIPER_MIN_DAYS = 3          # > 3 days to resolution
SNIPER_MAX_MARKET_AGE_H = 24 # < 24h old


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Market fetching
# ---------------------------------------------------------------------------

def fetch_markets(config: dict, limit: int = 50) -> list[dict]:
    """
    Fetch active Polymarket markets filtered to viable trading candidates.
    Filters: volume > $500, spread < 15%, days to resolution >= 1.
    """
    dev = config.get("dev_flags", {})

    if dev.get("mock_market_data", False):
        logger.info("[DEV] mock_market_data=true — returning hardcoded test markets")
        return _get_demo_markets()

    if not dev.get("use_polymarket_api", True):
        logger.info("[DEV] use_polymarket_api=false — skipping Polymarket API, using demo markets")
        return _get_demo_markets()

    host = config.get("polymarket", {}).get("host", "https://clob.polymarket.com")
    betting_cfg = config.get("betting", {})
    categories = betting_cfg.get("categories", ["crypto", "politics"])

    markets = []
    try:
        r = _SESSION.get(
            f"{host}/markets",
            params={"active": "true", "limit": limit},
            timeout=15,
        )
        r.raise_for_status()
        raw = r.json()
        items = raw if isinstance(raw, list) else raw.get("data", [])
        for item in items:
            parsed = _parse_market(item)
            if parsed and _passes_filters(parsed, betting_cfg, categories):
                markets.append(parsed)
    except Exception as exc:
        logger.error("Failed to fetch markets: %s", exc)
        # Return demo markets for paper trading development
        markets = _get_demo_markets()

    logger.info("Fetched %d viable markets.", len(markets))
    return markets


def _parse_market(raw: dict) -> dict | None:
    try:
        # Extract YES/NO prices from tokens array
        tokens = raw.get("tokens", [])
        yes_price = no_price = 0.5
        for tok in tokens:
            outcome = tok.get("outcome", "").upper()
            price = float(tok.get("price", 0.5))
            if outcome == "YES":
                yes_price = price
            elif outcome == "NO":
                no_price = price

        # Volume in USDC
        volume = float(raw.get("volume", 0) or raw.get("volumeNum", 0) or 0)

        # Days to resolution
        end_date_iso = raw.get("endDateIso") or raw.get("end_date_iso") or ""
        days_to_resolution = _calc_days(end_date_iso)

        # Spread
        spread = abs(yes_price + no_price - 1.0)

        return {
            "condition_id": raw.get("conditionId", raw.get("condition_id", "")),
            "question": raw.get("question", ""),
            "category": _infer_category(raw.get("question", ""), raw.get("category", "")),
            "yes_price": round(yes_price, 4),
            "no_price": round(no_price, 4),
            "spread": round(spread, 4),
            "volume": round(volume, 2),
            "days_to_resolution": days_to_resolution,
            "end_date_iso": end_date_iso,
            "created_at": raw.get("createdAt", raw.get("created_at", "")),
            "active": raw.get("active", True),
            "raw": raw,
        }
    except Exception as exc:
        logger.debug("Market parse failed: %s | %s", exc, str(raw)[:100])
        return None


def _passes_filters(market: dict, betting_cfg: dict, categories: list[str]) -> bool:
    min_vol = 500.0
    max_spread = 0.15
    min_days = 1

    if market["volume"] < min_vol:
        return False
    if market["spread"] > max_spread:
        return False
    if market["days_to_resolution"] < min_days:
        return False
    if categories and market["category"] not in categories:
        return False
    return True


def _calc_days(end_date_iso: str) -> float:
    if not end_date_iso:
        return 0.0
    try:
        end = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = (end - now).total_seconds() / 86400
        return round(max(delta, 0.0), 2)
    except Exception:
        return 0.0


def _infer_category(question: str, raw_cat: str) -> str:
    q = question.lower()
    raw_cat_lower = raw_cat.lower()
    crypto_terms = ["bitcoin", "btc", "eth", "ethereum", "crypto", "defi", "nft", "coin", "token", "blockchain"]
    if any(t in q for t in crypto_terms) or "crypto" in raw_cat_lower:
        return "crypto"
    return "politics"


# ---------------------------------------------------------------------------
# Arbitrage check
# ---------------------------------------------------------------------------

def check_arbitrage(market: dict) -> bool:
    """True if YES + NO < $0.97 — guaranteed profit opportunity."""
    return (market["yes_price"] + market["no_price"]) < ARB_THRESHOLD


# ---------------------------------------------------------------------------
# Sniper opportunity detection
# ---------------------------------------------------------------------------

def find_sniper_opportunities(markets: list[dict]) -> list[dict]:
    """
    Returns markets matching low-probability sniper criteria:
      - YES price 1-15%
      - volume < $5,000
      - days_to_resolution > 3
      - market age < 24h (if created_at available)
    """
    snipers = []
    now = datetime.now(timezone.utc)
    for m in markets:
        yes = m["yes_price"]
        if not (0.01 <= yes <= SNIPER_MAX_YES_PRICE):
            continue
        if m["volume"] >= SNIPER_MAX_VOLUME:
            continue
        if m["days_to_resolution"] <= SNIPER_MIN_DAYS:
            continue
        # Age check
        created_str = m.get("created_at", "")
        if created_str:
            try:
                created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                age_hours = (now - created).total_seconds() / 3600
                if age_hours > SNIPER_MAX_MARKET_AGE_H:
                    continue
            except Exception:
                pass
        snipers.append(m)
    return snipers


# ---------------------------------------------------------------------------
# Bet execution
# ---------------------------------------------------------------------------

def execute_bet(
    market: dict,
    side: str,
    bet_usdc: float,
    config: dict,
    exit_target_price: float | None = None,
) -> dict:
    """
    Execute a bet. If paper_trading=True, simulate. Otherwise, call real CLOB API.
    Returns a result dict with success, order_id, executed_price, etc.
    """
    paper = config.get("betting", {}).get("paper_trading", True)
    if paper:
        return _paper_bet(market, side, bet_usdc, exit_target_price)
    else:
        return _live_bet(market, side, bet_usdc, config, exit_target_price)


def _paper_bet(market: dict, side: str, bet_usdc: float, exit_target_price: float | None) -> dict:
    """Simulate bet placement — no real money movement."""
    price = market["yes_price"] if side == "YES" else market["no_price"]
    contracts = round(bet_usdc / price, 4) if price > 0 else 0
    logger.info(
        "[PAPER] BET_%s | %s | $%.2f | price=%.3f | contracts=%.2f",
        side, market["question"][:60], bet_usdc, price, contracts,
    )
    return {
        "success": True,
        "paper": True,
        "order_id": f"PAPER_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
        "market_id": market["condition_id"],
        "side": side,
        "bet_usdc": bet_usdc,
        "executed_price": price,
        "contracts": contracts,
        "exit_target_price": exit_target_price,
    }


def _live_bet(market: dict, side: str, bet_usdc: float, config: dict, exit_target_price: float | None) -> dict:
    """
    Real CLOB API bet execution.
    Requires py-clob-client and web3 (uncomment in requirements.txt).
    Chain ID must be 137 (Polygon Mainnet).
    """
    try:
        # Import guarded — only available when py-clob-client is installed
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType

        poly_cfg = config.get("polymarket", {})
        chain_id = poly_cfg.get("chain_id", 137)
        if chain_id != 137:
            raise ValueError(f"Wrong chain ID {chain_id}. Must be Polygon Mainnet (137).")

        client = ClobClient(
            host=poly_cfg["host"],
            chain_id=chain_id,
            private_key=poly_cfg["private_key"],
            creds=ApiCreds(
                api_key=poly_cfg["api_key"],
                api_secret=poly_cfg["api_secret"],
                api_passphrase=poly_cfg["api_passphrase"],
            ),
        )

        token_id = _get_token_id(market, side)
        price = market["yes_price"] if side == "YES" else market["no_price"]
        order_args = OrderArgs(token_id=token_id, price=price, size=bet_usdc)
        resp = client.create_and_post_order(order_args)

        logger.info("[LIVE] BET_%s | %s | $%.2f | order=%s", side, market["question"][:50], bet_usdc, resp)
        return {
            "success": True,
            "paper": False,
            "order_id": str(resp),
            "market_id": market["condition_id"],
            "side": side,
            "bet_usdc": bet_usdc,
            "executed_price": price,
            "exit_target_price": exit_target_price,
        }
    except ImportError:
        logger.error("py-clob-client not installed. Uncomment in requirements.txt for live trading.")
        return {"success": False, "error": "py-clob-client not installed"}
    except Exception as exc:
        logger.error("Live bet execution failed: %s", exc)
        return {"success": False, "error": str(exc)}


def _get_token_id(market: dict, side: str) -> str:
    tokens = market.get("raw", {}).get("tokens", [])
    for tok in tokens:
        if tok.get("outcome", "").upper() == side:
            return tok.get("token_id", "")
    return ""


# ---------------------------------------------------------------------------
# Demo markets (used when API is unavailable / unconfigured)
# ---------------------------------------------------------------------------

def _get_demo_markets() -> list[dict]:
    """Returns synthetic markets for paper trading development/testing."""
    now = datetime.now(timezone.utc)
    return [
        {
            "condition_id": "demo_btc_100k_2025",
            "question": "Will Bitcoin reach $100,000 by end of 2025?",
            "category": "crypto",
            "yes_price": 0.62,
            "no_price": 0.38,
            "spread": 0.0,
            "volume": 125000.0,
            "days_to_resolution": 45.0,
            "end_date_iso": (now + timedelta(days=45)).isoformat(),
            "created_at": now.isoformat(),
            "active": True,
            "raw": {},
        },
        {
            "condition_id": "demo_eth_5k_2025",
            "question": "Will Ethereum reach $5,000 before June 2025?",
            "category": "crypto",
            "yes_price": 0.31,
            "no_price": 0.69,
            "spread": 0.0,
            "volume": 73000.0,
            "days_to_resolution": 90.0,
            "end_date_iso": (now + timedelta(days=90)).isoformat(),
            "created_at": now.isoformat(),
            "active": True,
            "raw": {},
        },
        {
            "condition_id": "demo_us_election_dem",
            "question": "Will the Democratic candidate win the 2026 midterms?",
            "category": "politics",
            "yes_price": 0.44,
            "no_price": 0.56,
            "spread": 0.0,
            "volume": 210000.0,
            "days_to_resolution": 240.0,
            "end_date_iso": (now + timedelta(days=240)).isoformat(),
            "created_at": now.isoformat(),
            "active": True,
            "raw": {},
        },
    ]
