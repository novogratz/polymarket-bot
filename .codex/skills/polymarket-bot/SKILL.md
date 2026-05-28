---
name: polymarket-bot
description: Codex skill for the Polymarket smart-money copy-trading bot. Use for any change to strategy, filters, sizing, exits, journal, or the auto-tuner.
---

# Polymarket Bot Skill

## Current state (2026-05-28)

- **Live strategy:** `grinder` — race mode, heavy-favorite near-resolution scalp.
- **Config:** `configs/profiles/grinder.toml` (single source of truth).
- **Launcher:** `bash scripts/run_live_70.sh` — preserves ledger/journal. Do NOT use `run_all.sh` for live (it resets the ledger).
- **Bankroll:** $43 USDC. **Sizing:** 50%/trade, `max_orders_per_tick=2` (up to 2 simultaneous positions).
- **Entry:** bid ∈ [0.88, 0.95], ≤4h to close, spread ≤2¢, liq ≥$500, vol ≥$300.
- **Exits:** TP +7%, SL −15% (after 1 min), resolved_exit at bid ≥0.97, max-hold 4.5h.
- **W/L record:** `data/realized_trade_cache.jsonl` (survives `reset-ledger` journal rotation).
- **Analysts:** all deterministic — no AI, no LLM, no Codex anywhere.

## Guardrails

- No `.env` values, private keys, or passphrases in output or commits.
- Live trading requires `--live` flag on `pmbot auto-loop`; `--yes` is for script automation only.
- No LLM call in the scanning or trade-selection path.
- No random trade entry beyond bounded `noise_fallback` (disabled on grinder).
- Never delete `data/paper_state.json`, `data/trade_journal.jsonl`, or `data/realized_trade_cache.jsonl` unless the user explicitly asks for a reset.
- The bot must not gain the capability to commit or push source code.

## Commands

```bash
python3 -B -m unittest discover -s tests
uv run pmbot status
uv run pmbot positions
uv run pmbot journal-stats
bash scripts/run_live_70.sh
```

## Key files

- `polymarket_bot/race_strategies.py` — grinder entry/exit engine (`select_grinder`, `_build_eligible_candidates`, `_check_race_exits`).
- `polymarket_bot/main.py` — tick orchestration, sizing, journal.
- `polymarket_bot/config.py` — all `Settings` fields and env-var names.
- `scripts/run_live_70.sh` — canonical live launcher (update when config changes).

## Editing workflow

1. Read `race_strategies.py` + `main.py` for the grinder path.
2. Strategy/filter changes go in `configs/profiles/grinder.toml`.
3. Update tests if behavior changes (`tests/test_strategy.py`).
4. Update `CHANGELOG.md`, `README.md`, `CODEX.md`, and this SKILL.md when user-visible.
