# Agent Instructions

This repository contains the Polymarket grinder bot — a deterministic signal engine, a live order execution layer, a persistent trade journal, and a read-only local dashboard. Treat `.env` and `data/` as local-only state. Never print private keys, API secrets, or wallet credentials.

For the Claude Code and Codex entry points, see `CLAUDE.md` and `CODEX.md`. The structured skill files live at `.claude/skills/polymarket-bot/SKILL.md` and `.codex/skills/polymarket-bot/SKILL.md`.

## Guardrails

- Never reveal `.env` values, private keys, API secrets, or passphrases.
- Live trading requires the `--live` flag on `pmbot auto-loop`. The `--yes` flag is for script automation only (`scripts/run_live_70.sh`).
- No LLM call is permitted in the scanning or trade-selection path. The bot is deterministic Python over Polymarket APIs.
- No random or unfiltered live trade entry. The `noise_fallback` lane is the only forced-trade path and is hard-capped at $10 per trade, 4 trades per tick (disabled on the grinder profile).
- Strategy adjustments at runtime are data files (`data/strategy_overrides.json`), not code rewrites. The bot must not gain the capability to commit or push source code.
- Every change to strategy behavior must be covered by a unit test in `tests/test_strategy.py`.

## Commands

Run tests before pushing:

```bash
uv run python -B -m unittest discover -s tests
```

Common operator commands:

```bash
uv run pmbot status
uv run pmbot positions
uv run pmbot dashboard
uv run pmbot journal-stats
uv run pmbot tune-strategy
```

Canonical live launcher:

```bash
bash scripts/run_live_70.sh
```

## Trading rules

- Do not add random trade selection beyond the bounded `noise_fallback`.
- Any new live strategy must define explicit entry criteria, spread filters, sizing caps, and duplicate-position checks.
- Current live strategy is the grinder: heavy-favorite scalp, bid ∈ [0.88, 0.95], ≤4h to close, TP +7%, resolved_exit at bid ≥0.97, SL −15%.
- Sizing is 50% of available balance per trade, up to 2 simultaneous positions.
- Sync live Polymarket positions and live USDC balance into the local ledger every tick.
- The sell strategy runs before new entries.

## Money-making thesis

- A single profitable wallet buying can be noise; multiple profitable wallets on the same token in a short window is a stronger signal.
- A good signal can still be a bad trade if the spread is wide, the ask is at an extreme, or the chase premium is excessive.
- Grinder thesis: heavy-favorite markets within 4h of resolution are pricing near-certainty — pay the spread, take +7%, rotate.
- Risk control matters: size by bankroll fraction, cap per-trade, exit on resolved or SL.
- Skipping is a valid action when no eligible market qualifies.

## Code style

- Keep edits small, aligned with the standard-library-first style of the existing code.
- Use the `Settings` dataclass for new environment variables.
- Persist trade-visible metadata (strategy, exit reason, realized PnL) in the trade journal.
- Add focused unit tests for any change to filters, sizing, or exit logic.
