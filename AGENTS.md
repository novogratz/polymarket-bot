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

Each smart-money tick emits a `scan_report` that explains selected opportunities, considered opportunities, counts, and rejection reasons. The scanner must remain deterministic API/rules code and must not call Codex, Claude, or any LLM.

## Trading Rules

- Do not add random trade selection.
- Live trading must remain gated by `POLYMARKET_ENABLE_LIVE_TRADING=1`.
- Prefer strategies with explicit entry criteria, sizing limits, spread limits, and duplicate-position checks.
- The smart-money strategy copies only recent BUY trades from profitable leaderboard wallets when multiple wallets bought the same token.
- BTC edge trading is separate and should not be the default autonomous strategy.

## Strategy To Make Money

The bot's default money-making thesis is smart-money consensus. It tries to capture edge by copying repeated BUY flow from profitable Polymarket wallets, then avoiding trades where execution quality is poor.

Required properties for autonomous live entries:

- Multiple distinct profitable wallets bought the same token recently.
- The copied trades clear the configured minimum USDC size.
- The market is liquid, accepting orders, and has a spread under the configured max.
- The ask price is inside the configured price band.
- The local ledger has no duplicate open position for that market/outcome.
- Sizing is capped so one trade cannot dominate the account.

If those conditions are not present, the correct behavior is to skip. Do not weaken this into forced trading.

## Code Style

- Keep edits small and aligned with the current standard-library-first code style.
- Use `Settings` for new environment variables.
- Persist bot-visible trade metadata in the portfolio ledger so the dashboard can show it.
- Add focused unit tests for strategy filters and sizing behavior.
