# Codex Guide

Codex entry point for the Polymarket bot. See also the structured skill in `.codex/skills/polymarket-bot/SKILL.md`. The Claude Code version lives in `CLAUDE.md` and `.claude/skills/polymarket-bot/SKILL.md`.

The project is MIT licensed (see `LICENSE`). Tests run in CI (GitHub Actions, see `.github/workflows/test.yml`).

## Safety

- Never reveal `.env` values, private keys, API secrets, or passphrases.
- Live trading requires the `--live` flag on `pmbot auto-loop`. The `--yes` flag is for script automation only.
- Do not implement random or unfiltered live trades. The `noise_fallback` path is the only forced-trade lane and is hard-capped at $10 per trade, 4 trades per tick (disabled on the grinder profile).
- Preserve `data/paper_state.json`, `data/trade_journal.jsonl`, `data/realized_trade_cache.jsonl`, and `data/strategy_overrides.json` unless explicitly asked to reset them.
- No LLM call (Codex, Claude, anything else) in the scanning or trade-selection path.
- The bot must not have the capability to write or push source code on its own.

## Current state (2026-05-28)

**Live strategy:** `grinder` — race mode, heavy-favorite near-resolution scalp.
**Config:** `configs/profiles/grinder.toml` (single source of truth).
**Launcher:** `bash scripts/run_live_70.sh`. Do NOT use `run_all.sh` for live (it resets the ledger).
**Entry:** bid ∈ [0.88, 0.95], ≤4h to close, spread ≤2¢, liq ≥$500, vol ≥$300.
**Exits:** TP +7%, SL −15% (after 1 min), resolved_exit at bid ≥0.97, max-hold 4.5h.
**Sizing:** 50%/trade, `max_orders_per_tick=2`.

## Project map

- `polymarket_bot/main.py` — CLI commands and tick orchestration. Sizing helpers, trade-journal writer.
- `polymarket_bot/race_strategies.py` — grinder entry/exit engine.
- `polymarket_bot/smart_money.py` — leaderboards, parallel trade fetching, token grouping, scoring.
- `polymarket_bot/auto_tuner.py` — bounded overrides from the trade journal (defensive only).
- `polymarket_bot/trading.py` — live BUY/SELL order placement and stake computation.
- `polymarket_bot/portfolio.py` — local ledger, positions, pending orders, exits.
- `polymarket_bot/gamma.py` — Gamma client + reverse-lookup by clob_token_ids.
- `scripts/run_live_70.sh` — canonical live launcher.
- `tests/test_strategy.py` — 52+ tests.

## Commands

```bash
python3 -B -m unittest discover -s tests
uv run pmbot status
uv run pmbot positions
uv run pmbot journal-stats
bash scripts/run_live_70.sh
```

## Winning thesis

A market at bid 0.88–0.95 within 4 hours of resolution is pricing near-certainty. The bot pays the spread, targets +7%, and rotates. All logic is deterministic Python — no LLM anywhere.
