---
name: polymarket-bot
description: Claude Code skill for the Polymarket smart-money copy-trading bot. Use for any change to strategy, filters, sizing, exits, journal, or the auto-tuner.
---

# Polymarket Bot Skill

Use this skill when working in this repository: strategy code, filters, live commands, dashboard, trade journal, auto-tuner, BTC edge.

## Guardrails (non-negotiable)

- Never print or commit `.env` values, private keys, API secrets, or passphrases.
- Live trading stays gated by `POLYMARKET_ENABLE_LIVE_TRADING=1`. The only sanctioned bypass is `POLYMARKET_DRY_RUN=1`, which short-circuits SDK BUY/SELL calls and writes to `data/dry_run_state.json` + `data/dry_run_journal.jsonl`.
- No random trade entry. The live strategy only enters smart-money signals with explicit entry criteria.
- Any new live strategy must define explicit entry criteria, spread filters, sizing caps, and duplicate-position checks.
- Update tests when strategy behavior changes.
- No LLM call (Claude, Codex, anything else) in the scanning or trade-selection path. The scanner stays deterministic Python over Polymarket APIs.
- The bot must not have the capability to write or push source code on its own.

## Useful commands

```bash
uv run python -B -m unittest discover -s tests
uv run pmbot --version
uv run pmbot status                                  # snapshot rapide (mode, équité, positions, journal)
uv run pmbot positions                               # table CLI des positions ouvertes, triées par PnL desc
uv run pmbot dashboard
uv run pmbot doctor
uv run pmbot journal-stats
uv run pmbot tune-strategy
POLYMARKET_ENABLE_LIVE_TRADING=1 uv run pmbot auto-loop
POLYMARKET_DRY_RUN=1 uv run pmbot auto-loop          # simulated, no SDK calls, separate ledger
```

Canonical live config: `bash scripts/run_live_70.sh`

Code defaults — permissive entry filters (no trader PnL/volume/ROI floor), percentage-based sizing (50% of cash × conviction, high-conviction up to 80% of cash). No noise fallback. See `scripts/run_live_70.sh` for the exact override set.

CLI surface: 9 Typer commands (`auto-loop`, `dashboard`, `doctor`, `status`, `positions`, `journal-stats`, `tune-strategy`, `bootstrap-creds`, `reset-ledger`) plus the global `--version` / `-V` option. The Typer app is exposed as the `pmbot` console script via `[project.scripts]`; `python -m polymarket_bot.main <cmd>` continues to work as a fallback. `status` and `positions` are read-only — no SDK calls, no network — and automatically pick up the dry-run ledger when `POLYMARKET_DRY_RUN=1` is set. ANSI colors auto-disable when stdout is not a TTY (or when `NO_COLOR=1`); set `POLYMARKET_FORCE_COLOR=1` to keep them through pipes.

## Architecture

- `polymarket_bot/main.py` — tick orchestration, sizing, journal, CLI.
- `polymarket_bot/smart_money.py` — leaderboards, parallel trade fetching (ThreadPoolExecutor), token grouping, scoring, chunked reverse-lookup.
- `polymarket_bot/auto_tuner.py` — bounded overrides from the trade journal (defensive only, gated on 30 trades).
- `polymarket_bot/bitcoin.py` — BTC threshold edge with retry + Coinbase v2 fallback.
- `polymarket_bot/trading.py` — live BUY/SELL order placement and final stake.
- `polymarket_bot/portfolio.py` — local ledger + exit history.
- `polymarket_bot/gamma.py` — Gamma client + reverse-lookup by clob_token_ids.
- `polymarket_bot/strategy.py` — candidate ranking.

## Default strategy

Smart-money copy-trading:

1. Load active Polymarket markets (Gamma scan + keyword scan + reverse-lookup of high-flow tokens).
2. Pull monthly-leaderboard wallets that pass PnL / volume / ROI floors.
3. Inspect their recent BUYs in parallel.
4. Require multi-wallet consensus on the same token, sufficient copied USDC, tight spreads (absolute and relative), price band, freshness.
5. Three passes: strict → relaxed (consensus floor relaxed) → deep fallback (consensus=1, looser filters). One leaderboard+trades fetch shared across all three.
6. Conviction-weighted sizing (0.55x to 2.5x), dynamic per-slot redistribution toward `SMART_CASH_FLOOR_PCT` (5%).
7. Per-position ceiling: `max(SMART_MAX_POSITION_CEILING_USD, equity × SMART_MAX_POSITION_CEILING_PCT)`.
8. Multi-level exits (run before every entry): take-profit ladder +50/+100/+200/+300, trailing stop, peak-protect, stop-loss, cohort-sell, cohort-silent, near-expiry, max-hold-time (24h).
9. No duplicate per market_id, per token, or per event-slug (sports). Per-category cap on sports.
10. BTC edge integrated after the smart-money tick (cap $5, edge ≥ 8%).

## Defensive auto-tuner

Reads `data/trade_journal.jsonl` each tick. Active from 30 closed trades. Bounded rules:

- Stop-loss > 40% of trades: tighten `MAX_CHASE_PREMIUM` ×0.80, `MAX_RELATIVE_SPREAD` ×0.85.
- Consensus=2 trades avg PnL < -$0.30 (≥20 sample): raise `MIN_CONSENSUS` to 3.
- Sports avg PnL < -$0.30 (≥15 sample): bump `SPORTS_SCORE_PENALTY` ×1.5.
- Win rate < 30%: raise `MIN_COPIED_USDC` ×1.5.
- Avg PnL < -$0.20: reduce `POSITION_PCT` ×0.75.

Defensive only: tightens after losses, never loosens after wins. Overrides written to `data/strategy_overrides.json` (auditable).

## Logic

- One wallet alone = noise.
- Several profitable wallets buying the same token in a short window = stronger collective signal.
- A good signal can still be a bad trade if execution is poor (spread, chase, fill).
- No-signal / no-trade is a valid decision.
- Quiet hours stay quiet.

Hierarchy to preserve in any strategy edit: **consensus first, execution quality second, sizing discipline third.** Never replace this with random market selection.

## Current config

The live strategy uses **code defaults** for all entry filters and sizing:
- **No trader quality filters**: `MIN_TRADER_PNL=0`, `MIN_TRADER_VOLUME=0`, `MIN_TRADER_ROI=0`
- **Permissive entry**: `MIN_COPIED_USDC=5`, `MAX_SPREAD=0.25`, `MAX_CHASE_PREMIUM=0.35`, `MAX_ENTRY_SLIPPAGE=0.50`, no signal age limit
- **Sizing**: `POSITION_PCT=0.50`, `MAX_POSITION_CEILING_USD=150`, `MAX_POSITION_CEILING_PCT=0.40`, `CASH_FLOOR_PCT=0.02`. High-conviction signals (5+ wallets / $5k+ copied) get up to 80% of available cash via `HIGH_CONVICTION_BALANCE_FRACTION=0.80`
- **Scan**: 24h lookback, MONTH + ALL leaderboard, top 100 traders
- **Overlays enabled**: leaderboard-position, top10-flow, reverse-lookup
- **BTC edge**: disabled (code default False)
- **Noise fallback**: disabled
- **Sports**: penalty 4, max 8 positions

See `scripts/run_live_70.sh` for the exact override set.

## Editing workflow

1. Read the relevant code (`smart_money.py`, `main.py`, `auto_tuner.py`).
2. Modify while preserving the hierarchy above.
3. Update tests in `tests/test_strategy.py`.
4. Run `uv run python -B -m unittest discover -s tests`.
5. If the change affects the live command, update `scripts/run_live_70.sh`.
6. Update `CHANGELOG.md`, `README.md`, `CLAUDE.md`, `CODEX.md`, and the SKILL files when user-visible.
7. Commit and push.

## Known issues

- The installed `py-clob-client` SDK (≥0.21.0) does not export a `Side` enum. Always pass side as a plain `"BUY"` / `"SELL"` string to `OrderArgs` and `MarketOrderArgs`.
- For FOK market orders, use `create_market_order` + `post_order` — `create_and_post_market_order` does not exist on this SDK version.
- **PnL double-count bug**: In `main.py:_portfolio_update_snapshot` (~line 2177), `open_realized` is unconditionally added to `sum(records)`, which can double-count partial-exit PnLs that are already embedded in position records. Not fixed yet.
