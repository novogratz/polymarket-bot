---
name: polymarket-bot
description: Codex skill for the Polymarket smart-money copy-trading bot. Use for any change to strategy, filters, sizing, exits, journal, or the auto-tuner.
---

# Polymarket Bot Skill

## Current state (v4 — 2026-06-21)

- **Live strategy:** `grinder` — race mode, heavy-favorite near-resolution. All 3 bots.
- **Config:** `configs/profiles/grinder.toml` (bot 1) / `grinder_b.toml` (bots 2 & 3).
- **Launcher:** `bash scripts/run_live_70.sh` / `run_live_b.sh` — preserve ledger/journal. Do NOT use `run_all.sh` for live (it resets the ledger).
- **Sizing:** **FIXED $5/trade** (`fixed_stake_usd = 5.0`) — no Kelly/%/martingale/double-down; bankroll deploys across `bankroll/5` positions.
- **Entry:** ask ∈ [0.80, 0.94], hard cap 0.96 (0.97+ never), ≤4h to close, spread ≤4¢, liq ≥$250, vol ≥$1000.
- **Universe:** `unban_all_markets = true` — all categories, governed by data-driven category auto-disable (`categories.py`) + opt-in forecasting EV/quality gates (`forecast.py`).
- **Exits:** resolved_exit at bid ≥**0.99** (else settle 1.0), confirmed −30% SL on soccer moneylines only, never-sell-below-entry, max-hold 4.5h. No TP, no pause-halts.
- **W/L record:** `data/realized_trade_cache.jsonl` (survives journal rotation).
- **Analysts:** deterministic. The forecasting model (`forecast.py`) is deterministic arithmetic over the ledger — not an LLM.

## Guardrails

- No `.env` values, private keys, or passphrases in output or commits.
- Live trading requires `--live` flag on `pmbot auto-loop`; `--yes` is for script automation only.
- No LLM call in the scanning or trade-selection path.
- No random trade entry beyond bounded `noise_fallback` (disabled on grinder).
- Never delete `data/paper_state.json`, `data/trade_journal.jsonl`, or `data/realized_trade_cache.jsonl` unless the user explicitly asks for a reset.
- The bot must not gain the capability to commit or push source code.

## Commands

```bash
python3 -B -m unittest discover -s tests
uv run pmbot status
uv run pmbot positions
uv run pmbot journal-stats
bash scripts/run_live_70.sh
```

## Key files

- `polymarket_bot/race_strategies.py` — grinder entry/exit engine (`select_grinder`, `_build_eligible_candidates`, `_check_race_exits`).
- `polymarket_bot/main.py` — tick orchestration, sizing, journal.
- `polymarket_bot/config.py` — all `Settings` fields and env-var names.
- `scripts/run_live_70.sh` — canonical live launcher (update when config changes).

## Editing workflow

1. Read `race_strategies.py` + `main.py` for the grinder path.
2. Strategy/filter changes go in `configs/profiles/grinder.toml`.
3. Update tests if behavior changes (`tests/test_strategy.py`).
4. Update `CHANGELOG.md`, `README.md`, `CODEX.md`, and this SKILL.md when user-visible.
