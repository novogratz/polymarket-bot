# polymarket-bot

[![tests](https://github.com/novogratz/polymarket-bot/actions/workflows/test.yml/badge.svg)](https://github.com/novogratz/polymarket-bot/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

Automated trading bot for [Polymarket](https://polymarket.com) binary prediction markets. Buys heavily-favored outcomes (ask 0.85–0.97) within ~6 hours of resolution and holds until the market prints near-final value (bid ≥ 0.99), with a controlled −25% confirmed stop-loss and a hard "never sell below entry" floor. Crypto and esports markets are excluded. Runs as up to 3 independent bots. Ships an opt-in autonomous self-improvement loop that tunes the strategy's exit/sizing knobs via auto-merged pull requests (entry selection stays frozen).

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

Starts the live grinder (**10 s tick**) alongside a dry paper twin and a read-only **live analyst** sidecar that posts a Telegram **LIVE REPORT** — on startup, then on a fixed cadence (`LIVE_ANALYST_CYCLE_SECONDS`), plus a daily 10:00 ET fire. The report shows **equity, P&L since start, total trades + win rate, and open positions** — nothing else (no per-trade lists, no heartbeat, no BUY/SELL spam). The live trade loop is **fully deterministic — no LLM in the scanning or trade-selection path**.

Three live bots run independently (Grinder Bot 1/2/3), each with its own wallet, ledger, and per-bot analyst (`run_live_70.sh` = bot 1, `run_live_b.sh` = bots 2 & 3, `run_live_win.sh` on the `kzer_windows` branch). Each keeps a per-machine baseline (`data/starting_cash.txt`); reset any bot with `scripts/fresh_start.py` (wipes closed-trade history, keeps open trades).

> **Do not use `run_all.sh` for live trading.** It resets the ledger on startup and launches a retired 95-profile dry race.

---

## Strategy

**Thesis.** A binary market at ask 0.85–0.97 within ~6 hours of close is pricing near-certainty. The bot pays the spread and holds until the market resolves, capturing the final leg of the implied-probability move to 1.0.

### Entry filters

| Parameter | Value (`grinder.toml [race]`) |
|---|---|
| Price band (ask) | 0.85 – 0.97 |
| Time to close | ≤ 6 hours |
| Max spread | ≤ 4¢ |
| Min liquidity | $500 |
| Min 24 h volume | $300 |
| Max one-day price change | 10 % |
| Min outcome momentum | −5 % |

> Exact values live in `configs/profiles/grinder.toml` and are the single source of truth — the table reflects the current live config but may be tuned over time (see [Self-improvement](#self-improvement)).

The **price-stability gate** (max day change 10 %) blocks markets that moved significantly today — a live-game "No" sitting at 0.93 can collapse to 0.40 in a single tick when a goal is scored. The **momentum filter** (min −5 %) additionally skips outcomes that are actively falling today: a market at 0.91 that was at 0.96 this morning is trending *away* from resolution, not toward it.

### Excluded market types

These categories are blocked globally because a stop-loss cannot protect against gap moves:

| Pattern | Reason |
|---|---|
| `exact score` | Soccer exact-score collapses instantly on a goal |
| `o/u 0.5 / 1.5 / 2.5 / 3.5` | Low-line totals — any goal flips them |
| `o/u 5.5 / 6.5 / 7.5` | High-line soccer, catastrophic if 6+ goals scored |
| `spread:` (Asian handicap) | Same gap risk as exact-score |
| draw markets (`end in a draw`, `win or draw`) | Coin-flip-like, no real favorite edge |
| halftime leading / score | Resolves mid-game, gap risk |
| `temperature`, `°c`, `°f` | Specific-degree weather, near-zero win rate in band |
| **All crypto** — `bitcoin`/`btc`/`ethereum`/`solana`/`dogecoin`/`xrp`/… + Up/Down | Banned: volatile, no edge for this strategy |
| **Esports** — `counter-strike`/`valorant`/`league of legends`/`dota`/… + `(bo3)`/`(bo5)` | Banned: in-series swings are uncatchable |

### Sizing

**Percentage of equity per trade** (`position_pct`), capped by `max_position_ceiling_pct` — no fixed dollar cap, so the stake scales automatically with the bankroll. Up to `max_orders_per_tick` new entries per tick, with a cash floor kept in reserve.

### Exits

| Condition | Code |
|---|---|
| Bid ≥ 0.99 | `race_big_win_resolved` — primary win exit (`resolved_exit_threshold`; raised from 0.97 on 2026-06-10, fallback 0.98) |
| Down ≥ 25 % from entry, confirmed 3 consecutive ticks | `race_stop_loss_confirmed` — the one path allowed to sell below entry |
| Genuinely-resolved loser ~8 h past expiry | written off locally (no order; settles on-chain) |

**Never sell below entry** is a hard floor in `execute_live_sell` — the *only* exception is the **controlled stop-loss**, which fires at −25% only after the loss persists for 3 consecutive ticks (so a one-tick thin-book phantom bid can't dump a winner). There is no take-profit ladder, no EOD flatten, and no loss-sweep (the universal sweep realizes **winners** ≥ 0.99 only). The expiry path never force-closes a market that is still accepting orders (it uses a live lookup + `gameStartTime`, since Gamma `endDate` is often set before kickoff). The **daily drawdown halt is disabled** — the per-trade confirmed SL is the risk control.

### Self-improvement

An **opt-in** autonomous loop (`scripts/auto_improve.py` + `.github/workflows/auto-improve.yml`) lets the bot tune its own strategy and ship the changes as auto-merged pull requests, driven by the Claude Code CLI. It is fenced so it can never harm the win rate:

- **Entry/bet-selection is frozen** — the agent can only change *exit/sizing* knobs (`tp_pct`, `stake_pct`, `max_orders_per_tick`, `resolved_exit_threshold`, `max_hold_hours`), each hard-clamped. An audit aborts the run if any entry filter moves.
- **A stop-loss can never be introduced.**
- It edits only `grinder.toml`, never other profiles, `.env`, or source code.
- A PR opens only after the unit-test suite passes, and auto-merges only when CI is green.

Off by default (`AUTO_IMPROVE_ENABLED=0`). Full design, switches, and setup in [`docs/AUTONOMY.md`](docs/AUTONOMY.md).

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
POLYMARKET_AUTO_INTERVAL_SECONDS=10      # tick interval (set by the launcher)
POLYMARKET_RACE_DAILY_DRAWDOWN_PCT=0     # daily DD halt — 0 = disabled
LIVE_ANALYST_CYCLE_SECONDS=1800          # LIVE REPORT cadence (fires on startup + this interval + daily 10:00 ET)
```

**Autonomous self-improvement (opt-in — see [`docs/AUTONOMY.md`](docs/AUTONOMY.md)):**

```bash
AUTO_IMPROVE_ENABLED=0     # master gate; nothing runs unless 1
AUTO_IMPROVE_USE_LLM=1     # propose changes via the Claude Code CLI
AUTO_IMPROVE_AUTOMERGE=1   # auto-merge the PR once CI is green
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
  run_live_70.sh       canonical live launcher (Bot 1)
  run_live_b.sh        Bot 2 launcher (grinder_b)
  live_analyst.py      30 min Telegram LIVE REPORT sidecar (read-only)
  dry_analyst.py       15 min deterministic report + loser-kill
  auto_improve.py      opt-in self-improvement loop (auto-PR, off by default)
docs/
  PROFILES.md          TOML key reference
  STRATEGIES.md        entry lanes and exit conditions
  AUTONOMY.md          self-improvement engine design + switches
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
