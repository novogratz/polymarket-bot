# Agent Instructions

This repository contains a Polymarket scanner, local dashboard, paper ledger, and live-trading bot. Treat `.env` and `data/` as local-only state. Never print private keys, API secrets, or wallet credentials.

## Commands

Run tests before pushing:

```bash
python3 -B -m unittest discover -s tests
```

Common bot commands:

```bash
python3 -B -m polymarket_bot.main scan
python3 -B -m polymarket_bot.main dashboard
POLYMARKET_ENABLE_LIVE_TRADING=1 python3 -B -m polymarket_bot.main smart-money-once
POLYMARKET_ENABLE_LIVE_TRADING=1 python3 -B -m polymarket_bot.main auto-loop
```

`auto-loop` runs the smart-money autonomous strategy every `POLYMARKET_AUTO_INTERVAL_SECONDS` seconds.

For faster scans:

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

Each smart-money tick emits a `scan_report` that explains selected opportunities, considered opportunities, counts, and rejection reasons. The scanner must remain deterministic API/rules code and must not call Codex, Claude, or any LLM.

Each selected opportunity should include `selection_reason` and `selection_metrics`. Each tick may place multiple $5-capped smart-money orders until funds run out, a configured per-tick cap is reached, or qualified signals are exhausted.

## Trading Rules

- Do not add random trade selection.
- Live trading must remain gated by `POLYMARKET_ENABLE_LIVE_TRADING=1`.
- Prefer strategies with explicit entry criteria, sizing limits, spread limits, and duplicate-position checks.
- The smart-money strategy copies only recent BUY trades from profitable leaderboard wallets when multiple wallets bought the same token.
- The live strategy may use all available balance only across qualified smart-money opportunities; do not add forced liquidity-only or random buys to zero out cash.
- The sell strategy should run before new entries: partial profit-taking at configured tiers and peak giveback protection via SELL orders.
- Sync live Polymarket positions into the local ledger before entry/exit decisions when available.
- Treat crypto up/down micro markets more strictly than sports or longer-duration markets.
- BTC edge trading is separate and should not be the default autonomous strategy.

## Strategy To Make Money

The bot's default money-making thesis is smart-money consensus. It tries to capture edge by copying repeated BUY flow from profitable Polymarket wallets, then avoiding trades where execution quality is poor.

Required properties for autonomous live entries:

- Multiple distinct profitable wallets bought the same token recently.
- The copied trades clear the configured minimum USDC size.
- The market is liquid, accepting orders, and has a spread under the configured max.
- The ask price is inside the configured price band.
- The local ledger has no duplicate open position for that market/outcome.
- Sizing is capped at the per-trade limit so one trade cannot dominate the account.
- Open live positions are checked for configured take-profit and peak-protection exits before new buys.
- Markets too close to expiry are skipped unless settings explicitly allow them.

If those conditions are not present, the correct behavior is to skip. Do not weaken this into forced trading.

## Code Style

- Keep edits small and aligned with the current standard-library-first code style.
- Use `Settings` for new environment variables.
- Persist bot-visible trade metadata in the portfolio ledger so the dashboard can show it.
- Add focused unit tests for strategy filters and sizing behavior.
