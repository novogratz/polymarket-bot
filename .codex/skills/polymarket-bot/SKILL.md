---
name: polymarket-bot
description: Codex skill for the Polymarket trading engine (grinder / weather / smart-money copy-trading). Use for any change to strategy, filters, sizing, exits, journal, or the auto-tuner. Current live focus is weather (bots 2 & 3) + grinder (bot 1, general-purpose).
---

# Polymarket Bot Skill

General-purpose engine, several strategies share one pipeline. Not all 3 bots run
the same strategy — see below.

## Current state (v4 — 2026-06-21)

- **Live strategy:** bot 1 = `grinder` (general-purpose, all categories); bots 2 & 3
  = `weather` mode (temperature markets only, Open-Meteo forecast-gated,
  `race_weather_only=true`). Same engine, different candidate set + edge model.
- **Config:** `configs/profiles/grinder.toml` (bot 1) / `grinder_b.toml` (bots 2 & 3).
- **Launcher:** `bash scripts/run_live_70.sh` / `run_live_b.sh` — preserve ledger/journal. Do NOT use `run_all.sh` for live (it resets the ledger).
- **Sizing:** **FIXED $5/trade** (`fixed_stake_usd = 5.0`) — no Kelly/%/martingale/double-down; bankroll deploys across `bankroll/5` positions. Same on both modes.
- **Entry (grinder, bot 1):** ask ∈ [0.80, 0.94], hard cap 0.96 (0.97+ never), ≤4h to close, spread ≤4¢, liq ≥$250, vol ≥$1000.
- **Entry (weather, bots 2/3):** same engine restricted to temperature/degree-bracket markets (`polymarket_bot/weather_forecast.py`); extra edge gate `weather_forecast_min_edge=0.10` (model_P(outcome) − ask ≥ 0.10) and bracket-margin guard `weather_min_bracket_margin_c=2.0` (skip "No" bets within 2°C of the bracket threshold). 24h entry window, lower liquidity floors.
- **Universe (grinder, bot 1):** `unban_all_markets = true` — all categories, governed by data-driven category auto-disable (`categories.py`) + opt-in forecasting EV/quality gates (`forecast.py`).
- **Exits:** resolved_exit at bid ≥**0.99** (else settle 1.0), confirmed −30% SL on soccer moneylines only, never-sell-below-entry, max-hold 4.5h. No TP, no pause-halts. Same on both modes.
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
