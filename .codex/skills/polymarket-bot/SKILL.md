---
name: polymarket-bot
description: Codex skill for changes to the Polymarket bot's strategy, filters, forecasts, sizing, exits, journals, reporting, or offline tuner. The maintained live strategy is forecast-gated and weather-only on all three bots.
---

# Polymarket Bot Skill

Use this skill for any customer-visible or behavioral change to the trading engine.

## Live source of truth

- Profiles: `configs/profiles/grinder.toml`, `grinder_b.toml`, and `grinder_c.toml`.
- Launchers: `scripts/run_live_70.sh`, `run_live_b.sh`, and `run_live_c.sh`.
- Universe: supported weather and temperature markets only; cryptocurrency and all other categories are excluded.
- Entry: executable ask from 0.90 through 0.97, no more than six hours to configured close, and Open-Meteo probability at least `ask + 0.02`. Missing forecast data fails closed.
- Exposure: no more than two lines for one city/date and no opposite outcomes on the same binary.
- Sizing: equal-weight full deployment with a 5% target, 10% hard line cap, and approximately $5 venue minimum.
- Exit: no weather stop loss; hold for an executable 0.99 bid or settlement, and never intentionally sell below entry.
- Spread and local solar-hour gates are disabled.
- Decision audit: `data/decision_journal.jsonl`.
- Realized outcomes: `data/realized_trade_cache.json` or the configured colocated cache.

Full deployment can leave cash idle when the eligible universe is too small or not executable. Do not relax deterministic eligibility merely to create activity.

## Required workflow

1. Read `AGENTS.md`, the affected profile, implementation, and relevant tests.
2. Preserve deterministic scanning and selection; do not introduce an LLM or random fallback.
3. Add a focused unit test for every strategy behavior change.
4. Keep all three production profiles aligned unless the request explicitly requires a documented difference.
5. Persist decision metadata and exit outcomes for auditability.
6. Run:

   ```bash
   uv run ruff check .
   uv run python -B -m unittest discover -s tests
   ```

7. Update `README.md`, `docs/STRATEGIES.md`, and `CHANGELOG.md` when live behavior changes.

Never expose credentials, edit `.env`, run a live order as verification, or manipulate runtime history while a bot is active.
