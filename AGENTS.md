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

## Trading Rules

- Do not add random trade selection.
- Live trading must remain gated by `POLYMARKET_ENABLE_LIVE_TRADING=1`.
- Prefer strategies with explicit entry criteria, sizing limits, spread limits, and duplicate-position checks.
- The smart-money strategy copies only recent BUY trades from profitable leaderboard wallets when multiple wallets bought the same token.
- BTC edge trading is separate and should not be the default autonomous strategy.

## Code Style

- Keep edits small and aligned with the current standard-library-first code style.
- Use `Settings` for new environment variables.
- Persist bot-visible trade metadata in the portfolio ledger so the dashboard can show it.
- Add focused unit tests for strategy filters and sizing behavior.
