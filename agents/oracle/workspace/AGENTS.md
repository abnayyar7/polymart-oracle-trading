# ORACLE — Agent Protocols

## Pipeline Flow

```
Breaking News (RSS)
      |
   HERALD (herald.py)
      |-- confidence boost: +5% to +25%
      |-- triggers immediate scan on 3+ articles
      v
Raw Signals (APIs)
      |
   SCOUT (scout_agent.py / Ollama qwen2.5:14b)
      |-- BULLISH / BEARISH / NEUTRAL
      |-- confidence 0-100
      |-- if confidence < 55: SKIP (no Gemini cost)
      v
   APEX (apex_agent.py / Gemini 2.5 Pro)
      |-- pre-bet safety checklist (6 checks)
      |-- BET_YES / BET_NO / SKIP
      |-- bet sizing by confidence band
      v
Polymarket API -> execute
      |
Telegram -> alert
      |
memory.py -> log
```

## HERALD Protocol

- Polls RSS feeds every 60 seconds
- Velocity window: 10 minutes
- Trigger threshold: 3+ articles on same topic
- Confidence boost formula:
  - base = min(article_count \* 2, 10)
  - diversity = min(source_count \* 3, 15)
  - total = base + diversity (hard cap +25%)

## SCOUT Protocol

- Receives: Binance price/RSI, Fear & Greed, CryptoPanic, StockTwits sentiment, Nitter/Telegram updates, Reddit velocity, HERALD signal, Polymarket prices
- Analysis order: EXTREMES → MOMENTUM → CROWD → MARKET STRUCTURE
- Output: JSON with sentiment, confidence, narrative, key_signals, conflicting_signals, reasoning_chain

## APEX Protocol

- Only receives markets where SCOUT confidence >= 55
- Pre-bet checklist (ALL must pass):
  1. balance > $20 survival threshold
  2. open_bets < 10
  3. daily_loss_pct < 10%
  4. bid_ask_spread < 15%
  5. market_volume > $500 USDC
  6. days_to_resolution >= 1
- Bet sizing:
  - 55-64%: 1% of balance (min $1.00)
  - 65-74%: 2% of balance
  - 75-84%: 3% of balance
  - 85%+: 4% of balance (max $5.00)
- Output: JSON with action, bet_usdc, exit_strategy, telegram_message

## Trading Strategies

1. **Arbitrage**: YES + NO < $0.97 → BET_BOTH_SIDES (guaranteed profit)
2. **Sniper**: YES 1-15%, new market, volume < $5k, days > 3 → target 2.5x
3. **HERALD Fast-Trigger**: Breaking news → immediate scan bypass
4. **Exit Before Resolution**: SELL_AT_TARGET at 2.5x entry
5. **Range Specialization**: Track patterns.json, learn from history
