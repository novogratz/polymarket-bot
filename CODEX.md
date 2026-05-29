# Codex Guide

Codex entry point for the Polymarket bot. The Claude Code version lives in `CLAUDE.md`. Structured skill files are in `.codex/skills/polymarket-bot/SKILL.md` and `.claude/skills/polymarket-bot/SKILL.md`.

MIT licensed. Tests run in CI — see `.github/workflows/test.yml`.

## Safety

- Never reveal `.env` values, private keys, API secrets, or passphrases.
- Live trading requires the `--live` flag on `pmbot auto-loop`. The `--yes` flag is for script automation only.
- No LLM call in the scanning or trade-selection path. All logic is deterministic Python.
- Preserve `data/paper_state.json`, `data/trade_journal.jsonl`, `data/realized_trade_cache.jsonl` unless explicitly asked to reset.
- The bot must not gain the capability to write or push source code on its own.

## Current state (2026-05-29)

**Live strategy:** `grinder` — heavy-favorite, ride-to-resolution.  
**Config:** `configs/profiles/grinder.toml` (single source of truth).  
**Launcher:** `bash scripts/run_live_70.sh`. Do **not** use `run_all.sh` for live.  
**Entry:** bid ∈ [0.89, 0.94], ≤4h to close, spread ≤2¢, day-change ≤10 %.  
**Exits:** resolved_exit at bid ≥0.99, max-hold 4.5h. No TP, no SL.  
**Sizing:** 40 % per trade, max 2 concurrent. Bankroll $123.

## Project map

- `polymarket_bot/main.py` — CLI, tick orchestration, journal writer.
- `polymarket_bot/race_strategies.py` — grinder entry/exit engine.
- `polymarket_bot/models.py` — shared dataclasses, exclusion filters.
- `polymarket_bot/portfolio.py` — local ledger, positions, exits.
- `polymarket_bot/trading.py` — live CLOB order placement.
- `polymarket_bot/gamma.py` — Gamma market scan.
- `scripts/run_live_70.sh` — canonical live launcher.
- `tests/` — 553 tests.

## Commands

```bash
uv run python -B -m unittest discover -s tests
uv run pmbot status
uv run pmbot positions
uv run pmbot journal-stats
bash scripts/run_live_70.sh
```

## Thesis

A binary market at bid 0.89–0.94 within 4 hours of close is pricing near-certainty. The bot pays the spread and holds until bid ≥ 0.99. No stop-loss — the exclusion filters and price-stability gate are the risk controls. 40 % sizing means one bad trade is survivable.
