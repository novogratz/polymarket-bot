---
name: polymarket-bot
description: Claude Code skill for the Polymarket grinder bot. Use for any change to strategy, filters, sizing, exits, exclusions, reporting, or the reset/launch scripts.
---

# Polymarket Bot Skill

Deterministic **weather-only** favorite-grinder bot for Polymarket binary markets
(since 2026-07-06 it bets exclusively on weather / temperature markets). **No LLM
in the scan or trade-selection path** — the engine is pure Python over Polymarket
APIs.

## Current strategy — `grinder` (race mode) — WEATHER-ONLY

Buy a heavily-favored binary outcome and ride it to resolution.

- **Config (source of truth):** `configs/profiles/grinder.toml` (bot 1) and
  `configs/profiles/grinder_b.toml` (bots 2 & 3). Keep their strategy keys in sync.
- **WEATHER-ONLY lane (user 2026-07-06, "put bot 1 to the same strategy as
  bot 2 which is weather only bets"):** `weather_only = true`
  (`POLYMARKET_RACE_WEATHER_ONLY`) in BOTH profiles — entry selection keeps
  ONLY weather / temperature markets (`is_weather_market` in `models.py`:
  temperature, °C/°F, weather, rainfall, snowfall, high/low temp) and
  bypasses the ban list (weather is itself banned there). Every non-weather
  market is dropped. Ported from `kzer_windows` (bot 3's 2026-06-23
  experiment); `WeatherOnlyLaneTests` pin the behavior.
- **Entry:** ask ∈ **[0.80, 0.94]**, absolute hard cap **0.96**
  (`max_price_hard_cap`, v4 2026-06-21 — 0.97+ never tradeable),
  **game starts OR market closes within
  ≤ 24 h** (weather-only 2026-07-06 — weather resolves end-of-day ~22–46 h
  out, a 4 h window has zero weather candidates; dynamic widening OFF via
  `max_hours_cap=0`), (`max_hours=24`,
  `daily_expiry_fallback=false`; user 2026-06-12). **One bet per GAME**
  (`_dedup_same_game` on date-truncated event slug + team names — one game
  spans several Polymarket events; `_open_game_keys` blocks across ticks;
  `EVENT_EXPOSURE_CAP=1`); keeps the single best (highest-bid) candidate
  per game (the soccer under-4.5 priority was dropped 2026-06-14). Spread ≤ 4¢, liquidity
  ≥ $250, 24 h volume ≥ $1000 (v4 2026-06-21). **Per-lane entry floor ≥ 0.92**: soccer/sport
  "Will <X> win on <date>?" moneylines
  (`SOCCER_MONEYLINE_MIN_ASK`, user 2026-06-17 — gap-bombs below 0.92; every
  moneyline loss ever entered ≤ 0.90, 0.90+ has zero losses). NO price-movement
  gates (user 2026-06-10): day-
  change, day-momentum, and 1h gates all removed — fast movers stay tradeable,
  values logged in the forward net only, pinned by tests.
  Scan paginates Gamma past its 100-row cap; held/pending/capped markets are
  dropped before pick-slot truncation.
- **Sizing (FULL-DEPLOY — user 2026-07-09, "100% of the account is always
  invested"):** `full_deploy = true` in BOTH profiles. Each tick spreads ALL
  available cash across the actionable picks (`cash / N` per bet); the three
  sizing functions return **full equity as the cap** (no per-position
  ceiling), so leftover cash keeps flowing into already-held markets via the
  top-up lane (each top-up re-passes every entry filter) until the account
  is fully deployed. `cash_floor_pct = 0`. Worst-case loss on one market =
  the whole account (explicit mandate). OVERRIDES `fixed_stake_usd`;
  rollback = `full_deploy = false`, `fixed_stake_usd = 5.0`.
  `FullDeploySizingTests` pin it.
- **Sizing (RETIRED v4 fixed-dollar — user 2026-06-21):** every trade = EXACTLY $5
  (`fixed_stake_usd = 5.0`). No Kelly, no %-of-equity, no martingale, no
  averaging/double-down, no confidence scaling, no dynamic spread. The three
  sizing functions (`_position_cap_usd`, `_entry_cap_usd`,
  `_dynamic_stake_target`) short-circuit to the flat $5 (capped only by
  available cash), so worst single-trade loss is $5 and the bankroll deploys
  fully across `bankroll / 5` positions. Legacy `stake_pct`/`initial_stake_pct`
  ignored while fixed sizing is on. **Double-down DISABLED**
  (`double_down_enabled = false`).
- **Unban + category auto-disable (v4):** `unban_all_markets = true` bypasses
  `is_excluded_market` at entry selection — every category allowed, governed by
  the **data-driven category auto-disable** (`categories.py`): ≥ 100 realized
  trades in a category AND ROI < −5% → dropped from selection
  (`race_category_min_samples` / `race_category_disable_roi`). Computed per tick
  from the realized ledger (fail-open); `other` never disabled. Risk bounded by
  the $5 stake. `pmbot journal-stats` shows `by_v4_category` ROI.
- **Forecasting EV/quality gates (v4, OPT-IN — default OFF):** `forecast.py`
  calibrates a favorite's win probability per (category, price-bucket) from
  realized history (shrunk toward the prior = overall win rate); `edge =
  predicted − ask`. `race_min_edge > 0` drops sub-edge outcomes;
  `race_min_quality_score > 0` drops low-`quality_score` ones (blend of edge /
  volume / resolution clarity / category & bucket ROI). Both default 0 (need
  history to calibrate). `journal-stats` adds `by_v4_price_bucket` +
  `v4_performance` (Sharpe / profit factor / max DD / promotion ≥500 trades &
  ROI ≥5%). Drawdown/loss-pause halts intentionally NOT built (user "no halts").
- **Resolution-safety filter (v4, ALWAYS-ON — `min_resolution_clarity = 60`):**
  skips subjective/ambiguous-settlement markets (`forecast.resolution_clarity`)
  even under unban (needs no history). Clean market = 100; one strong
  subjective marker (deemed/disputed/judges/…) drops below 60.
- **Exits:**
  - Resolved-exit: sell at **live CLOB book** bid ≥ a dynamic per-position
    threshold `min(0.99, max(resolved_exit_threshold, entry + min_profit_margin))`
    — v4 (user 2026-06-21) `resolved_exit_threshold = **0.99**`, so EVERY
    winner exits at a real 0.99 bid (fast-lane 0.98 downgrade removed), else
    rides to settlement at 1.00. The exit
    loop probes the live book per position (`live_best_bid`) — Gamma
    quotes/`curPrice` lag. Probe fail-open → cached price.
  - **Controlled stop-loss: −30 %, confirmed over 3 consecutive ticks,
    SPORT MONEYLINES ONLY** (`sl_pct=0.30`, `sl_confirm_ticks`,
    `_is_soccer_moneyline_position` — matches "Will <X> win on YYYY-MM-DD?"
    Yes/No and excludes politics/awards; any soccer club passes regardless of
    league slug, 2026-06-16). Min age 5 min. Everything else (O/U,
    elections, …) rides to resolution. **Anti-gap floor
    `sl_min_exit_price=0.50` (2026-06-17):** the SL only executes while the live
    bid is still ≥ 0.50; below that it's a goal-crash that mean-reverts (Difaâ
    "No" 0.89 → 0.02 → resolved 1.0) → HOLD to resolution, don't dump.
  - **Hard rule: never sell below entry** (floor in `trading.execute_live_sell`).
    Only `race_stop_loss_confirmed` is exempt. Other losers ride to resolution.
  - **Winner floor (0.99)**: winner-exit orders below **0.99** are refused
    (`winner_floor` in `execute_live_sell`, tuner pinned (0.99, 0.99)). One
    flat floor across every lane (user 2026-06-21 v4, "sell at 0.99 as well").
  - No EOD flatten, no loss-sweep; the winners-only sweep uses
    max(smart, race) thresholds (0.99) and can never front-run the race exit.
  - FOK BUY capped to 90% of executable ask depth; true fill booked; depth-
    capped entries top up later toward the 10% per-bet cap.
  - Expiry never force-closes a market still `acceptingOrders` (uses a live
    lookup + `gameStartTime`, since Gamma `endDate` is often pre-kickoff).
  - **Daily drawdown halt: disabled** (`POLYMARKET_RACE_DAILY_DRAWDOWN_PCT=0`).
- **Tradeable by decision (test-pinned):** elections/primaries/mayoral races
  and fast-moving markets (no 1h gate).
- **Excluded markets (`models.is_excluded_market`):** ALL crypto
  (bitcoin/btc/ethereum/solana/… + Up-Down),
  weather/°C/°F, exact-score, **ALL O/U goal totals (0.5–7.5 incl. 4.5 — banned
  2026-06-14, 80% of losses)**, Asian-handicap
  "Spread:", draw markets, halftime markets, League of Ireland soccer
  (`irl1-` slug prefix, 2026-06-12).
  Tweet-count markets banned outright (2026-06-12); YouTube views +
  entertainment ("Divertissement") banned outright (2026-06-14:
  `youtube`/`mrbeast`/`views` + awards/box-office/charts/streaming/social
  metrics). **"What will be said" markets banned outright (2026-06-18,
  `_SPEECH_MARKET_RE`):** word-bounded `say/says/said/mention(s/ed)/utter(s/ed)`
  — 'Will the announcers say "Golden Boot"…?' — linguistic coin-flips, no edge.
  **ALL stock market/equities banned outright** (re-banned 2026-06-12;
  generic "(TICKER) … $" rule catches unlisted tickers).
  **Esports — BANNED OUTRIGHT (2026-06-19):** every title (League of
  Legends/LoL, Counter-Strike, CS2, Valorant, Dota, Mobile Legends, Game/Map
  Handicap, BO1/BO3/BO5, …) excluded regardless of live status or ask
  (`is_esports_text`); the prior LoL-only-while-live carve-out is removed.
- **Disabled:** `btc_edge` lane, `noise_fallback`.

## Multi-bot layout

3 independent live bots, each its own wallet / `.env` / ledger.

- **Launchers:** `run_live_70.sh` (bot 1), `run_live_b.sh` (bots 2 & 3),
  `run_live_win.sh` (Windows). Branches: `main` + `kzer_windows`.
- **Per-machine baseline:** `data/starting_cash.txt` (gitignored) — each bot's
  report baseline, independent of the shared profile. Written by `fresh_start.py`.
- Ledger/journal/cache are gitignored = per-machine; only code + profiles are shared.

## Reporting — `scripts/live_analyst.py`

The **only** Telegram message. Deterministic French "RAPPORT LIVE": fires on
**startup**, then every `LIVE_ANALYST_CYCLE_SECONDS`, plus a daily 10:00 US/Eastern.
Shows equity, **P&L since start (= equity − baseline)**, **total trades + win
rate**, a **v4 performance block** (≥10 closed trades: ROI / Sharpe / profit
factor / max DD + a **p/q/edge** line all-time & today, `🎯 p=avg entry · q=win
rate · edge(q−p)` — +EV only when q>p — + a **best/worst category** line
(`🥇 Meilleure catégorie … 🥶 Pire …`, ranked by realized $ P&L; "weather" is
a first-class category since 2026-07-10, so the weather-only lane shows
`weather +$X` here) + worst
per-category ROIs, ⛔ on auto-disabled), open positions and trades-of-the-day **each capped to the top
`LIVE_REPORT_TOP_N` winners + N worst losers** (default 5, `_winners_losers`;
rest folded into `… +X autres`; `=0` → summary only — user 2026-06-22 "I want
something clear as a summary"), shown positions keeping their estimated end time,
and a redemption watchdog (resolved-but-unpaid positions ≥ $1). No heartbeat, no
BUY/SELL alerts. `_fetch_live_equity` retries the `/positions` API 3× and falls
back to the local ledger on failure (never cash-only — that used to fabricate a
"$60 / -$100" capital via a stale `assumed_live_balance_usd` floor, now removed).

## Reset workflow — `scripts/fresh_start.py`

Run on a bot's own machine, bot stopped: wipes closed-trade history, **keeps open
trades** (re-synced on start), stamps `data/live_tracking_start`, and sets the
per-machine baseline. `--equity X` forces the baseline (else cash + positions).

## Guardrails

- No `.env` values, private keys, or passphrases in output or commits.
- Live trading requires `--live` on `pmbot auto-loop`; `--yes` is script-only.
- No LLM in the scan or trade-selection path.
- Never delete `data/paper_state.json`, `data/trade_journal.jsonl`, or
  `data/realized_trade_cache.jsonl` unless the user explicitly asks for a reset.
- The bot must not gain the capability to commit or push source code.
- **Daily self-learning sidecar** (`scripts/daily_self_improve.sh`): launchers
  run it once/day after 23:00 local → end-of-day analysis (`auto_improve.py
  --analyze-only`) + the fenced Claude self-tuner. Fully try/catch-wrapped
  (never crashes the live loop), once/day, always restores the git branch.
  Tuner fences intact: EXIT/SIZING only, entry FROZEN, no SL, tests+CI gated,
  only `grinder.toml` writable. Toggle `DAILY_SELF_IMPROVE=0`.

## Commands

```bash
uv run python -B -m unittest discover -s tests   # tests
uv run pmbot status                              # equity, open positions
bash scripts/run_live_b.sh                       # launch a live bot
uv run python scripts/fresh_start.py             # reset (keep open trades)
```

## Key files

- `polymarket_bot/race_strategies.py` — grinder engine (`select_grinder`,
  `_build_eligible_candidates`, `_execute_race_exits`, confirmed SL, expiry).
- `polymarket_bot/trading.py` — order execution + never-sell-below-entry floor.
- `polymarket_bot/models.py` — `is_excluded_market` (the ban list).
- `polymarket_bot/config.py` — all `Settings` fields / env-var names.
- `scripts/live_analyst.py` — the Telegram report.
- `scripts/fresh_start.py` — per-machine reset.

## Editing workflow

1. Strategy/filter/sizing/exit changes → `configs/profiles/grinder.toml` **and**
   `grinder_b.toml` (keep in sync). Code-level → `race_strategies.py` /
   `trading.py` / `models.py`.
2. Update tests (`tests/test_strategy.py`) if behavior changes.
3. Propagate to `kzer_windows` (cherry-pick) when it should apply to bot 3.
4. Update `CHANGELOG.md`, `README.md`, `CLAUDE.md`, and this SKILL.md when user-visible.
