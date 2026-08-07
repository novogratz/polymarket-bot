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
- Entry ask from 0.90 through 0.97 and at most six hours to configured close.
- Multi-model Open-Meteo probability must be at least `ask + 0.02`; missing forecast data fails closed.
- At most two positions for one city and target date; opposite outcomes on the same binary are blocked.
- Equal-weight full-deployment sizing: approximately 5% target, 10% hard line cap, and venue minimum order constraints.
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
