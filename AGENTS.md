# Repository Agent Guide

This repository implements a deterministic, weather-only Polymarket trading engine with live execution, persistent journals, and read-only reporting.

## Non-negotiable rules

- Never reveal or commit `.env` values, private keys, API secrets, wallet credentials, or passphrases.
- Never run live trading as part of development or testing.
- Live execution requires `pmbot auto-loop --live`; `--yes` only automates the confirmation in maintained launchers.
- Do not introduce LLM calls, randomness, or unfiltered fallback into scanning or trade selection.
- Preserve weather-only live eligibility. Cryptocurrency and unrelated categories are excluded.
- Every strategy behavior change requires a focused unit test.
- Treat `data/` as local runtime state and preserve unrelated user changes.

## Current live policy

- Weather and temperature markets only.
- Fresh-entry candidate ask from 0.90 through 0.97; the FOK price guard is candidate ask plus one tick, capped at 0.99.
- Close or game start within six hours, with a same-target-day exception for stale Gamma weather deadlines that remain open.
- For fresh entries, multi-model Open-Meteo probability must be at least `candidate ask + 0.02`; missing forecast data fails closed.
- Held-line leftover-cash redistribution may top up without the forecast-edge or bracket-margin gates; it cannot create a new position.
- Reported liquidity and 24-hour volume must each be at least $50.
- At most two positions for one city and target date; opposite outcomes on the same binary are blocked.
- Equal-weight full-deployment sizing: 5% soft line cap and 10% held-line redistribution cap, each subject to a $5 floor and venue share minimum.
- Weather positions have no stop loss and are held until an executable 0.99 bid or settlement.
- Spread and local solar-hour gates are disabled.

See `docs/STRATEGIES.md` for the complete policy.

## Development commands

```bash
uv run ruff check .
uv run python -B -m unittest discover -s tests
uv run pmbot status
uv run pmbot positions
uv run pmbot journal-stats
```

## Implementation conventions

- Prefer the standard library.
- Add environment-backed settings through the `Settings` dataclass.
- Persist strategy, decision metadata, exit reason, and realized PnL in the appropriate journal.
- Use explicit, testable boundaries for price, time, sizing, and exposure.
- Update README, strategy documentation, and changelog when customer-visible behavior changes.
