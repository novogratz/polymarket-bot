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
- **Sizing:** **EQUAL-WEIGHT FULL DEPLOYMENT** (`full_deploy = true`, 5% soft entry/top-up cap, 10% redistribution-only hard cap) — every line targets equity/N over all lines ($5 floor); held lines top up toward the shared target, never past the on-chain line-cap guard. Cash approaches $0 when enough eligible distinct lines exist; caps may leave cash idle when the safe universe is small. Rollback: `full_deploy=false, fixed_stake_usd=5.0`.
- **Entry:** all three live profiles admit asks ∈ [0.85, 0.97] with a matching 0.97 hard cap and a +0.01 forecast-edge gate. Markets must close within 6h, spread ≤4¢, narrow `between X–Y°` brackets stay excluded, and Open-Meteo remains fail-closed.
- **Universe:** weather only. "weather" is a first-class v4 category (2026-07-10), shown in the Telegram 🥇 line, never auto-disabled while the lane is on (starvation guard).
- **Exits:** resolved_exit at bid ≥**0.99** (else settle 1.0) and an absolute weather stop at executable bid ≤**0.55**. The weather stop explicitly bypasses the normal loss-sale guard.
- **W/L record:** `data/realized_trade_cache.jsonl` (survives journal rotation).
- **Live report integrity (2026-07-31):** weather-only launchers set `LIVE_ANALYST_WEATHER_ONLY=1`, so Telegram statistics reject records outside the active lane and group “today” by US/Eastern. A custom journal automatically colocates its realized cache unless a cache path was explicitly configured, preventing test/dry-run closes from contaminating production history.
- **Forecast audit trail (2026-07-31):** admitted trades persist model probability, calculated edge, city, broad region, and target date in the position and realized journal.
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
