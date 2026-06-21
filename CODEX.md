# Codex Guide

Codex entry point for the Polymarket bot. The Claude Code version lives in `CLAUDE.md`. Structured skill files are in `.codex/skills/polymarket-bot/SKILL.md` and `.claude/skills/polymarket-bot/SKILL.md`.

MIT licensed. Tests run in CI — see `.github/workflows/test.yml`.

## Safety

- Never reveal `.env` values, private keys, API secrets, or passphrases.
- Live trading requires the `--live` flag on `pmbot auto-loop`. The `--yes` flag is for script automation only.
- No LLM call in the scanning or trade-selection path. All logic is deterministic Python.
- Preserve `data/paper_state.json`, `data/trade_journal.jsonl`, `data/realized_trade_cache.jsonl` unless explicitly asked to reset.
- The bot must not gain the capability to write or push source code on its own.

## Current state (v4 — 2026-06-21)

**Live strategy:** `grinder` — heavy-favorite, ride-to-resolution. All 3 bots.  
**Config:** `configs/profiles/grinder.toml` (bot 1) / `grinder_b.toml` (bots 2 & 3).  
**Launcher:** `bash scripts/run_live_70.sh` / `run_live_b.sh`. Do **not** use `run_all.sh` for live.  
**Entry:** ask ∈ [0.80, 0.94], hard cap 0.96 (0.97+ never), ≤4h to close, spread ≤4¢, liq ≥$250, vol ≥$1000.  
**Sizing:** **FIXED $5 per trade** (`fixed_stake_usd = 5.0`) — no Kelly/%/martingale/double-down. Bankroll deploys across `bankroll/5` positions.  
**Universe:** `unban_all_markets = true` — every category allowed, governed by the data-driven category auto-disable (`categories.py`) + opt-in forecasting EV/quality gates (`forecast.py`).  
**Exits:** resolved_exit at bid ≥**0.99** (else settle 1.0), confirmed −30% SL on soccer moneylines only, never-sell-below-entry, max-hold 4.5h. No TP, no pause-halts.

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

A binary market at ask 0.80–0.94 within 4 hours of close is pricing near-certainty. The bot pays the spread and holds until bid ≥ 0.99 (else settles at 1.0). The risk controls are the **fixed $5 per-trade cap** (worst single loss = $5), the data-driven category auto-disable, and a confirmed −30% stop-loss on soccer moneylines. v4 optimizes for capital preservation and steady grind, not win-rate or volume.
