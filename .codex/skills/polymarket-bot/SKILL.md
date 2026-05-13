---
name: polymarket-bot
description: Codex skill for the Polymarket smart-money copy-trading bot. Use for any change to strategy, filters, sizing, exits, journal, or the auto-tuner.
---

# Polymarket Bot Skill

Use this skill when working in this repository: strategy code, filters, live commands, dashboard, trade journal, auto-tuner, BTC edge.

## Guardrails (non-negotiable)

- Never print or commit `.env` values, private keys, API secrets, or passphrases.
- Live trading stays gated by `POLYMARKET_ENABLE_LIVE_TRADING=1`.
- No random trade entry beyond the bounded `noise_fallback` ($10/trade, 4/tick).
- Any new live strategy must define explicit entry criteria, spread filters, sizing caps, and duplicate-position checks.
- Update tests when strategy behavior changes.
- No LLM call (Codex, Claude, anything else) in the scanning or trade-selection path.
- The bot must not have the capability to write or push source code on its own.

## Commands

```bash
uv run python -B -m unittest discover -s tests
uv run pmbot auto-loop --profile copy-advanced --live --yes
POLYMARKET_PAPER_BALANCE_USD=100 uv run pmbot auto-loop --profile copy-advanced --dry-run --run mirror-whales
uv run pmbot dashboard
uv run pmbot doctor
uv run pmbot status
uv run pmbot positions
uv run pmbot journal-stats
```

## Architecture

- `polymarket_bot/main.py` — CLI commands, tick orchestration, sizing, journal.
- `polymarket_bot/mirror.py` — mirror-mode strategy: whale discovery, polling, eligibility, sizing, buy/sell, drawdown limits.
- `polymarket_bot/smart_money.py` — legacy smart-money strategy.
- `polymarket_bot/dry_run_runs.py` — named dry-run lifecycle (`--run <name>`).
- `polymarket_bot/auto_tuner.py` — defensive overrides from trade journal.
- `polymarket_bot/trading.py` — live order placement (FOK BUY, GTC SELL), SDK v2 wrapper with `cancel_active_orders_for_token`.
- `polymarket_bot/portfolio.py` — local ledger (cash, positions, exits).
- `polymarket_bot/gamma.py` — Gamma client + reverse-lookup.
- `polymarket_bot/strategy.py` — candidate ranking.
- `polymarket_bot/profiles.py` — TOML profile loader.

## Default strategy (mirror mode)

Whale copy-trading:

1. Static target wallets + weekly leaderboard discovery (8 categories, min PnL $10k).
2. Poll BUY trades per target, filter by age/seen/min stake/price band.
3. `[live]` Sync positions + on-chain cash before exits.
4. Exit waterfall: take-profit (+25% to +300%), trailing stop, peak-protect, stop-loss, cohort, near-expiry, max-hold.
5. GTC SELLs recorded as exits immediately (status "live"/"delayed"). Stale orders auto-cancelled via SDK.
6. BUY filters: chase ≤ 5%, liquidity ≥ $10k, category cap 50%, max 12 open, no duplicates.
7. Sizing: `whale_stake × 0.20` (tiered up to 0.35), capped by equity × 25%, $250 hard cap, cash.

## Money-making logic

- Single wallet = noise.
- Multiple profitable wallets buying the same token = stronger collective signal.
- Good signal + bad execution = bad trade.
- Skipping is valid when no setup is clean.

When editing, preserve: **consensus first, execution quality second, sizing discipline third.**
