# Agent Instructions

Polymarket grinder bot — deterministic signal engine, live order execution, persistent trade journal, and read-only local dashboard. Treat `.env` and `data/` as local-only state. Never print private keys, API secrets, or wallet credentials.

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

## Current strategy (2026-05-29)

**Grinder:** buy bid 0.89–0.94, ≤4h to close, hold until bid ≥ 0.99. No SL. 40 % stake, 2 concurrent max. Exclusion filters block exact scores, weather, O/U 0.5/5.5+. Price-stability gate blocks markets that moved >10 % today.

## Code style

- Standard-library-first. Use the `Settings` dataclass for new environment variables.
- Persist trade metadata (strategy, exit reason, realized PnL) in the trade journal.
- Add focused unit tests for any filter, sizing, or exit change.
