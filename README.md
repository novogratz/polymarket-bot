# polymarket-bot

[![tests](https://github.com/novogratz/polymarket-bot/actions/workflows/test.yml/badge.svg)](https://github.com/novogratz/polymarket-bot/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

A general-purpose, deterministic trading engine for [Polymarket](https://polymarket.com) binary prediction markets. The same scan → exclude → filter → rank → size → execute → manage-exits pipeline (`polymarket_bot/race_strategies.py`) can run several different strategies off a TOML profile, with **no LLM anywhere in the scan or trade-selection path**. The team's **current live focus is the weather strategy** (bots 2 & 3): buy temperature/degree-bracket markets gated by a multi-model Open-Meteo forecast consensus (see [Weather mode](#weather-mode--current-live-focus) below). Bot 1 still runs the engine's general-purpose configuration, **grinder mode**: buy heavily-favored outcomes (ask 0.80–0.94, hard cap 0.96) close to resolution (4 h window, hard maximum) across any market category at a **fixed $5 per trade** (v4, 2026-06-21), holding until a real 0.99 bid exists on the live order book (otherwise rides to on-chain settlement at 1.00), with a controlled −30% confirmed stop-loss (sport moneylines only) and a hard "never sell below entry" floor. With `unban_all_markets` every category is allowed and governed by a data-driven category auto-disable. A third lane, **smart-money copy-trading** (`polymarket_bot/smart_money.py`), also exists in the codebase but is not currently run live. Runs as up to 3 independent bots. Ships an opt-in autonomous self-improvement loop that tunes exit/sizing knobs via auto-merged pull requests (entry selection stays frozen).

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

Three live bots run independently, each with its own wallet, ledger, and per-bot analyst: bot 1 (`run_live_70.sh`) runs **grinder mode**, bots 2 & 3 (`run_live_b.sh`, plus `run_live_win.sh` on the `kzer_windows` branch) run **weather mode**. Each keeps a per-machine baseline (`data/starting_cash.txt`); reset any bot with `scripts/fresh_start.py` (wipes closed-trade history, keeps open trades).

> **Do not use `run_all.sh` for live trading.** It resets the ledger on startup and launches a retired 95-profile dry race.

---

## Strategy

The engine supports more than one strategy off the same pipeline; each live bot is just a TOML profile pointing the engine at a different candidate set and edge model. Two are live today: **weather mode** (bots 2 & 3, current focus) and **grinder mode** (bot 1, the general-purpose configuration, documented in detail below since it's still trading live capital). A third lane, smart-money copy-trading, exists in the codebase (`polymarket_bot/smart_money.py`) but isn't run live.

### Weather mode — current live focus

Weather mode is the same deterministic engine as grinder (same scan/rank/size/execute machinery, same $5 fixed stake, same 0.99 winner floor and never-sell-below-entry rule) but with the candidate set restricted to **temperature / degree-bracket markets** ("Will the high in `<city>` be `X`–`Y`°C/°F on `<date>`?") and an extra forecast-based edge model gating entry, implemented in `polymarket_bot/weather_forecast.py` and switched on per-profile via `race_weather_only` (`weather_only = true` in `configs/profiles/grinder_b.toml`, bots 2 & 3).

- **Forecast consensus.** For each candidate market, the bot fetches same-day/next-day forecasts from multiple free Open-Meteo models (GFS, ECMWF IFS, best-match/UK Met Office) in parallel. σ (uncertainty) is derived from the actual spread between the models rather than a fixed formula; models that disagree with each other by more than `MAX_SPREAD_C` (3.0 °C) are silently dropped, and the whole lookup is skipped (fail-open — normal price filters still apply) if fewer than `MIN_MODELS` (2) respond.
- **Edge gate (`race_weather_forecast_min_edge`).** The consensus forecast + σ are turned into a bracket-probability via a normal-CDF model, `model_P(outcome) − market_ask`. A trade is only taken when that edge is ≥ the configured minimum — bot 2 sets `weather_forecast_min_edge = 0.10`. Default is `0.0` (off) so a fresh bot doesn't starve on missing history — this gate needs no trade history, only live forecast data.
- **Bracket-margin guard (`race_weather_min_bracket_margin_c`).** Added after a real loss (Qingdao, 2026-06-28: ECMWF forecast 28.1 °C vs a 29 °C bracket threshold — a 0.9 °C margin — resolved as a loss). "No" bets are skipped outright when the model consensus sits within this many °C of the bracket's threshold, i.e. too close to call. Bot 2 sets `weather_min_bracket_margin_c = 2.0`; default `0.0` (off).
- **Intraday kill-switch.** For same-day, daily-max markets, once it's past solar 3 PM at the market's location the function checks the *already-observed* current temperature against the bracket: if the daily max physically can't reach the bracket (or has already blown past it), it returns a near-certain probability immediately instead of waiting for the day's max to be recorded.
- **Fail-open throughout.** Any API failure, parse failure, or missing history returns `None` and the normal price/liquidity filters apply — a forecast outage never blocks trading outright, it just removes the extra edge gate for that tick.
- Sizing and exits are unchanged from grinder mode (fixed $5 stake, 0.99 winner floor, never-sell-below-entry); bot 2's profile widens the entry window to 24 h (`max_hours = 24.0`, since weather markets resolve within a day) and uses weather-grade liquidity floors (`min_liquidity_usd = 50`, `min_volume_24h_usd = 200` — thinner books than sports).

### Grinder mode — general-purpose

**Thesis.** A binary market at ask 0.80–0.94 within a few hours of close is pricing near-certainty. The bot pays the spread and holds until the market resolves, capturing the final leg of the implied-probability move to 1.0. Every tick (10 s) it runs the same deterministic pipeline: **scan → exclude → filter → rank → size → execute → manage exits**. No LLM is ever in this path. This is the general-purpose configuration — with `unban_all_markets` it trades any category, not just weather — and it's still what bot 1 runs live.

### 1. Scan

Every Gamma market closing within the 4 h window (hard max), fetched with two orderings (soonest-closing + highest-volume) and **full pagination past the API's silent 100-row cap** — ~1,000–2,000+ raw markets per tick depending on time of day.

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
| **Esports** — `counter-strike`/`valorant`/`league of legends`/`LoL:`/`mobile legends`/`dota`/… + `(bo1)`/`(bo3)`/`(bo5)` | **Banned outright** (2026-06-19): every title incl. League of Legends excluded regardless of live status or ask — the prior LoL-only-while-live carve-out is removed |
| **All O/U goal totals** — `o/u 0.5`…`o/u 7.5` incl. **`o/u 4.5`** | Banned outright; 4.5 added 2026-06-14 (loss audit: O/U 4.5 Unders = 80% of all losses, 3 worst trades ever) |
| **All stock market / equities** — S&P/`SPY`/Nasdaq/`QQQ`/Dow/DJIA + big-cap tickers & companies (word-bounded) + `closes above/below $X` + generic `(TICKER) … $` | **Banned outright** (re-banned 2026-06-12 after a one-day in-session experiment) |
| **Tweet-count markets** — `tweet` + `-tweets`/`of-tweets` slugs | Banned outright (2026-06-12): week-long counts, no convergence signal |
| **YouTube views + entertainment ("Divertissement")** — `youtube`/`mrbeast`/`views` + awards/box-office/charts/streaming/social-metrics | Banned outright (2026-06-14): no convergence edge, jumps on hype |
| **League of Ireland soccer** — `irl1-` slug prefix | Banned outright (2026-06-12) |
| **"What will be said" markets** — word-bounded `say`/`says`/`said`/`mention(s/ed)`/`utter(s/ed)` | Banned outright (2026-06-18): 'Will the announcers say "Golden Boot"…?' — linguistic coin-flips, no convergence edge |

**Deliberately tradeable** (test-pinned, do not ban): elections, primaries, and mayoral races — a profitable lane despite occasional postponements — and **fast-moving markets**: there are no `oneDayPriceChange` or `oneHourPriceChange` gates (recently-moving markets are often the ones converging toward resolution; both values are logged for offline analysis only).

### 3. Entry filters

| Parameter | Value (`grinder.toml [race]`) |
|---|---|
| Price band (ask) | 0.80 – 0.94, absolute hard cap **0.96** (v4 2026-06-21 — 0.97+ never tradeable) |
| Entry window | **game starts OR market closes within ≤ 4 h** (user 2026-06-14); dynamic widening disabled (`max_hours_cap = 0`) |
| Max spread | ≤ 4¢ |
| Min liquidity | $250 |
| Min 24 h volume | $1000 |

> Exact values live in `configs/profiles/grinder.toml` and are the single source of truth — the table reflects the current live config but may be tuned over time (see [Self-improvement](#self-improvement)).

There are **no price-movement gates** (removed 2026-06-10): markets that moved today or in the last hour, and outcomes that are falling, all stay tradeable — fast movers are often exactly the ones converging toward resolution. `oneDayPriceChange`/`oneHourPriceChange` are logged in the forward-observation net for offline analysis only, and tests pin that neither value can ever exclude a market. The gap-risk protection comes from the category exclusions above, not from movement filters.

### 4. Ranking & pick slots

Survivors are ranked by `bid / hours_to_close` (confidence per remaining hour) and the top `max_orders_per_tick` (**12** in v4 — open as many $5 bets per tick as there are distinct eligible games) become this tick's picks. Markets that can never execute — token already held at its cap, order pending, or event already held — are removed **before** ranking, so they never burn pick slots. **One bet per game:** a game is identified by its date-truncated event slug and the team names in the question (one game spans several Polymarket events — moneyline, `-more-markets`, `-first-to-score`); same-game candidates collapse to a single pick before ranking, an open position on the game blocks all its other markets, and the execution loop backstops same-tick repeats. The single best (highest-bid) candidate per game is kept (the soccer under-4.5 priority was dropped 2026-06-14).

### 5. Sizing (v4 fixed-dollar — user 2026-06-21)

- **Every trade = EXACTLY $5** (`fixed_stake_usd = 5.0`). No Kelly, no %-of-equity, no martingale, no averaging-down, no double-down, no confidence scaling, no dynamic spread. When `fixed_stake_usd > 0` the three sizing functions short-circuit to the flat amount (capped only by available cash).
- **Full capital deployment:** because risk is capped at $5 per trade, the whole bankroll deploys across `bankroll / 5` positions ($50 → 10, $100 → 20, $500 → 100). Worst single-trade loss is $5.
- **Double-down disabled** (`double_down_enabled = false`) — fixed sizing means no averaging into a position.
- Legacy `stake_pct`/`initial_stake_pct` Kelly knobs are ignored while fixed sizing is on (kept for the legacy % mode).

### 6. Execution

- FOK market BUY with a price guard of ask + 1 tick (≤ 0.99).
- **Stake capped to 90 % of the executable ask-side depth** within the guard, so big stakes fill what the book offers instead of being killed (`FOK orders are fully filled or killed`).
- The ledger books the **true fill** (`makingAmount`/`takingAmount`), not the request — entry price, share count, and cash are exact.
- **Top-ups:** a depth-capped entry keeps its market actionable; later ticks may buy more of the same token (re-passing every entry filter and the depth cap) until the position's total cost reaches the 10 % cap. One position per event otherwise.

### 7. Exits

| Condition | Code |
|---|---|
| **Live book** bid ≥ **0.99** (`resolved_exit_threshold = 0.99`, v4 2026-06-21 "sell at 0.99 as well") | `race_big_win_resolved` — primary win exit. EVERY winner sells at a real 0.99 bid (fast-lane 0.98 downgrade removed); above 0.99 rides to settlement at 1.00. Probes the live CLOB bid each tick |
| Cached price ≥ 0.99 after the market left the scan | `resolved_market_sweep_win` — winners-only sweep, same threshold (it can never fire earlier than the race exit) |
| Down ≥ 25 % from entry, confirmed 3 consecutive ticks, **soccer moneylines only** ("Will <Team> win on <date>?") | `race_stop_loss_confirmed` — the one path allowed to sell below entry |
| Genuinely-resolved loser ~8 h past expiry | written off locally (no order; settles on-chain) |

**Never sell below entry** is a hard floor in `execute_live_sell` — the *only* exception is the **controlled stop-loss**, which fires at −30 % only after the loss persists for 3 consecutive ticks (so a one-tick thin-book phantom bid can't dump a winner) and only on sport moneylines ("Will <X> win on YYYY-MM-DD?" Yes/No, any soccer club regardless of league, politics/awards excluded); O/U totals, elections, and everything else ride to on-chain resolution. There is no take-profit ladder, no EOD flatten, and no loss-sweep. The expiry path never force-closes a market that is still accepting orders (it uses a live lookup + `gameStartTime`, since Gamma `endDate` is often set before kickoff). The **daily drawdown halt is disabled** — the per-trade confirmed SL is the risk control.

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
configs/profiles/grinder.toml       bot 1 — grinder (general-purpose) — source of truth
configs/profiles/grinder_b.toml     bots 2 & 3 — weather mode (current live focus)
data/paper_state.json               live ledger (positions, cash)
data/realized_trade_cache.jsonl     durable W/L record (survives resets)
data/trade_journal.jsonl            per-trade metadata + exit reasons
polymarket_bot/
  main.py              CLI, tick orchestration, sizing, journal writer
  race_strategies.py   engine: entry/exit pipeline shared by every strategy + arb scanner
  weather_forecast.py  Open-Meteo multi-model consensus + edge/bracket-margin gates (weather mode)
  portfolio.py         local ledger and position accounting
  trading.py           live CLOB order placement
  gamma.py             Gamma market scan
  models.py            shared dataclasses, exclusion filters
  smart_money.py       leaderboard fetch and signal scoring (not currently run live)
scripts/
  run_live_70.sh       canonical live launcher (Bot 1, grinder)
  run_live_b.sh        Bots 2 & 3 launcher (grinder_b, weather mode)
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
