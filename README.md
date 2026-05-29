# polymarket-bot

[![tests](https://github.com/novogratz/polymarket-bot/actions/workflows/test.yml/badge.svg)](https://github.com/novogratz/polymarket-bot/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

Automated trading bot for [Polymarket](https://polymarket.com) binary prediction markets. Buys heavily-favored outcomes (bid 0.89–0.94) within 4 hours of resolution, holds until the market prints near-final value (bid ≥ 0.99), and rotates capital into the next opportunity.

> **Financial disclaimer.** This software places real-money trades. It is not financial advice. Losses are possible. You are solely responsible for all trading decisions. Use only capital you can afford to lose entirely. See the [full disclaimer](#disclaimer).

---

## Install

```bash
pip install -e .       # or: uv sync
cp .env.example .env   # add wallet + CLOB credentials
```

Dev tools (linter):

```bash
pip install -e ".[dev]"
```

## Run

```bash
bash scripts/run_live_70.sh
```

Starts the live grinder (30 s tick) alongside a dry paper twin (10 min tick), a live analyst sidecar (Telegram summary every 30 min), and an autonomous dry analyst (15 min report). All logic is deterministic — no AI anywhere.

> **Do not use `run_all.sh` for live trading.** It resets the ledger on startup and launches a retired 95-profile dry race.

---

## Strategy

**Thesis.** A binary market at bid 0.89–0.94 within 4 hours of close is pricing near-certainty. The bot pays the spread and holds until the market resolves, capturing the final leg of the implied-probability move to 1.0.

### Entry filters

| Parameter | Value |
|---|---|
| Bid band | 0.89 – 0.94 |
| Time to close | ≤ 4 hours |
| Max spread | 2¢ |
| Min liquidity | $500 |
| Min 24 h volume | $300 |
| Max one-day price change | 10 % |

The **price-stability gate** (max day change 10 %) blocks markets that moved significantly today — a live-game "No" sitting at 0.93 can collapse to 0.40 in a single 30 s tick when a goal is scored.

### Excluded market types

These categories are blocked globally because a stop-loss cannot protect against gap moves:

| Pattern | Reason |
|---|---|
| `exact score` | Soccer exact-score collapses instantly on a goal |
| `o/u 0.5` | Any-goal binary — same gap risk |
| `o/u 5.5 / 6.5 / 7.5` | High-line soccer, catastrophic if 6+ goals scored |
| `temperature`, `°c`, `°f` | Specific-degree weather, near-zero win rate in band |
| `up or down`, slug `updown` | Crypto Up/Down binaries, no real book depth |

### Sizing

**40 % of balance per trade, up to 2 concurrent positions.**

One bad trade costs 40 % of the account — painful but survivable. At 95 % stake a single loss would wipe the account.

**Realistic performance expectations at $123:**

| Day type | Frequency | Result |
|---|---|---|
| Active — 3 qualifying trades, all win | ~30 % of days | +8–11 % |
| Normal — 1–2 trades | ~50 % of days | +2–6 % |
| Quiet — 0 qualifying markets | ~15 % of days | 0 % |
| Bad — 1 loss | ~5 % of days | −8–16 % |

**Expected average: 2–4 % per day** when the market cooperates. 10 % days happen but are not the norm — they require 3 qualifying markets all winning on the same day. A realistic weekly target is 15–25 %. That compounds to exceptional annual returns without requiring the impossible.

### Exits

| Condition | Code |
|---|---|
| Bid ≥ 0.99 | `race_big_win_resolved` — primary exit |
| Hold ≥ 4.5 h | `race_expired_close` — backstop |
| Bid ≤ 0.03 (universal sweep) | `resolved_market_sweep_loss` |

No take-profit ladder. No stop-loss. The exclusion filters and price-stability gate are the primary risk controls.

---

## CLI

```bash
uv run pmbot status          # equity, open positions, journal count
uv run pmbot positions        # open positions sorted by PnL
uv run pmbot dashboard        # live dashboard at http://127.0.0.1:8765
uv run pmbot journal-stats    # P&L breakdown by exit reason
uv run pmbot doctor           # health check (env, auth, endpoints)
uv run pmbot tune-strategy    # run auto-tuner manually
uv run pmbot reset-ledger     # rebuild ledger from live CLOB state
```

`status` and `positions` are read-only — no network calls. Output is colorized on a TTY; disable with `NO_COLOR=1`.

---

## Configuration

`configs/profiles/grinder.toml` is the single source of truth. Environment variables override profile values at runtime — see `polymarket_bot/config.py` for the full list.

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
POLYMARKET_SYNC_LIVE_POSITIONS=1         # sync live CLOB positions each tick
POLYMARKET_AUTO_INTERVAL_SECONDS=30      # tick interval (set by run_live_70.sh)
POLYMARKET_RACE_DAILY_DRAWDOWN_PCT=0.15  # daily DD halt threshold
POLYMARKET_HTTP_CACHE_TTL_SECONDS=600    # shared HTTP cache TTL
```

---

## Development

```bash
# Tests
uv run python -B -m unittest discover -s tests

# Lint
ruff check polymarket_bot tests

# Dry-run (no real orders, separate ledger under data/dry_runs/grinder/)
uv run pmbot auto-loop --dry-run --profile grinder
```

CI runs tests + lint on Python 3.11 / 3.12 for every push.

---

## Project structure

```
configs/profiles/grinder.toml       live strategy — single source of truth
data/paper_state.json               live ledger (positions, cash)
data/realized_trade_cache.jsonl     durable W/L record (survives resets)
data/trade_journal.jsonl            per-trade metadata + exit reasons
polymarket_bot/
  main.py              CLI, tick orchestration, sizing, journal writer
  race_strategies.py   grinder entry/exit engine + arb scanner
  portfolio.py         local ledger and position accounting
  trading.py           live CLOB order placement
  gamma.py             Gamma market scan
  models.py            shared dataclasses, exclusion filters
  smart_money.py       leaderboard fetch and signal scoring
scripts/
  run_live_70.sh       canonical live launcher
  live_analyst.py      30 min Telegram sidecar (read-only)
  dry_analyst.py       15 min deterministic report + loser-kill
docs/
  PROFILES.md          TOML key reference
  STRATEGIES.md        entry lanes and exit conditions
```

---

## Contributing and security

- Contributions: see [`CONTRIBUTING.md`](CONTRIBUTING.md).
- Security disclosures: see [`SECURITY.md`](SECURITY.md).
- Agent entry points: [`CLAUDE.md`](CLAUDE.md), [`CODEX.md`](CODEX.md), [`AGENTS.md`](AGENTS.md).

## License

MIT — see [`LICENSE`](LICENSE).

---

## Disclaimer

**This software places real-money trades when configured to do so.**

- It is not financial advice.
- There is no guarantee of profit. Losses are possible and likely over some time horizons.
- You are solely responsible for all trading decisions and committed funds.
- You are responsible for complying with applicable laws and Polymarket's terms of service in your jurisdiction.
- The author and contributors disclaim all liability for losses or damages arising from use of this software.

Exercise the bot in dry-run mode first. Limit the initial bankroll to an amount you can afford to lose entirely.
