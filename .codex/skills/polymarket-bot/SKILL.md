---
name: polymarket-bot
description: Codex skill for the Polymarket smart-money copy-trading bot. Use for any change to strategy, filters, sizing, exits, journal, or the auto-tuner.
---

# Polymarket Bot Skill

## Current state (WEATHER-ONLY + FULL-DEPLOY — 2026-07-10)

- **Live strategy:** `grinder` — race mode, **WEATHER-ONLY** (2026-07-06): `weather_only = true` keeps ONLY weather / temperature markets (`is_weather_market`); everything else is dropped at selection. All 3 bots.
- **Config:** `configs/profiles/grinder.toml` (bot 1) / `grinder_b.toml` (bots 2 & 3).
- **Launcher:** `bash scripts/run_live_70.sh` / `run_live_b.sh` — preserve ledger/journal. Do NOT use `run_all.sh` for live (it resets the ledger).
- **Sizing:** **FULL-DEPLOY + diversification cap** (`full_deploy = true`, `full_deploy_max_position_pct = 0.05`, 2026-07-09/11) — 100% invested, spread wide: cash/N across the tick's picks, **no position may exceed 5% of equity** ($5 floor), after 3 dry ticks (no new market, `topup_dry_ticks`) leftover cash splits EQUALLY across existing positions, exempt from the 5% cap (equality is the constraint). Rollback: `full_deploy=false, fixed_stake_usd=5.0`.
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
