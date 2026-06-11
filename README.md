# polymarket-bot

[![tests](https://github.com/novogratz/polymarket-bot/actions/workflows/test.yml/badge.svg)](https://github.com/novogratz/polymarket-bot/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

Automated trading bot for [Polymarket](https://polymarket.com) binary prediction markets. Buys heavily-favored outcomes (ask 0.85–0.97) close to resolution (4 h window, widening to 12 h max when quiet) and holds until a real 0.99 bid exists on the live order book (otherwise rides to on-chain settlement at 1.00), with a controlled −25% confirmed stop-loss and a hard "never sell below entry" floor. Crypto and esports markets are excluded. Runs as up to 3 independent bots. Ships an opt-in autonomous self-improvement loop that tunes the strategy's exit/sizing knobs via auto-merged pull requests (entry selection stays frozen).

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

**Thesis.** A binary market at ask 0.85–0.97 within a few hours of close is pricing near-certainty. The bot pays the spread and holds until the market resolves, capturing the final leg of the implied-probability move to 1.0. Every tick (10 s) it runs the same deterministic pipeline: **scan → exclude → filter → rank → size → execute → manage exits**. No LLM is ever in this path.

### 1. Scan

Every Gamma market closing within the widest ladder rung (24 h cap / end of tomorrow), fetched with two orderings (soonest-closing + highest-volume) and **full pagination past the API's silent 100-row cap** — ~1,000–2,000+ raw markets per tick depending on time of day.

### 2. Excluded market types

Blocked globally (`models.is_excluded_market`) because no exit can protect against their gap moves:

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
| **Esports** — `counter-strike`/`valorant`/`league of legends`/`LoL:`/`dota`/… + `(bo1)`/`(bo3)`/`(bo5)` | **Live games only** (2026-06-12): tradeable while the game is in progress (`gameStartTime` past, ≤ 8 h); pre-game/unknown start banned |
| **Stock market / equities** — S&P/`SPY`/Nasdaq/`QQQ`/Dow/DJIA + big-cap tickers & companies (word-bounded) + `closes above/below $X` + generic `(TICKER) … $` | **Ongoing session only** (2026-06-12): tradeable Mon–Fri 09:30–16:00 ET for that day's close; overnight/weekend/multi-day banned; weekly "Week of" / `hit (LOW)/(HIGH)` touch markets banned outright |
| **Tweet-count markets** — `tweet` + `-tweets`/`of-tweets` slugs | Banned outright (2026-06-12): week-long counts, no convergence signal |

**Deliberately tradeable** (test-pinned, do not ban): elections, primaries, and mayoral races — a profitable lane despite occasional postponements — and **fast-moving markets**: there are no `oneDayPriceChange` or `oneHourPriceChange` gates (recently-moving markets are often the ones converging toward resolution; both values are logged for offline analysis only).

### 3. Entry filters

| Parameter | Value (`grinder.toml [race]`) |
|---|---|
| Price band (ask) | 0.85 – 0.97 |
| Time to close | **dynamic**: ≤ 4 h preferred; widens 4 → 6 → 8 → 10 → 12 → 24 h, then to end of tomorrow (UTC) for daily markets, only when nothing is actionable |
| Max spread | ≤ 4¢ |
| Min liquidity | $500 |
| Min 24 h volume | $300 |

> Exact values live in `configs/profiles/grinder.toml` and are the single source of truth — the table reflects the current live config but may be tuned over time (see [Self-improvement](#self-improvement)).

There are **no price-movement gates** (removed 2026-06-10): markets that moved today or in the last hour, and outcomes that are falling, all stay tradeable — fast movers are often exactly the ones converging toward resolution. `oneDayPriceChange`/`oneHourPriceChange` are logged in the forward-observation net for offline analysis only, and tests pin that neither value can ever exclude a market. The gap-risk protection comes from the category exclusions above, not from movement filters.

### 4. Ranking & pick slots

Survivors are ranked by `bid / hours_to_close` (confidence per remaining hour) and the top `max_orders_per_tick` (4) become this tick's picks. Markets that can never execute — token already held at its cap, order pending, or event already held — are removed **before** ranking, so they never burn pick slots. **One bet per game:** a game is identified by its date-truncated event slug and the team names in the question (one game spans several Polymarket events — moneyline, `-more-markets`, `-first-to-score`); same-game candidates collapse to a single pick before ranking, an open position on the game blocks all its other markets, and the execution loop backstops same-tick repeats. For soccer the **under-4.5-goals** market is preferred over everything else in the game.

### 5. Sizing (dynamic, 20 % hard cap)

- **Hard cap: 20 % of equity per bet** (`stake_pct = 0.20`) — never more on a single outcome, top-ups included.
- **Opportunity spread:** the per-bet target is `min(cap, available cash / N)` where N = actionable markets this tick. A busy evening with 20 qualifying markets gets ~5 % each so all can be funded; a quiet window gives each bet the full 20 %. Time-of-day adaptivity is emergent from N.
- **Near-resolution boost:** 1.5× under 30 min to close, 1.25× under 1 h — scales the spread share but can never pierce the 20 % cap.
- No fixed dollar caps — everything scales with the bankroll.

### 6. Execution

- FOK market BUY with a price guard of ask + 1 tick (≤ 0.99).
- **Stake capped to 90 % of the executable ask-side depth** within the guard, so big stakes fill what the book offers instead of being killed (`FOK orders are fully filled or killed`).
- The ledger books the **true fill** (`makingAmount`/`takingAmount`), not the request — entry price, share count, and cash are exact.
- **Top-ups:** a depth-capped entry keeps its market actionable; later ticks may buy more of the same token (re-passing every entry filter and the depth cap) until the position's total cost reaches the 20 % cap. One position per event otherwise.

### 7. Exits

| Condition | Code |
|---|---|
| **Live book** bid ≥ 0.99 | `race_big_win_resolved` — primary win exit (`resolved_exit_threshold`); the exit probes the live CLOB bid each tick (Gamma quotes lag). A 0.98 bid is NOT enough — the displayed "98¢" is usually the midpoint, and settlement pays 1.00, so the bot holds for a real 0.99 bid or resolution |
| Cached price ≥ 0.99 after the market left the scan | `resolved_market_sweep_win` — winners-only sweep, same threshold (it can never fire earlier than the race exit) |
| Down ≥ 25 % from entry, confirmed 3 consecutive ticks, **soccer moneylines only** ("Will <Team> win on <date>?") | `race_stop_loss_confirmed` — the one path allowed to sell below entry |
| Genuinely-resolved loser ~8 h past expiry | written off locally (no order; settles on-chain) |

**Never sell below entry** is a hard floor in `execute_live_sell` — the *only* exception is the **controlled stop-loss**, which fires at −25 % only after the loss persists for 3 consecutive ticks (so a one-tick thin-book phantom bid can't dump a winner) and only on soccer moneylines; O/U totals, elections, and everything else ride to on-chain resolution. There is no take-profit ladder, no EOD flatten, and no loss-sweep. The expiry path never force-closes a market that is still accepting orders (it uses a live lookup + `gameStartTime`, since Gamma `endDate` is often set before kickoff). The **daily drawdown halt is disabled** — the per-trade confirmed SL is the risk control.

### Self-improvement

An **opt-in** autonomous loop (`scripts/auto_improve.py` + `.github/workflows/auto-improve.yml`) lets the bot tune its own strategy and ship the changes as auto-merged pull requests, driven by the Claude Code CLI. It is fenced so it can never harm the win rate:

- **Entry/bet-selection is frozen** — the agent can only change *exit/sizing* knobs (`tp_pct`, `stake_pct`, `max_orders_per_tick`, `max_hold_hours`), each hard-clamped; `resolved_exit_threshold` is pinned at 0.99 (winners sell at a real 0.99 bid or settle at 1.00). An audit aborts the run if any entry filter moves.
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
