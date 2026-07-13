# Agent Instructions

Polymarket trading engine — deterministic signal engine, live order execution, persistent trade journal, and read-only local dashboard. The engine supports multiple strategies (grinder, weather, smart-money) off a shared pipeline; current live focus is the weather strategy (bots 2 & 3) plus grinder (bot 1, general-purpose). Treat `.env` and `data/` as local-only state. Never print private keys, API secrets, or wallet credentials.

See `CLAUDE.md` and `CODEX.md` for agent-specific entry points. Structured skill files are in `.claude/skills/` and `.codex/skills/`.

## Guardrails

- Never reveal `.env` values, private keys, API secrets, or passphrases.
- Live trading requires the `--live` flag on `pmbot auto-loop`. The `--yes` flag is for script automation only (`scripts/run_live_70.sh`).
- No LLM call in the scanning or trade-selection path. The bot is deterministic Python over Polymarket APIs.
- No random or unfiltered live trade entry. `noise_fallback` is disabled on the grinder profile.
- Every change to strategy behavior must be covered by a unit test.

## Commands

```bash
uv run python -B -m unittest discover -s tests
uv run pmbot status
uv run pmbot positions
uv run pmbot journal-stats
bash scripts/run_live_70.sh
```

## Current strategy (v4 — 2026-06-21)

**Grinder:** buy ask 0.80–0.94 (hard cap 0.96), ≤4h to close, hold until bid ≥ 0.99 (else settle 1.0). **Fixed $5 per trade** (no Kelly/%/double-down). `unban_all_markets` — all categories allowed, governed by the data-driven category auto-disable (`categories.py`) + opt-in forecasting EV/quality gates (`forecast.py`). Confirmed −30% SL on soccer moneylines only; never sell below entry. Up to 12 new $5 bets per tick. See `CLAUDE.md` / `docs/STRATEGIES.md`.

## Code style

- Standard-library-first. Use the `Settings` dataclass for new environment variables.
- Persist trade metadata (strategy, exit reason, realized PnL) in the trade journal.
- Add focused unit tests for any filter, sizing, or exit change.
