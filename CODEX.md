# Codex Guide

Codex entry point for the Polymarket bot. The Claude Code version lives in `CLAUDE.md`. Structured skill files are in `.codex/skills/polymarket-bot/SKILL.md` and `.claude/skills/polymarket-bot/SKILL.md`.

MIT licensed. Tests run in CI — see `.github/workflows/test.yml`.

## Safety

- Never reveal `.env` values, private keys, API secrets, or passphrases.
- Live trading requires the `--live` flag on `pmbot auto-loop`. The `--yes` flag is for script automation only.
- No LLM call in the scanning or trade-selection path. All logic is deterministic Python.
- Preserve `data/paper_state.json`, `data/trade_journal.jsonl`, `data/realized_trade_cache.jsonl` unless explicitly asked to reset.
- The bot must not gain the capability to write or push source code on its own.

## Current state (2026-07-06 — weather-only)

This is a general-purpose engine (`polymarket_bot/race_strategies.py`) that can run several strategies off a TOML profile. **Live strategy: `grinder` — WEATHER-ONLY. All 3 bots.** Only bot 2 (and bot 3, sharing `grinder_b.toml`) additionally cross-checks entries against a multi-model Open-Meteo forecast (`polymarket_bot/weather_forecast.py`, `weather_forecast_min_edge=0.10`, `weather_min_bracket_margin_c=2.0`) — bot 1's `grinder.toml` has `weather_only=true` but neither forecast gate set, so it trades weather markets on price/liquidity heuristics alone.

**Config:** `configs/profiles/grinder.toml` (bot 1) / `grinder_b.toml` (bots 2 & 3).  
**Launcher:** `bash scripts/run_live_70.sh` / `run_live_b.sh`. Do **not** use `run_all.sh` for live.  
**Universe:** `weather_only = true` — ONLY weather / temperature markets (`is_weather_market`); everything else dropped at selection. "weather" is a first-class category (2026-07-10), never auto-disabled while the lane is on.  
**Entry:** ask ∈ [0.80, 0.94], hard cap 0.96 (0.97+ never), ≤24h to close (weather resolves end-of-day), spread ≤4¢, liq ≥$250, vol ≥$1000.  
**Sizing:** **5% FIXED-FRACTION, NO REINFORCEMENT** (`full_deploy = true`, `full_deploy_max_position_pct = 0.05`, 2026-07-11) — every NEW position = exactly 5% of equity ($5 floor); a held market is NEVER bought again (no top-up/redistribution/re-bet); cash without a new market waits. Rollback: `full_deploy=false, fixed_stake_usd=5.0`.
**Exits:** resolved_exit at bid ≥**0.99** (else settle 1.0), never-sell-below-entry, max-hold backstop. The −30% confirmed SL gates on soccer moneylines only → weather positions never stop out. No TP, no pause-halts.

## Project map

- `polymarket_bot/main.py` — CLI, tick orchestration, journal writer.
- `polymarket_bot/race_strategies.py` — grinder entry/exit engine.
- `polymarket_bot/models.py` — shared dataclasses, exclusion filters.
- `polymarket_bot/portfolio.py` — local ledger, positions, exits.
- `polymarket_bot/trading.py` — live CLOB order placement.
- `polymarket_bot/gamma.py` — Gamma market scan.
- `polymarket_bot/weather_forecast.py` — Open-Meteo multi-model consensus + edge/bracket-margin gates (bots 2 & 3).
- `scripts/run_live_70.sh` — canonical live launcher.
- `tests/` — 769 tests.

## Commands

```bash
uv run python -B -m unittest discover -s tests
uv run pmbot status
uv run pmbot positions
uv run pmbot journal-stats
bash scripts/run_live_70.sh
```

## Thesis

A binary market at ask 0.80–0.94 within 24 hours of close (weather resolves end-of-day) is pricing near-certainty. The bot pays the spread and holds until bid ≥ 0.99 (else settles at 1.0). The risk controls are the **5% fixed-fraction per-position cap** (worst single-line loss ≈ 5% of equity, no re-bet on a held market), the data-driven category auto-disable, and — for non-weather grinder candidates only — a confirmed −30% stop-loss on soccer moneylines. Optimizes for capital preservation and steady grind, not win-rate or volume.
