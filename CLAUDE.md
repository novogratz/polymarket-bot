# Claude Code Guide

Use this file as the Claude Code entry point for the Polymarket bot.

## Safety

- Never reveal `.env` values, private keys, API secrets, or passphrases.
- Do not bypass `POLYMARKET_ENABLE_LIVE_TRADING=1`.
- Do not implement random or unfiltered live trades.
- Preserve the local ledger in `data/paper_state.json` unless the user explicitly asks to reset it.

## Project Map

- `polymarket_bot/main.py`: CLI commands and strategy loops.
- `polymarket_bot/smart_money.py`: autonomous smart-money copy-trading filters.
- `polymarket_bot/bitcoin.py`: optional BTC threshold edge model.
- `polymarket_bot/trading.py`: authenticated live BUY/SELL order placement and sizing.
- `polymarket_bot/dashboard.py`: local real-time HTML dashboard.
- `polymarket_bot/portfolio.py`: local ledger for paper and live positions.
- `tests/test_strategy.py`: strategy and order-building tests.

## Development Workflow

Run:

```bash
python3 -B -m unittest discover -s tests
```

Run the dashboard:

```bash
python3 -B -m polymarket_bot.main dashboard
```

Run the autonomous smart-money loop:

```bash
POLYMARKET_ENABLE_LIVE_TRADING=1 python3 -B -m polymarket_bot.main auto-loop
```

Run it faster:

```bash
POLYMARKET_ENABLE_LIVE_TRADING=1 POLYMARKET_AUTO_INTERVAL_SECONDS=30 python3 -B -m polymarket_bot.main auto-loop
```

Recommended live command:

```bash
POLYMARKET_ENABLE_LIVE_TRADING=1 \
  POLYMARKET_SYNC_LIVE_POSITIONS=0 \
  POLYMARKET_SMART_CATEGORIES=OVERALL,FINANCE,ECONOMICS,TECH,POLITICS,SPORTS,CULTURE,WEATHER \
  POLYMARKET_SMART_DISCOVERY_KEYWORDS='election,trump,senate,congress,fed,inflation,cpi,unemployment,gdp,weather,rain,snow,hurricane,temperature,box office,movie,earnings,stock,nasdaq' \
  POLYMARKET_SMART_ALLOW_CRYPTO=1 \
  POLYMARKET_SMART_CRYPTO_MIN_BUY_PRICE=0.70 \
  POLYMARKET_SMART_CRYPTO_MIN_HOURS_TO_CLOSE=0 \
  POLYMARKET_SMART_CRYPTO_MAX_HOURS_TO_CLOSE=48 \
  POLYMARKET_MAX_POSITION_USD=25 \
  POLYMARKET_SMART_MAX_TRADE_USD=25 \
  POLYMARKET_SMART_HIGH_CONVICTION_BALANCE_FRACTION=0.50 \
  POLYMARKET_SMART_MIN_CONSENSUS=2 \
  POLYMARKET_SMART_FALLBACK_CONSENSUS=2 \
  POLYMARKET_SMART_TRADE_LOOKBACK_MINUTES=30 \
  POLYMARKET_SMART_MAX_SIGNAL_AGE_MINUTES=5 \
  POLYMARKET_SMART_MIN_TRADE_USD=1 \
  POLYMARKET_SMART_MIN_COPIED_USDC=75 \
  POLYMARKET_SMART_MAX_CHASE_PREMIUM=0.25 \
  POLYMARKET_SMART_PRIORITY_CATEGORY_BONUS=8 \
  POLYMARKET_SMART_SPORTS_SCORE_PENALTY=12 \
  POLYMARKET_SMART_MAX_SPORTS_POSITIONS=3 \
  POLYMARKET_SMART_SOON_HOURS=168 \
  POLYMARKET_SMART_MAX_HOURS_TO_CLOSE=48 \
  POLYMARKET_SMART_LEADERBOARD_LIMIT=100 \
  POLYMARKET_SMART_MIN_HOURS_TO_CLOSE=0.01 \
  POLYMARKET_SMART_MAX_ENTRY_SLIPPAGE=0.25 \
  POLYMARKET_SMART_MIN_BUY_PRICE=0.01 \
  POLYMARKET_SMART_MAX_BUY_PRICE=0.99 \
  POLYMARKET_SMART_MAX_SPREAD=0.18 \
  POLYMARKET_AUTO_INTERVAL_SECONDS=10 \
  python3 -B -m polymarket_bot.main auto-loop
```

Each smart-money tick prints a `scan_report` explaining selected opportunities, considered opportunities, counts, rejection reasons, and sell exits. Scanning and trade selection are deterministic Python rules over Polymarket APIs; do not add Claude, Codex, or any LLM call to the scan path.

The dashboard is served at `http://127.0.0.1:8765` by default.

## Strategy To Make Money

The default strategy is smart-money copy trading. The bot does not try to invent an opinion on every market. It waits for public order-flow evidence that profitable wallets are buying the same token.

The live entry should require:

- Recent BUY trades from profitable leaderboard wallets.
- Consensus from multiple distinct wallets on the same token.
- Enough copied USDC size to matter.
- A tradable market with acceptable spread and ask price.
- No existing open position for the same market and outcome.
- Explicit live-trading enablement.
- $5-capped order sizing by default, repeated across qualified opportunities until funds, per-tick cap, or signal exhaustion stops the tick.
- Profit-taking exits before new buys: default +100%/+200%/+300% partial sells, plus peak giveback protection.
- Live position sync from Polymarket before decisions so stale local ledger state does not block fresh entries.
- Stricter handling for crypto up/down micro markets and near-expiry markets.

The expected edge comes from copying strong public flow while avoiding bad execution. This is not guaranteed profit; no-signal/no-trade is part of the strategy.
