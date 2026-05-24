---
name: polymarket-bot
description: Claude Code skill for the Polymarket smart-money copy-trading bot. Use for any change to strategy, filters, sizing, exits, journal, or the auto-tuner.
---

# Polymarket Bot Skill

Use this skill when working in this repository: strategy code, filters, live commands, dashboard, trade journal, auto-tuner, BTC edge.

## Current state (2026-05-24)

**Live profile:** `baseline_tight` — fork of `baseline`. Fresh leaderboard restart: all 88 archived profiles restored, dry runs reset.

**Bankroll:** $20 USDC.

**Live launcher:** `bash scripts/run_live_70.sh` or `bash scripts/run_all.sh`.

**Universal race rules:** `starting_cash=20.0`, `stake_pct=0.10`, `max_orders_per_tick=5`, `cash_floor_pct=0.02`, `max_hours=4.0`, `sl_min_age_minutes=15`, `resolved_exit_threshold=0.97`.

**Dry race:** 95 profiles auto-discovered from `configs/profiles/*.toml` by both launcher scripts. Kill thresholds relaxed: min 25 trades (auto) / 50 (human), ROI ≤ -25%, WR ≤ 30%, equity floor 30%.

## Guardrails (non-negotiable)

- Never print or commit `.env` values, private keys, API secrets, or passphrases.
- Live trading requires `--live` flag on `pmbot auto-loop`. The `--yes` flag exists only for script automation (e.g., `run_live_70.sh`).
- `POLYMARKET_DRY_RUN` and `POLYMARKET_ENABLE_LIVE_TRADING` env vars are deprecated — use the CLI flags.
- No random trade entry beyond the bounded `noise_fallback` ($10/trade, 4/tick) — currently disabled on most profiles.
- Any new live strategy must define explicit entry criteria, spread filters, sizing caps.
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

Canonical live config: `bash scripts/run_live_70.sh` (~$90 bankroll).

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
11. Noise fallback (cap $10, max 4 per tick) when 0 smart-money signal qualifies AND (positions below min OR cash above 35% of equity).

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

## Editing workflow

1. Read the relevant code (`smart_money.py`, `main.py`, `auto_tuner.py`).
2. Modify while preserving the hierarchy above.
3. Update tests in `tests/test_strategy.py`.
4. Run `uv run python -B -m unittest discover -s tests`.
5. If the change affects the live command, update `scripts/run_live_70.sh`.
6. Update `CHANGELOG.md`, `README.md`, `CLAUDE.md`, `CODEX.md`, and the SKILL files when user-visible.
7. Commit and push.
