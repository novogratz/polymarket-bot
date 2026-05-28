# polymarket-bot

[![tests](https://github.com/novogratz/polymarket-bot/actions/workflows/test.yml/badge.svg)](https://github.com/novogratz/polymarket-bot/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

Polymarket grinder bot: buys heavily-favored binary outcomes (bid 0.88–0.95) within 4 hours of resolution, exits at +7% or when the market trends toward resolution at bid ≥ 0.97, and immediately rotates capital into the next opportunity. Includes a persistent ledger, trade journal, local dashboard, deterministic analyst sidecars, and Telegram reporting.

> **Financial disclaimer.** This software places real-money trades when configured to do so. It is not financial advice. There is no guarantee of profit — losses are possible. You are solely responsible for all trading decisions and committed funds. See the [full disclaimer](#disclaimer) and use only what you can afford to lose entirely.

## Install

```bash
pip install -e .          # or: uv sync
cp .env.example .env      # fill in wallet and CLOB API credentials
```

With dev tools (ruff):

```bash
pip install -e ".[dev]"
```

## Run

```bash
bash scripts/run_live_70.sh
```

Boots the live grinder (30s tick) + live analyst sidecar (30min Telegram report) + live-only leaderboard (5min summary) + a dry grinder twin (paper, 10min tick) + the autonomous dry analyst (15min report). All deterministic — no AI anywhere.

> **Do not use `run_all.sh` for live trading.** It runs `pmbot reset-ledger` on startup (wipes `paper_state.json` and rotates the journal) and launches the retired 95-profile dry race.

## CLI

```bash
uv run pmbot --version
uv run pmbot status              # mode, equity, open positions, journal count
uv run pmbot positions           # open positions table, sorted by PnL desc
uv run pmbot dashboard           # local dashboard at http://127.0.0.1:8765
uv run pmbot doctor              # health check (.env, auth, endpoints, local state)
uv run pmbot journal-stats       # P&L breakdown by exit reason, category, entry price
uv run pmbot tune-strategy       # run the auto-tuner manually
uv run pmbot reset-ledger        # rebuild ledger from live state (use with caution)
```

`status` and `positions` are read-only — no network calls. Output is colorized on a TTY; `NO_COLOR=1` disables ANSI codes, `POLYMARKET_FORCE_COLOR=1` forces them through pipes.

## Strategy

**Thesis.** A market at bid 0.88–0.95 within 4 hours of close is pricing near-certainty. The bot pays the spread, targets +7%, and rotates. The edge is the implied-probability gap between the current bid and the binary outcome resolving at 1.0.

**Entry filters** (`configs/profiles/grinder.toml`):

| Parameter | Value |
|---|---|
| Price band | bid ∈ [0.88, 0.95] |
| Time to close | ≤ 4 hours |
| Max spread | 2¢ |
| Min liquidity | $500 |
| Min 24h volume | $300 |

**Sizing.** 50% of available balance per trade, up to 2 simultaneous positions (`max_orders_per_tick=2`). Bet size scales automatically with the bankroll — no config edit required after a top-up. The $1 CLOB minimum gates the entry floor.

**Exits:**

| Condition | Reason code |
|---|---|
| Price ≥ entry × 1.07 | `race_take_profit` (+7%) |
| Bid ≥ 0.97 | `race_big_win_resolved` (trending to resolution) |
| Price ≤ entry × 0.85 after 1 min | `race_stop_loss` (−15%) |
| Hold ≥ 4.5h | `race_expired_close` (max-hold backstop) |
| Near expiry (5 min) + losing | `race_expired_close` (loser flush) |

**Selector ranking.** `score = best_bid / max(hours_to_close, 1/60)` — highest score = closest to resolution and closest to certainty. The top-ranked eligible market gets the trade.

**Daily DD halt** at −15% of starting equity. Override via `POLYMARKET_RACE_DAILY_DRAWDOWN_PCT`.

## Development

```bash
# Run tests
uv run python -B -m unittest discover -s tests

# Lint
ruff check polymarket_bot tests

# Dry-run simulation (no real money, separate ledger at data/dry_runs/grinder/)
uv run pmbot auto-loop --dry-run --profile grinder
```

CI runs tests + lint on Python 3.11 / 3.12 for every push (see `.github/workflows/test.yml`).

## Configuration

`configs/profiles/grinder.toml` is the single source of truth for live strategy. Environment variables override profile values at runtime — see `polymarket_bot/config.py` for the full list.

**Required credentials (live trading only):**

```bash
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_FUNDER_ADDRESS=0x...
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...
```

**Key runtime vars:**

```bash
POLYMARKET_SYNC_LIVE_POSITIONS=1         # sync live positions every tick
POLYMARKET_AUTO_INTERVAL_SECONDS=30      # tick interval (set by run_live_70.sh)
POLYMARKET_RACE_DAILY_DRAWDOWN_PCT=0.15  # DD halt threshold (default 15%)
POLYMARKET_HTTP_CACHE_TTL_SECONDS=600    # shared HTTP cache TTL
```

## Dashboard

```bash
uv run pmbot dashboard
# → http://127.0.0.1:8765
```

Refreshes every 5 seconds: equity curve, open positions, recent closed trades, scanner candidates, last-tick rejection reasons.

## Trade journal

Every closed position writes a JSON line to `data/trade_journal.jsonl` with full entry metadata, exit reason, and realized PnL. The durable W/L record lives in `data/realized_trade_cache.jsonl` and survives `reset-ledger` rotations.

```bash
uv run pmbot journal-stats    # win rate, P&L by bucket, suggested tightenings
```

## Project structure

```
configs/profiles/grinder.toml     live strategy config (single source of truth)
data/paper_state.json             live ledger (positions, cash, equity)
data/realized_trade_cache.jsonl   durable W/L record
data/trade_journal.jsonl          per-trade metadata + exit reasons
polymarket_bot/
  main.py          CLI, tick orchestration, sizing, journal
  race_strategies.py  grinder entry/exit logic
  smart_money.py   leaderboard fetch, signal scoring, reverse-lookup
  trading.py       live CLOB order placement
  portfolio.py     local ledger
  gamma.py         Gamma market scan + reverse-lookup
scripts/
  run_live_70.sh   canonical live launcher
  live_analyst.py  read-only 30min Telegram sidecar
  dry_analyst.py   autonomous 15min report + loser-kill pass
  cache_warmer.py  HTTP cache pre-warm
```

## Contributing and security

- Contributions: see `CONTRIBUTING.md`.
- Security disclosures: see `SECURITY.md`.
- Agent entry points: `CLAUDE.md` (Claude Code), `CODEX.md` (Codex), `AGENTS.md` (generic).

## License

MIT. See `LICENSE`.

## Disclaimer

**This software places real-money trades when configured to do so.**

- It is not financial advice.
- There is no guarantee of profit. Losses are possible and likely over some time horizons.
- You are solely responsible for all trading decisions and committed funds.
- You are responsible for complying with applicable laws and Polymarket's terms of service in your jurisdiction.
- The author and contributors disclaim all liability for losses or damages arising from the use of this software.

Before the first live run, exercise the bot in dry-run mode, verify the filters, and limit the initial bankroll to an amount you can afford to lose entirely.
