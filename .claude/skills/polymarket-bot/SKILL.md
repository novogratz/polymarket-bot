---
name: polymarket-bot
description: Claude skill for changes to the Polymarket bot's strategy, filters, forecasts, sizing, exits, journals, reporting, or offline tuner. The maintained live strategy is forecast-gated and weather-only on all three bots.
---

# Polymarket Bot Skill

Read `AGENTS.md` first. The maintained production behavior is:

- Weather and temperature markets only; cryptocurrency and unrelated categories are excluded.
- Executable ask from 0.90 through 0.97 and at most six hours to configured close.
- Open-Meteo probability at least `ask + 0.02`; missing forecast data fails closed.
- At most two positions for one city/date and no opposite outcomes on one binary.
- Equal-weight full deployment with a 5% target, 10% hard line cap, and venue minimum.
- No weather stop loss; hold for an executable 0.99 bid or settlement and never intentionally sell below entry.
- Spread and local solar-hour gates disabled.

Profiles are `configs/profiles/grinder.toml`, `grinder_b.toml`, and `grinder_c.toml`. Launchers are `scripts/run_live_70.sh`, `run_live_b.sh`, and `run_live_c.sh`.

For every strategy change, keep selection deterministic, add focused tests, persist audit metadata, align the production profiles, update customer documentation, and run:

```bash
uv run ruff check .
uv run python -B -m unittest discover -s tests
```

Never expose credentials, introduce random live fallback, call an LLM from live selection, run live orders as tests, or edit active runtime state.
