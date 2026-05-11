# Agent Instructions

This repository contains the Polymarket smart-money copy-trading bot. It includes a deterministic signal engine, a live order execution layer, a defensive auto-tuner, a persistent trade journal, and a read-only local dashboard. Treat `.env` and `data/` as local-only state. Never print private keys, API secrets, or wallet credentials.

For the Claude Code and Codex entry points, see `CLAUDE.md` and `CODEX.md`. The structured skill files live at `.claude/skills/polymarket-bot/SKILL.md` and `.codex/skills/polymarket-bot/SKILL.md`.

## Guardrails

- Never reveal `.env` values, private keys, API secrets, or passphrases.
- Live trading must remain gated by `POLYMARKET_ENABLE_LIVE_TRADING=1`.
- The trading scan path is deterministic Python over Polymarket APIs. **No LLM call** is permitted in scanning or trade selection.
- Random or unfiltered live trade entry is not accepted. The `noise_fallback` lane is the only forced-trade path and is hard-capped at $10 per trade and 4 trades per tick.
- Strategy adjustments at runtime are data files (`data/strategy_overrides.json`), never code rewrites. The bot must not gain the capability to commit or push source code.
- Every change to strategy behavior must be covered by a unit test in `tests/test_strategy.py`.

## Commands

Run tests before pushing:

```bash
python3 -B -m unittest discover -s tests
```

Common operator commands:

```bash
python3 -B -m polymarket_bot.main dashboard
python3 -B -m polymarket_bot.main journal-stats
python3 -B -m polymarket_bot.main tune-strategy
POLYMARKET_ENABLE_LIVE_TRADING=1 python3 -B -m polymarket_bot.main auto-loop
```

The CLI exposes six commands: `auto-loop`, `dashboard`, `journal-stats`, `tune-strategy`, `bootstrap-creds`, `reset-ledger`.

Canonical live config:

```bash
bash scripts/run_live_70.sh
```

The live config uses smart-money only (no noise fallback), percentage-based sizing (75% of available cash × conviction multiplier 0.55×–2.5×), tight trader quality filters ($2k PnL floor), 5 min signal freshness, 6% max spread, and aggressive deployment toward a 5% cash floor. The full parameter list is in `scripts/run_live_70.sh`.

A `Makefile` is also provided: `make test`, `make lint`, `make run`, `make dashboard`, `make journal`, `make tune`, `make clean`.

## Trading rules

- Do not add random trade selection beyond the bounded `noise_fallback`.
- Live trading must remain gated by `POLYMARKET_ENABLE_LIVE_TRADING=1`.
- Prefer strategies with explicit entry criteria, sizing limits, spread limits, freshness checks, and duplicate-position checks.
- The smart-money strategy copies recent BUY trades only when multiple profitable wallets bought the same token in the lookback window.
- Sizing is conviction-weighted (0.55x to 2.5x), with a per-position absolute ceiling and an equity-percentage ceiling that scales with the bankroll.
- The sell strategy runs before new entries: take-profit ladder, trailing stop, peak-protect, stop-loss, cohort-sell, near-expiry, max-hold-time.
- Sync live Polymarket positions and live USDC balance into the local ledger every tick.
- Treat crypto up/down micro markets more strictly than sports or longer-duration markets.
- The integrated BTC edge strategy is opt-in (`POLYMARKET_BTC_EDGE_INTEGRATED=1`) and capped per trade.

## Money-making thesis

The expected edge is following public order flow:

- A single profitable wallet buying can be noise.
- Multiple profitable wallets buying the same token in a short window is a stronger signal.
- A good signal can still be a bad trade if the spread is wide, the ask is at an extreme price, or the chase premium is excessive.
- Risk control matters: cap per trade, cap per event, cap per category, exit on flip signals, and refuse to trade when nothing qualifies.
- Skipping is a valid action when the setup is not clean.

## Code style

- Keep edits small and aligned with the standard-library-first style of the existing code.
- Use the `Settings` dataclass for new environment variables.
- Persist trade-visible metadata (signal score, consensus, copied USDC, exit reason, realized PnL) in the trade journal so `journal-stats` can report on it.
- Add focused unit tests for any change to filters, sizing, or exit logic.
