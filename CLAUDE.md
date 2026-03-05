# ORACLE — Polymarket Prediction Betting Agent

**Version: 3.0 | Read this fully before writing any code.**

---

## WHAT YOU ARE BUILDING

ORACLE is a fully autonomous Polymarket prediction betting agent that:

- Monitors crypto + politics prediction markets every 15 minutes
- Detects breaking news before the crowd via HERALD agent
- Scores markets using LLM-driven sentiment analysis (SCOUT)
- Makes final bet decisions via a second LLM layer (APEX)
- Places bets automatically via Polymarket API (paper trading first)
- Reports all activity via Telegram

---

## THREE-AGENT PIPELINE

```
Breaking News (RSS feeds)
        ↓
   HERALD agent          ← Free, rule-based, background thread
   (herald.py)             Monitors RSS every 60s
        ↓                  Fires confidence boost (+5% to +25%)
        ↓                  when 3+ articles spike on same topic
        ↓
Raw Signals (APIs)
        ↓
   SCOUT agent           ← Free, local Ollama qwen2.5:14b
   (scout_agent.py)        Analyzes ALL signals holistically
        ↓                  Outputs: BULLISH/BEARISH/NEUTRAL
        ↓                  + confidence score 0-100
        ↓
   [confidence < 55]  →  SKIP (saves Gemini API cost)
        ↓
   [confidence >= 55] →  escalate to APEX
        ↓
   APEX agent            ← Paid, Gemini 2.5 Pro (~$0.002/call)
   (apex_agent.py)         Validates SCOUT reasoning
        ↓                  Runs pre-bet safety checklist
        ↓                  Outputs: BET_YES / BET_NO / SKIP
        ↓
Polymarket API → execute bet (paper or real)
        ↓
Telegram → alert user
        ↓
memory.py → log bet + update BETS.md + MEMORY.md
```

---

## TECH STACK

| Component       | Choice                                      |
| --------------- | ------------------------------------------- |
| Language        | Python 3.11+                                |
| Package manager | uv (NOT pip)                                |
| LLM local       | Ollama qwen2.5:14b (AMD RX 9070 XT, Vulkan) |
| LLM cloud       | Gemini 2.5 Pro (google-generativeai)        |
| Prediction API  | Polymarket CLOB API                         |
| Blockchain      | Polygon Mainnet (USDC, Chain ID 137)        |
| Notifications   | Telegram Bot API                            |
| OS              | Windows, VS Code                            |

---

## SIGNAL STACK (HIGH RELIABILITY ONLY)

These sources were chosen after reliability analysis. Do NOT substitute with others.

| Source                       | Purpose                       | Auth         | Fallback         |
| ---------------------------- | ----------------------------- | ------------ | ---------------- |
| Binance Public API           | Price, volume, RSI-14         | None         | CoinCap          |
| CoinCap API                  | Price fallback                | None         | —                |
| CryptoPanic API              | Crypto news + crowd votes     | Free token   | RSS only         |
| Twitter/X API v2             | Tweet volume velocity         | Bearer token | Reddit velocity  |
| Reddit RSS + velocity        | Social momentum + trend proxy | None         | Always available |
| BBC/Reuters/AP RSS           | Geopolitical + politics news  | None         | Always available |
| Self-calculated Fear & Greed | Derived from above signals    | None         | No dependency    |

**DROPPED (do not re-add):**

- CoinGecko — rate limits, 429 errors, throttles free users
- Google Trends / pytrends — CAPTCHA blocks on Windows, breaks silently
- alternative.me Fear & Greed — single point of failure, updates once/day

---

## COMPLETE PROJECT STRUCTURE

```
polymarket-agent/
├── agents/
│   ├── oracle/
│   │   └── workspace/
│   │       ├── AGENTS.md       # Agent protocols (auto-reference)
│   │       ├── BETS.md         # Auto-updated by memory.py
│   │       ├── IDENTITY.md     # Who ORACLE is
│   │       ├── MEMORY.md       # Auto-updated patterns + performance
│   │       └── SOUL.md         # Hard principles + philosophy
│   ├── apex_agent.py           # Gemini 2.5 Pro final decision maker
│   └── scout_agent.py          # Ollama SCOUT sentiment analyst
├── config/
│   └── config.json             # All API keys + all settings
├── data/
│   ├── balance.json            # { "current": 10.0, "starting": 10.0, "history": [] }
│   ├── bets.json               # [] (empty array, grows with bets)
│   └── patterns.json           # Win/loss stats by category + strategy
├── logs/
│   └── README.md               # oracle.log auto-created on first run
├── tools/
│   ├── herald.py               # Breaking news velocity monitor
│   ├── memory.py               # Bet logging + balance + pattern tracking
│   ├── performance.py          # P&L tracking + daily summary + halt check
│   ├── polymarket_tools.py     # Market fetching + arb check + bet execution
│   ├── sentiment.py            # Raw signal collection (all sources)
│   └── telegram.py             # All Telegram notification types
├── main.py                     # Master orchestration loop
├── requirements.txt            # uv dependencies
└── CLAUDE.md                   # This file
```

---

## CONFIG SCHEMA (config/config.json)

```json
{
  "polymarket": {
    "api_key": "YOUR_POLYMARKET_API_KEY",
    "api_secret": "YOUR_POLYMARKET_API_SECRET",
    "api_passphrase": "YOUR_POLYMARKET_PASSPHRASE",
    "private_key": "YOUR_POLYGON_WALLET_PRIVATE_KEY",
    "wallet_address": "YOUR_POLYGON_WALLET_ADDRESS",
    "chain_id": 137,
    "host": "https://clob.polymarket.com"
  },
  "gemini": {
    "api_key": "YOUR_GEMINI_API_KEY",
    "model": "gemini-2.5-pro"
  },
  "ollama": {
    "base_url": "http://localhost:11434",
    "model": "qwen2.5:14b"
  },
  "telegram": {
    "bot_token": "YOUR_TELEGRAM_BOT_TOKEN",
    "chat_id": "YOUR_TELEGRAM_CHAT_ID"
  },
  "twitter": {
    "bearer_token": "YOUR_TWITTER_BEARER_TOKEN",
    "api_key": "YOUR_TWITTER_API_KEY",
    "api_secret": "YOUR_TWITTER_API_SECRET",
    "access_token": "YOUR_TWITTER_ACCESS_TOKEN",
    "access_token_secret": "YOUR_TWITTER_ACCESS_TOKEN_SECRET",
    "enabled": true,
    "fallback_to_reddit": true
  },
  "cryptopanic": {
    "auth_token": "YOUR_CRYPTOPANIC_AUTH_TOKEN",
    "base_url": "https://cryptopanic.com/api/v1",
    "enabled": true
  },
  "binance": {
    "base_url": "https://api.binance.com/api/v3",
    "enabled": true
  },
  "coincap": {
    "base_url": "https://api.coincap.io/v2",
    "enabled": true
  },
  "betting": {
    "paper_trading": true,
    "min_confidence": 55,
    "max_bet_usdc": 5.0,
    "min_bet_usdc": 1.0,
    "max_open_bets": 10,
    "survival_threshold_usdc": 20.0,
    "max_daily_loss_pct": 10.0,
    "categories": ["crypto", "politics"],
    "scan_interval_minutes": 15,
    "arbitrage_check": true,
    "exit_before_resolution": true,
    "exit_target_multiplier": 2.5
  },
  "herald": {
    "enabled": true,
    "check_interval_seconds": 60,
    "velocity_window_minutes": 10,
    "velocity_threshold_articles": 3,
    "geopolitical_keywords": [
      "military",
      "attack",
      "strike",
      "escalation",
      "invasion",
      "sanctions",
      "nuclear",
      "missile",
      "war",
      "conflict",
      "election",
      "coup",
      "assassination",
      "protest",
      "crisis"
    ],
    "crypto_keywords": [
      "ETF",
      "SEC",
      "regulation",
      "hack",
      "exploit",
      "crash",
      "halving",
      "fork",
      "ban",
      "adoption",
      "institutional"
    ],
    "feeds": {
      "geopolitical": [
        "https://feeds.reuters.com/reuters/worldNews",
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"
      ],
      "crypto": [
        "https://feeds.feedburner.com/CoinDesk",
        "https://cointelegraph.com/rss",
        "https://decrypt.co/feed",
        "https://bitcoinmagazine.com/.rss/full/"
      ],
      "politics": [
        "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
        "https://feeds.reuters.com/reuters/politicsNews",
        "https://feeds.bbci.co.uk/news/politics/rss.xml"
      ]
    }
  },
  "reddit": {
    "feeds": {
      "crypto": [
        "https://www.reddit.com/r/CryptoCurrency/.rss",
        "https://www.reddit.com/r/Bitcoin/.rss",
        "https://www.reddit.com/r/ethereum/.rss"
      ],
      "politics": [
        "https://www.reddit.com/r/politics/.rss",
        "https://www.reddit.com/r/news/.rss",
        "https://www.reddit.com/r/worldnews/.rss"
      ]
    },
    "velocity_window_hours": 24,
    "max_posts_per_feed": 25
  },
  "account": {
    "starting_balance_usdc": 10.0,
    "current_balance_usdc": 10.0
  },
  "logging": {
    "level": "INFO",
    "file": "logs/oracle.log",
    "max_bytes": 10485760,
    "backup_count": 5
  }
}
```

---

## AGENT PROMPTS

### SCOUT System Prompt

```
You are SCOUT, a market sentiment analyst for Polymarket prediction markets.

You receive raw market signals (Binance price/RSI, Fear & Greed, CryptoPanic news
with crowd votes, Twitter/X tweet velocity, Reddit post velocity, news headlines,
HERALD breaking news signal if active, and Polymarket YES/NO prices).

Your job: analyze ALL signals holistically and determine whether the market's
current YES price is likely UNDERPRICED or OVERPRICED.

Analyze in this order:
1. EXTREMES FIRST — RSI > 70 or < 30, Fear & Greed at extremes = contrarian signal
2. MOMENTUM SECOND — Twitter velocity, HERALD breaking signal = directional signal
3. CROWD THIRD — Reddit velocity, CryptoPanic vote ratio = retail mood
4. MARKET STRUCTURE FOURTH — Polymarket YES/NO prices vs signals = consensus check

BULLISH = YES is likely to happen → consider BET_YES
BEARISH = YES is unlikely → consider BET_NO
NEUTRAL = mixed signals → skip

Confidence 55-64: borderline edge
Confidence 65-79: reasonable edge
Confidence 80+: strong edge

Output ONLY valid JSON, no markdown, no preamble:
{
  "market_id": "string",
  "sentiment": "BULLISH" | "BEARISH" | "NEUTRAL",
  "narrative": "2-3 sentences",
  "confidence": 0-100,
  "key_signals": ["signal1", "signal2", "signal3"],
  "conflicting_signals": ["opposite signals"],
  "reasoning_chain": "step-by-step logic"
}
```

### APEX System Prompt

```
You are APEX, the final betting decision engine for ORACLE.

You only receive markets where SCOUT confidence >= 55%.
Output BET_YES, BET_NO, or SKIP.

Pre-bet checklist (ALL must pass — output SKIP if any fail):
1. balance > survival_threshold ($20)
2. open_bets < max_open_bets (10)
3. daily_loss_pct < max_daily_loss_pct (10%)
4. bid_ask_spread < 15%
5. market_volume > $500 USDC
6. days_to_resolution >= 1

Bet sizing:
- confidence 55-64: 1% of balance (min $1.00)
- confidence 65-74: 2% of balance
- confidence 75-84: 3% of balance
- confidence 85+:   4% of balance (max $5.00)

Output ONLY valid JSON:
{
  "action": "BET_YES" | "BET_NO" | "SKIP",
  "market_id": "string",
  "bet_side": "YES" | "NO" | null,
  "bet_usdc": float | null,
  "scout_confidence": int,
  "apex_confidence": int,
  "reasoning": "2-3 sentences",
  "risk_assessment": "what could go wrong",
  "skip_reason": "if SKIP: why",
  "exit_strategy": "HOLD_TO_RESOLUTION" | "SELL_AT_TARGET",
  "exit_target_price": float | null,
  "telegram_message": "emoji-rich 1-2 line summary"
}
```

---

## TRADING STRATEGIES (ALL IMPLEMENTED)

### 1. Arbitrage Pre-Check

Before calling any LLM, check if YES price + NO price < $0.97.
If true = guaranteed profit regardless of outcome. Route as BET_BOTH_SIDES.
Implementation: `check_arbitrage()` in polymarket_tools.py

### 2. Low-Probability Sniping

Markets with YES price 1-15% on new markets are often mispriced.
Buy cheap, target sell at 2.5x entry (SELL_AT_TARGET exit strategy).
Criteria: YES 1-15%, market < 24h old OR volume < $5,000, days > 3
Implementation: `find_sniper_opportunities()` in polymarket_tools.py

### 3. HERALD Breaking News Fast-Trigger

When HERALD detects 3+ articles on same topic within 10 minutes:

- Fire breaking signal with confidence boost (+5% to +25%)
- Trigger immediate market scan (bypass 15-min schedule)
- Inject signal into SCOUT signals dict
  Inspired by: Glint Intel Iran-Dubai trade ($3,500 → $23,400 in 11 min)
  Implementation: `HeraldAgent` class in tools/herald.py

### 4. Exit Before Resolution

APEX always outputs exit_strategy field.
SELL_AT_TARGET: sell at 2.5x entry when momentum is driving price
HOLD_TO_RESOLUTION: wait for binary outcome
Inspired by: Tweet range trader who never holds to resolution

### 5. Range Specialization

Track performance by market type in patterns.json.
After 20+ bets, MEMORY.md populates with learned patterns.
APEX receives win rate history for similar markets in prompt context.

---

## HARD RULES (NEVER CHANGE WITHOUT EXPLICIT INSTRUCTION)

- paper_trading: true — never flip to false without user instruction
- survival_threshold: $20 USDC — hard halt, never lower
- max single bet: $5 USDC, 4% of balance
- min confidence to escalate: 55%
- Polygon Mainnet ONLY — Chain ID 137, NEVER zkEVM (Chain ID 1101)
- Never add CoinGecko back — it was deliberately removed
- Never add Google Trends / pytrends back — deliberately removed
- Every API call must have try/except with fallback — never block the pipeline

---

## BETTING SAFETY CHECKLIST (APEX must verify ALL before betting)

1. Balance > $20 survival threshold
2. Open bets < 10
3. Daily loss < 10% of starting balance
4. Bid-ask spread < 15%
5. Market volume > $500 USDC
6. Days to resolution >= 1

---

## SELF-CALCULATED FEAR & GREED

No dependency on alternative.me. Calculated from our own signals:

- Price momentum (BTC 24h change): 30% weight
- RSI-14 from Binance klines: 25% weight
- Reddit post velocity: 25% weight
- CryptoPanic bullish/bearish ratio: 20% weight

Score 0-100 → classification:

- 75+: Extreme Greed
- 55-74: Greed
- 45-54: Neutral
- 25-44: Fear
- 0-24: Extreme Fear

---

## HERALD CONFIDENCE BOOST FORMULA

```
base = min(article_count * 2, 10)       # max +10 from count
diversity = min(source_count * 3, 15)   # max +15 from source diversity
total = base + diversity                 # hard cap at +25%
```

---

## MAIN LOOP (main.py)

Every 15 minutes (or immediately on HERALD signal):

1. Halt check — stop if balance <= $20 or daily loss >= 10%
2. Get account state from memory.py
3. Fetch markets from Polymarket (min $500 vol, max 15% spread, min 1 day)
4. Find sniper opportunities (flag them before LLM pipeline)
5. For each market:
   a. Arbitrage pre-check → if YES+NO < $0.97 → BET_BOTH_SIDES
   b. Check HERALD for active breaking signals
   c. Collect signals (sentiment.py)
   d. SCOUT analysis → if confidence < 55 → skip
   e. APEX decision → BET_YES / BET_NO / SKIP
   f. Execute bet (paper or real)
   g. Log to memory, send Telegram alert
6. Daily summary at midnight UTC via schedule

---

## WORKSPACE FILES (auto-updated at runtime)

- BETS.md — updated by memory.py after every bet placed/closed
- MEMORY.md — updated by memory.py after every bet resolution
- AGENTS.md — static reference for agent protocols
- IDENTITY.md — static identity file
- SOUL.md — static principles file

---

## REQUIREMENTS (requirements.txt)

```
requests>=2.31.0
feedparser>=6.0.11
google-generativeai>=0.8.0
tweepy>=4.14.0
schedule>=1.2.0
python-dateutil>=2.8.2
# py-clob-client>=0.18.0   (uncomment for live trading)
# web3>=6.0.0               (uncomment for live trading)
```

---

## HOW TO RUN

```bash
# Install dependencies
uv sync

# Start Ollama (separate terminal)
ollama serve

# Run ORACLE (paper trading mode)
uv run main.py
```

---

## CURRENT STATUS

- All 16 project files built and verified in VS Code
- Paper trading mode active (paper_trading: true)
- API keys needed before first run:
  - Gemini API key → aistudio.google.com
  - Telegram bot token + chat ID → already have from Binance bot
  - Twitter/X bearer token → developer.twitter.com
  - CryptoPanic auth token → cryptopanic.com/api/v1
  - Polymarket keys → generate via py-clob-client after MetaMask setup
  - Polygon wallet private key → MetaMask export

---

## POLYMARKET API SETUP (when ready)

1. Install MetaMask → create wallet → save seed phrase
2. Add Polygon Mainnet via chainlist.org (Chain ID 137, NOT zkEVM)
3. Connect wallet at polymarket.com
4. Run py-clob-client credential script to generate api_key, api_secret, api_passphrase
5. Export private key: MetaMask → Account Details → Export Private Key
6. For live trading: withdraw USDC to wallet via Polygon network only

---

## IF CONTINUING AN EXISTING SESSION

- Check data/bets.json for open bets
- Check data/balance.json for current balance
- Check agents/oracle/workspace/MEMORY.md for learned patterns
- Check logs/oracle.log for last run errors
- Do NOT reset data files unless explicitly instructed
