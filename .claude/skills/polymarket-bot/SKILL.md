---
name: polymarket-bot
description: Claude Code skill for the Polymarket smart-money copy-trading bot. Use for any change to strategy, filters, sizing, exits, journal, or the auto-tuner.
---

# Polymarket Bot Skill

Use this skill when working in this repository: strategy code, filters, live commands, dashboard, trade journal, auto-tuner, BTC edge.

## Guardrails (non-negotiable)

- Never print or commit `.env` values, private keys, API secrets, or passphrases.
- Live trading stays gated by `POLYMARKET_ENABLE_LIVE_TRADING=1`. The only sanctioned bypass is `POLYMARKET_DRY_RUN=1`, which short-circuits SDK BUY/SELL calls and writes to `data/dry_run_state.json` + `data/dry_run_journal.jsonl`.
- No random trade entry beyond the bounded `noise_fallback` ($10/trade, 4/tick).
- Any new live strategy must define explicit entry criteria, spread filters, sizing caps, and duplicate-position checks.
- Update tests when strategy behavior changes.
- No LLM call (Claude, Codex, anything else) in the scanning or trade-selection path. The scanner stays deterministic Python over Polymarket APIs.
- The bot must not have the capability to write or push source code on its own.

## Useful commands

```bash
uv run python -B -m unittest discover -s tests
uv run pmbot --version
uv run pmbot status                                  # snapshot rapide (mode, équité, positions, journal)
uv run pmbot positions                               # table CLI des positions ouvertes, triées par PnL desc
uv run pmbot dashboard
uv run pmbot doctor
uv run pmbot journal-stats
uv run pmbot tune-strategy
uv run pmbot auto-loop --profile copy-advanced --live --yes
POLYMARKET_PAPER_BALANCE_USD=100 uv run pmbot auto-loop --profile copy-advanced --dry-run --run mirror-whales
```

Canonical live config: `uv run pmbot auto-loop --profile copy-advanced --live --yes`.

CLI surface: 9 Typer commands (`auto-loop`, `dashboard`, `doctor`, `status`, `positions`, `journal-stats`, `tune-strategy`, `bootstrap-creds`, `reset-ledger`) plus the global `--version` / `-V` option. ANSI colors auto-disable when stdout is not a TTY (or when `NO_COLOR=1`); set `POLYMARKET_FORCE_COLOR=1` to keep them through pipes.

## Architecture

- `polymarket_bot/main.py` — tick orchestration, sizing, journal, CLI.
- `polymarket_bot/mirror.py` — mirror-mode strategy: whale discovery, trade polling, eligibility filters, conviction sizing, buy/sell execution, daily/weekly drawdown limits, stale-order cancellation.
- `polymarket_bot/smart_money.py` — legacy smart-money strategy: leaderboards, parallel trade fetching, token grouping, scoring.
- `polymarket_bot/dry_run_runs.py` — named dry-run lifecycle (`--run <name>`): isolated ledger, journal, mirror state, tick history.
- `polymarket_bot/auto_tuner.py` — bounded overrides from the trade journal (defensive only, gated on 30 trades).
- `polymarket_bot/bitcoin.py` — BTC threshold edge with retry + Coinbase v2 fallback.
- `polymarket_bot/trading.py` — live BUY/SELL order placement, FOK market orders, GTC limit sells, SDK v2 wrapper with `cancel_active_orders_for_token` (falls back from SDK `get_open_orders` to legacy REST).
- `polymarket_bot/portfolio.py` — local ledger + exit history.
- `polymarket_bot/gamma.py` — Gamma client + reverse-lookup by clob_token_ids.
- `polymarket_bot/strategy.py` — candidate ranking.
- `polymarket_bot/profiles.py` — TOML profile loader and env-var injection.

## Default strategy (mirror mode)

Whale copy-trading:

1. Load static target wallets + discover new whales from weekly leaderboards (8 categories, 25 per page, min PnL $10k).
2. Poll BUY trades for every target in parallel, filter by age, already-seen, min stake ($100), buy price band (0.02–0.98).
3. Sort eligible trades chronologically so SELLs before BUY on the same token get the right order.
4. _(live only)_ Sync live Polymarket positions + on-chain pUSD cash balance before exits.
5. Run exits: take-profit ladder (+25/+50/+100/+200/+300%), trailing stop, peak-protect, stop-loss (-40%), cohort-sell, near-expiry, max-hold (24h).
6. For each eligible BUY: chase premium ≤ 5%, liquidity ≥ $10k, per-category cap (50%), max 12 open positions, no duplicate event/position.
7. Conviction-weighted sizing: `whale_stake × copy_ratio (0.20, tiered up to 0.35)`, capped by `equity × 25%`, capped by `$250`, capped by cash.
8. FOK market orders for BUYs. GTC limit orders for SELLs — recorded as exits immediately (status "live"/"delayed" = on the book).
9. Stale GTC orders auto-cancelled via SDK v2 `get_open_orders` on "not enough balance" errors.
10. Mirror state isolated per `--run` name, including day/week equity anchors for drawdown limits.

## Defensive auto-tuner

Reads `data/trade_journal.jsonl` each tick. Active from 30 closed trades. Bounded rules:

- Stop-loss > 40% of trades: tighten `MAX_CHASE_PREMIUM` ×0.80, `MAX_RELATIVE_SPREAD` ×0.85.
- Consensus=2 trades avg PnL < -$0.30 (≥20 sample): raise `MIN_CONSENSUS` to 3.
- Sports avg PnL < -$0.30 (≥15 sample): bump `SPORTS_SCORE_PENALTY` ×1.5.
- Win rate < 30%: raise `MIN_COPIED_USDC` ×1.5.
- Avg PnL < -$0.20: reduce `POSITION_PCT` ×0.75.

Defensive only: tightens after losses, never loosens after wins. Overrides written to `data/strategy_overrides.json` (auditable).

## Logic

- One wallet alone = noise.
- Several profitable wallets buying the same token in a short window = stronger collective signal.
- A good signal can still be a bad trade if execution is poor (spread, chase, fill).
- No-signal / no-trade is a valid decision.

## Editing workflow

1. Read the relevant code (`mirror.py`, `main.py`, `trading.py`).
2. Modify while preserving the hierarchy above.
3. Update tests in `tests/test_mirror.py`.
4. Run `uv run python -B -m unittest discover -s tests`.
5. Update `CHANGELOG.md`, `README.md`, `CLAUDE.md`, `CODEX.md`, and the SKILL files when user-visible.
6. Commit and push.
