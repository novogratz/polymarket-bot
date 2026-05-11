---
name: polymarket-bot
description: Codex skill for the Polymarket smart-money copy-trading bot. Use for any change to strategy, filters, sizing, exits, journal, or the auto-tuner.
---

# Polymarket Bot Skill

Use this skill when working in this repository: strategy code, filters, live commands, dashboard, trade journal, auto-tuner, BTC edge.

## Guardrails (non-negotiable)

- Never print or commit `.env` values, private keys, API secrets, or passphrases.
- Live trading stays gated by `POLYMARKET_ENABLE_LIVE_TRADING=1`.
- No random trade entry. The live strategy only enters smart-money signals with explicit entry criteria.
- Any new live strategy must define explicit entry criteria, spread filters, sizing caps, and duplicate-position checks.
- Update tests when strategy behavior changes.
- No LLM call (Claude, Codex, anything else) in the scanning or trade-selection path.
- The bot must not have the capability to write or push source code on its own.

## Commands

```bash
python3 -B -m unittest discover -s tests
python3 -B -m polymarket_bot.main dashboard
python3 -B -m polymarket_bot.main journal-stats
python3 -B -m polymarket_bot.main tune-strategy
POLYMARKET_ENABLE_LIVE_TRADING=1 python3 -B -m polymarket_bot.main auto-loop
```

Canonical live config: `bash scripts/run_live_70.sh` (~$90 bankroll).

## Architecture

- `polymarket_bot/main.py` — tick orchestration, sizing, journal, CLI.
- `polymarket_bot/smart_money.py` — leaderboards, parallel trade fetching, signals, reverse-lookup.
- `polymarket_bot/auto_tuner.py` — bounded overrides from the trade journal (defensive only).
- `polymarket_bot/bitcoin.py` — BTC threshold edge model.
- `polymarket_bot/trading.py` — live BUY/SELL order placement and final stake.
- `polymarket_bot/portfolio.py` — local ledger.
- `polymarket_bot/gamma.py` — Gamma client + reverse-lookup.
- `polymarket_bot/strategy.py` — candidate ranking.

## Default strategy

Smart-money copy-trading:

1. Load active Polymarket markets (Gamma scan + keyword scan + reverse-lookup).
2. Pull leaderboard wallets that pass PnL / volume / ROI floors.
3. Inspect recent BUYs in parallel.
4. Require multi-wallet consensus, sufficient copied USDC, tight spreads, price band, freshness.
5. Three passes: strict → relaxed → deep fallback. One shared leaderboard+trades fetch.
6. Conviction-weighted sizing (0.55x to 2.5x), dynamic per-slot toward `SMART_CASH_FLOOR_PCT`.
7. Multi-level exits before every new entry: take-profit ladder +50/+100/+200/+300, trailing stop, peak-protect, stop-loss, cohort-sell, cohort-silent, near-expiry, max-hold-time.
8. No duplicate per market_id, per token, per event-slug. Per-category cap on sports.
9. BTC edge integrated after the smart-money tick (cap $5, edge ≥ 8%).

## Money-making logic

- Single wallet = noise.
- Multiple profitable wallets buying the same token in a short window = stronger collective signal.
- A good signal can be a bad trade if execution is poor (spread, chase, fill).
- Risk control matters: size by bankroll fraction, cap per-trade dollars, exit on flip signals.
- Skipping is a valid action when the setup is not clean.

When editing strategy code, preserve this hierarchy: **consensus first, execution quality second, sizing discipline third.** Never replace it with random market selection.

## Known issues

- The installed `py-clob-client` SDK (≥0.21.0) does not export a `Side` enum. Always pass side as a plain `"BUY"` / `"SELL"` string.
- For FOK market orders, use `create_market_order` + `post_order` — `create_and_post_market_order` does not exist on this SDK version.
- **PnL double-count bug**: In `main.py:_portfolio_update_snapshot` (~line 2177), `open_realized` is unconditionally added to `sum(records)`, which can double-count partial-exit PnLs that are already embedded in position records. Not fixed yet.
