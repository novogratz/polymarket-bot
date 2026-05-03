# Codex Guide

Use this file as the Codex entry point for the Polymarket bot.

## Safety

- Never reveal `.env` values, private keys, API secrets, or passphrases.
- Keep live trading gated by `POLYMARKET_ENABLE_LIVE_TRADING=1`.
- Do not add random or unfiltered live trade entry.
- Preserve `data/paper_state.json` unless the user explicitly asks to reset local state.

## Project Map

- `polymarket_bot/main.py`: CLI commands and strategy loops.
- `polymarket_bot/smart_money.py`: autonomous smart-money copy-trading filters.
- `polymarket_bot/bitcoin.py`: optional BTC threshold edge model.
- `polymarket_bot/trading.py`: authenticated live BUY/SELL order placement and sizing.
- `polymarket_bot/dashboard.py`: local real-time HTML dashboard.
- `polymarket_bot/portfolio.py`: local ledger for paper and live positions.
- `tests/test_strategy.py`: strategy and order-building tests.

## Commands

Run tests:

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

Each smart-money loop tick prints a `scan_report` with the selected opportunity, top considered opportunities, trader/trade counts, grouped tokens, rejection reasons, and sell exits. Opportunities include `selection_reason` and `selection_metrics`. The scan path is deterministic Python plus Polymarket APIs; do not add Codex, Claude, or LLM calls to scanning or trade selection.

## Strategy

The default autonomous strategy is smart-money copy trading. It requires profitable leaderboard wallets, recent BUY consensus on the same token, size filters, spread filters, price-band filters, and duplicate-position checks before live entry. A tick may place multiple $5-capped orders across qualified signals until funds, per-tick cap, or signal exhaustion stops it.

Before buying, the bot syncs live Polymarket positions into the ledger and checks live open positions for deterministic exits: default +100%/+200%/+300% partial profit-taking, positive-PnL near-expiry exits, and peak giveback protection. Exits use SELL orders and are recorded in the local ledger.

BTC edge trading is optional and separate.

## Money-Making Logic

The bot should try to make money by following high-quality public order flow, not by guessing or forcing trades. The smart-money strategy looks for multiple profitable leaderboard wallets buying the same token recently, then checks that the market can be entered without a bad fill.

Keep these requirements intact:

- Consensus beats single-wallet signals.
- Tight spreads beat illiquid markets.
- Size caps protect the account from one bad thesis.
- Duplicate-position checks prevent accidental overexposure.
- Profit ladders protect large unrealized gains from round-tripping to zero.
- Live position sync beats stale local dashboard state.
- Near-expiry and crypto micro-market filters reduce noisy forced trades.
- Refusing to trade is correct when the signal is weak.

Never describe this as guaranteed profit. Describe it as an edge-seeking copy-trading system with execution and risk filters.
