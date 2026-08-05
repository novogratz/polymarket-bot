# Codex Guide

Codex entry point for the Polymarket bot. The Claude Code version lives in `CLAUDE.md`. Structured skill files are in `.codex/skills/polymarket-bot/SKILL.md` and `.claude/skills/polymarket-bot/SKILL.md`.

MIT licensed. Tests run in CI — see `.github/workflows/test.yml`.

## Safety

- Never reveal `.env` values, private keys, API secrets, or passphrases.
- Live trading requires the `--live` flag on `pmbot auto-loop`. The `--yes` flag is for script automation only.
- No LLM call in the scanning or trade-selection path. All logic is deterministic Python.
- Preserve `data/paper_state.json`, `data/trade_journal.jsonl`, `data/realized_trade_cache.jsonl` unless explicitly asked to reset.
- The bot must not gain the capability to write or push source code on its own.

## Current state (2026-08-04 — category-aware late resolution)

This is a general-purpose engine (`polymarket_bot/race_strategies.py`) that can run several strategies off a TOML profile. **All 3 live grinders use a deterministic category-aware late-resolution lane:** weather, already-started sports, and objective economics releases within two hours. All crypto is blocked. Subjective and historically unsafe categories remain excluded.

**Config:** `configs/profiles/grinder.toml` (bot 1) / `grinder_b.toml` (bots 2 & 3).  
**Launcher:** `bash scripts/run_live_70.sh` / `run_live_b.sh`. Do **not** use `run_all.sh` for live.  
**Universe:** `weather_only = false`, `unban_all_markets = true`, narrowed in code to the deterministic late-resolution whitelist.
**Entry:** all three live profiles admit asks through 0.97 with a matching hard cap and require at least +1 point of Open-Meteo edge. Markets must close within 6h, target the city's current local date, and enter after solar 15:00. Narrow ranges are excluded; Open-Meteo validation fails closed, region/date exposure is capped at two lines, and realized Brier score pauses entries when calibration deteriorates.
**Sizing:** **EQUAL-WEIGHT FULL DEPLOYMENT** (`full_deploy = true`, `full_deploy_max_position_pct = 0.10`, 2026-07-19) — cash ≈ $0 at all times: every line targets equity/N over all lines (10% cap, $5 floor); held lines top up toward the shared target, never past it (on-chain line-cap guard). Rollback: `full_deploy=false, fixed_stake_usd=5.0`.
**Exits:** no stop losses. Positions hold for a resolved-exit bid ≥**0.99** or settlement; forecast-flip exits are also disabled.

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

A binary market at ask 0.80–0.94 within 24 hours of close (weather resolves end-of-day) is pricing near-certainty. The bot pays the spread and holds until bid ≥ 0.99 (else settles at 1.0). All stop-loss paths are disabled; position sizing, deterministic entry filters, and the data-driven category auto-disable are the remaining risk controls. Optimizes for capital preservation and steady grind, not win-rate or volume.
