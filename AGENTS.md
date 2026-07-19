# Agent Instructions

Polymarket **weather-only** grinder bot — deterministic signal engine, live order execution, persistent trade journal, and read-only local dashboard. Since 2026-07-06 all 3 bots bet exclusively on weather / temperature markets; bots 2 & 3 additionally gate entries on a multi-model Open-Meteo forecast edge (`polymarket_bot/weather_forecast.py`). A general-purpose grinder mode (any category) and a smart-money lane also exist in the codebase but aren't run live. Treat `.env` and `data/` as local-only state. Never print private keys, API secrets, or wallet credentials.

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

## Current strategy (WEATHER-ONLY + FULL-DEPLOY — 2026-07-10)

**Weather-only grinder:** `weather_only = true` — entry selection keeps ONLY weather / temperature markets (`is_weather_market`); everything else is dropped. Buy ask 0.80–0.94 (hard cap 0.96), ≤24h to close (weather resolves end-of-day), hold until bid ≥ 0.99 (else settle 1.0). **Sizing: EQUAL-WEIGHT FULL DEPLOYMENT** (`full_deploy = true`, `full_deploy_max_position_pct = 0.10`, 2026-07-19) — cash ≈ $0 at all times: every line targets equity/N over all lines (10% cap, $5 floor); held lines top up toward the shared target, never past it (on-chain line-cap guard). "weather" is a first-class category (2026-07-10) — shown in the Telegram 🥇 line and never auto-disabled while the lane is on. Confirmed −30% SL applies to soccer moneylines only, so weather positions never stop out — they ride to resolution; never sell below entry. See `CLAUDE.md` / `docs/STRATEGIES.md`.

## Code style

- Standard-library-first. Use the `Settings` dataclass for new environment variables.
- Persist trade metadata (strategy, exit reason, realized PnL) in the trade journal.
- Add focused unit tests for any filter, sizing, or exit change.
