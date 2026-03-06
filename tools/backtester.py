"""Deterministic ORACLE backtester using the exact live decision pipeline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean, pstdev

from config import trading_config as tc
from core.bayesian_engine import bayesian_update
from core.edge_calculator import calculate_edge
from core.market_features import extract_market_features
from core.probability_model import compute_model_probability


def _load_config() -> dict:
    config_path = Path(__file__).resolve().parent.parent / "config" / "config.json"
    if not config_path.exists():
        return {}
    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def load_market_history(file_path: str) -> list[dict]:
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"History file not found: {file_path}")

    with open(p, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("markets"), list):
        return data["markets"]
    raise ValueError("Unsupported history format. Expected list or {'markets': [...]}.")


def _infer_outcome_yes(market: dict) -> bool | None:
    outcome = market.get("outcome")
    if isinstance(outcome, str):
        up = outcome.strip().upper()
        if up in ("YES", "Y", "TRUE", "1"):
            return True
        if up in ("NO", "N", "FALSE", "0"):
            return False

    if isinstance(market.get("resolved_yes"), bool):
        return bool(market["resolved_yes"])

    if isinstance(market.get("final_yes_price"), (int, float)):
        return float(market["final_yes_price"]) >= 0.5

    return None


def _sim_pnl(side: str, stake: float, entry_price: float, outcome_yes: bool) -> float:
    if entry_price <= 0:
        return -stake

    shares = stake / entry_price
    settles_yes = 1.0 if outcome_yes else 0.0

    if side == "YES":
        gross = shares * settles_yes
    else:
        gross = shares * (1.0 - settles_yes)

    fees = stake * float(tc.TAKER_FEE_RATE)
    return gross - stake - fees


def simulate_trades(
    history: list[dict],
    config: dict,
    starting_balance: float,
) -> dict:
    model_weights = config.get("model_weights", {})
    bayes_strength = float(config.get("betting", {}).get("bayes_strength", 0.35))

    balance = float(starting_balance)
    peak = balance
    max_drawdown = 0.0

    trade_logs: list[dict] = []
    edges: list[float] = []
    returns: list[float] = []

    for market in history:
        snapshots = market.get("snapshots") or []
        if len(snapshots) < 2:
            continue

        outcome_yes = _infer_outcome_yes(market)
        if outcome_yes is None:
            continue

        price_hist: list[float] = []
        volume_hist: list[float] = []

        for snap in snapshots[:-1]:
            yes_price = float(snap.get("yes_price", 0.5) or 0.5)
            no_price = float(snap.get("no_price", 1.0 - yes_price) or (1.0 - yes_price))
            volume = float(snap.get("volume", 0.0) or 0.0)
            spread = float(snap.get("spread", abs((yes_price + no_price) - 1.0)) or 0.0)

            pseudo_market = {
                "condition_id": market.get("market_id") or market.get("condition_id") or market.get("id") or "",
                "question": market.get("question", ""),
                "yes_price": yes_price,
                "no_price": no_price,
                "volume": volume,
                "liquidity": float(snap.get("liquidity", volume) or volume),
                "spread": spread,
                "days_to_resolution": float(snap.get("days_to_resolution", 0.0) or 0.0),
            }

            price_hist.append(yes_price)
            volume_hist.append(volume)

            features = extract_market_features(
                pseudo_market,
                price_history=price_hist,
                volume_history=volume_hist,
            )
            model_prob = compute_model_probability(features, model_weights)["model_probability"]
            posterior = bayesian_update(model_prob, features["market_probability"], bayes_strength)["posterior_probability"]
            edge_out = calculate_edge(
                posterior_probability=posterior,
                market_probability=features["market_probability"],
                min_net_edge=float(tc.MIN_NET_EDGE),
            )

            if not edge_out["has_edge"]:
                continue

            side = "YES" if edge_out["edge"] > 0 else "NO"
            stake = min(float(tc.MAX_POSITION_USDC), balance * float(tc.MAX_RISK_PER_TRADE_PCT))
            if stake < float(tc.MIN_POSITION_USDC):
                continue

            entry = yes_price if side == "YES" else no_price
            pnl = _sim_pnl(side=side, stake=stake, entry_price=entry, outcome_yes=outcome_yes)
            balance += pnl

            peak = max(peak, balance)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - balance) / peak)

            edges.append(float(edge_out["edge"]))
            returns.append(pnl / stake)
            trade_logs.append(
                {
                    "market_id": pseudo_market["condition_id"],
                    "question": pseudo_market["question"],
                    "side": side,
                    "entry_price": round(entry, 6),
                    "edge": round(float(edge_out["edge"]), 6),
                    "stake": round(stake, 6),
                    "pnl": round(pnl, 6),
                    "won": pnl > 0,
                }
            )
            break

    total_pnl = round(balance - float(starting_balance), 6)
    trades_count = len(trade_logs)
    wins = sum(1 for t in trade_logs if t["won"])
    win_rate = (wins / trades_count) if trades_count > 0 else 0.0
    avg_edge = mean(edges) if edges else 0.0

    if len(returns) < 2:
        sharpe = 0.0
    else:
        sigma = pstdev(returns)
        sharpe = 0.0 if sigma <= 1e-12 else mean(returns) / sigma

    return {
        "total_PnL": total_pnl,
        "win_rate": round(win_rate, 6),
        "average_edge": round(avg_edge, 6),
        "max_drawdown": round(max_drawdown, 6),
        "sharpe_ratio": round(float(sharpe), 6),
        "trades": trade_logs,
        "starting_balance": float(starting_balance),
        "ending_balance": round(balance, 6),
    }


def main():
    parser = argparse.ArgumentParser(description="Run deterministic ORACLE backtest")
    parser.add_argument("history", help="Path to historical snapshot JSON")
    parser.add_argument("--starting-balance", type=float, default=float(tc.STARTING_BALANCE))
    parser.add_argument("--output", default=None, help="Optional path for full JSON output")
    args = parser.parse_args()

    history = load_market_history(args.history)
    config = _load_config()
    result = simulate_trades(history=history, config=config, starting_balance=args.starting_balance)

    print("Backtest Report")
    print(f"total PnL: {result['total_PnL']:+.4f}")
    print(f"win rate: {result['win_rate'] * 100:.2f}%")
    print(f"average edge: {result['average_edge'] * 100:.3f}%")
    print(f"max drawdown: {result['max_drawdown'] * 100:.2f}%")
    print(f"sharpe ratio: {result['sharpe_ratio']:.4f}")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)


if __name__ == "__main__":
    main()
