---
name: polymarket-bot
description: Codex skill for the Polymarket trading engine (grinder / weather / smart-money copy-trading). Use for any change to strategy, filters, sizing, exits, journal, or the auto-tuner. Current live strategy is weather-only on all 3 bots (bots 2 & 3 additionally forecast-gated).
---

# Polymarket Bot Skill

General-purpose engine, several strategies share one pipeline. All 3 bots currently
run the same strategy (weather-only) — see below.

## Current state (WEATHER-ONLY + FULL-DEPLOY — 2026-07-10)

- **Live strategy:** `grinder` — race mode, **WEATHER-ONLY** (2026-07-06): `weather_only = true` keeps ONLY weather / temperature markets (`is_weather_market`); everything else is dropped at selection. All 3 bots. Only bots 2 & 3 (`grinder_b.toml`) additionally cross-check entries against a multi-model Open-Meteo forecast (`polymarket_bot/weather_forecast.py`) — edge gate `weather_forecast_min_edge=0.10` (model_P(outcome) − ask ≥ 0.10) and bracket-margin guard `weather_min_bracket_margin_c=2.0` (skip "No" bets within 2°C of the bracket threshold). Bot 1 (`grinder.toml`) has neither gate set (both default `0.0` = off), so it trades weather on price/liquidity heuristics alone.
- **Config:** `configs/profiles/grinder.toml` (bot 1) / `grinder_b.toml` (bots 2 & 3).
- **Launcher:** `bash scripts/run_live_70.sh` / `run_live_b.sh` — preserve ledger/journal. Do NOT use `run_all.sh` for live (it resets the ledger).
- **Sizing:** **EQUAL-WEIGHT FULL DEPLOYMENT** (`full_deploy = true`, `full_deploy_max_position_pct = 0.10`, 2026-07-19) — cash ≈ $0 at all times: every line targets equity/N over all lines (10% cap, $5 floor); held lines top up toward the shared target, never past it (on-chain line-cap guard). Rollback: `full_deploy=false, fixed_stake_usd=5.0`.
- **Entry:** ask ∈ [0.80, 0.94], hard cap 0.96 (0.97+ never), ≤24h to close (weather resolves end-of-day), spread ≤4¢, liq ≥$250, vol ≥$1000.
- **Universe:** weather only. "weather" is a first-class v4 category (2026-07-10), shown in the Telegram 🥇 line, never auto-disabled while the lane is on (starvation guard).
- **Exits:** resolved_exit at bid ≥**0.99** (else settle 1.0), never-sell-below-entry, max-hold backstop. The −30% confirmed SL gates on soccer moneylines only → weather positions never stop out. No TP, no pause-halts.
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
