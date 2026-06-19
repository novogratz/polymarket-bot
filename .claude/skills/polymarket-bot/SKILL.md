---
name: polymarket-bot
description: Claude Code skill for the Polymarket grinder bot. Use for any change to strategy, filters, sizing, exits, exclusions, reporting, or the reset/launch scripts.
---

# Polymarket Bot Skill

Deterministic favorite-grinder bot for Polymarket binary markets. **No LLM in the
scan or trade-selection path** — the engine is pure Python over Polymarket APIs.

## Current strategy — `grinder` (race mode)

Buy a heavily-favored binary outcome and ride it to resolution.

- **Config (source of truth):** `configs/profiles/grinder.toml` (bot 1) and
  `configs/profiles/grinder_b.toml` (bots 2 & 3). Keep their strategy keys in sync.
- **Entry:** ask ∈ **[0.85, 0.97]**, **game starts OR market closes within
  ≤ 4 h** (user 2026-06-14; dynamic widening OFF via `max_hours_cap=0`),
  (`max_hours=4`,
  `daily_expiry_fallback=false`; user 2026-06-12). **One bet per GAME**
  (`_dedup_same_game` on date-truncated event slug + team names — one game
  spans several Polymarket events; `_open_game_keys` blocks across ticks;
  `EVENT_EXPOSURE_CAP=1`); keeps the single best (highest-bid) candidate
  per game (the soccer under-4.5 priority was dropped 2026-06-14). Spread ≤ 4¢, liquidity
  ≥ $500, 24 h volume ≥ $300. **Per-lane entry floors ≥ 0.92**: esports
  (`ESPORTS_MIN_ASK`) and soccer/sport "Will <X> win on <date>?" moneylines
  (`SOCCER_MONEYLINE_MIN_ASK`, user 2026-06-17 — gap-bombs below 0.92; every
  moneyline loss ever entered ≤ 0.90, 0.90+ has zero losses). NO price-movement
  gates (user 2026-06-10): day-
  change, day-momentum, and 1h gates all removed — fast movers stay tradeable,
  values logged in the forward net only, pinned by tests.
<<<<<<< HEAD
  Scan reads the **top 100 markets** per ordering (`scan_limit=100`, user
  2026-06-15 — single Gamma page, no pagination); held/pending/capped markets
  are dropped before pick-slot truncation.
- **Sizing (dynamic):** hard cap **15% of equity per bet** (`stake_pct`; raised from 10% 2026-06-14); fresh entries open at
  the lower `initial_stake_pct` (5%) so the dip double-down has headroom to
  fill toward the 10% cap; per-bet
  target = available cash spread across the actionable opportunities (cash/N),
  full cap when the market is slow. Near-resolution boost never pierces the cap.
  Depth-capped entries top up later toward the same cap.
=======
  Scan paginates Gamma past its 100-row cap; held/pending/capped markets are
  dropped before pick-slot truncation.
- **Sizing (Kelly, dynamic — user 2026-06-18 "10% isn't enough"):** the binding
  lever is the ENTRY size (`initial_stake_pct = 0.20`), not the cap — most
  winners never dip so they ride at the entry size; the hard cap
  `stake_pct = 0.35` is only reached via the dip double-down. Near-full-Kelly,
  aggressive by explicit choice: `f* = (p·b − q·a)/(a·b) ≈ 0.35` for p≈0.97,
  b≈8.4%, a≈1.0. Worst single total loss −20% (entry) / −35% (doubled). Per-bet
  target = cash/N spread, full cap when slow; near-resolution boost never
  pierces the cap. Tuner may move `stake_pct` in (0.05, 0.35). See
  `docs/STRATEGIES.md` for the derivation.
>>>>>>> 23db231 (race: Kelly position sizing — near-full-Kelly (entries 20%, cap 35%))
  **Dip double-down (2026-06-14):** ANY held position whose live ask has
  dipped below entry and is still **≥ 0.60** (alive proxy — no live-score
  feed) is bought up once toward the 10% cap (`_execute_double_downs`,
  `race_double_down_enabled`).
- **Exits:**
  - Resolved-exit: sell at **live CLOB book** bid ≥ a dynamic per-position
    threshold `min(0.99, max(resolved_exit_threshold, entry + min_profit_margin))`
    (2026-06-15: 0.97 entry → 0.99 exit, never break-even); else `resolved_exit_threshold`
    (**0.97**, user 2026-06-14 "as we had before"; was 0.99). The exit
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
  - **Winner floor (0.97)**: winner-exit orders below **0.97** are refused
    (`winner_floor` in `execute_live_sell`, sweep clamped to 0.97, tuner
    pinned (0.97, 0.97)). One flat floor across every lane (user 2026-06-14;
    was 0.99/0.98-fast-lane).
  - No EOD flatten, no loss-sweep; the winners-only sweep uses
    max(smart, race) thresholds (0.97) and can never front-run the race exit.
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
  **Conditional (2026-06-12):** esports = **League of Legends ONLY**
  (Mobile Legends, CS, and all other titles banned outright; Game/Map
  Handicap banned), only while the game is live (gameStartTime past,
  ≤8h), ask ≥ **0.92**.
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
rate**, open positions (sorted by expiry, each with its estimated end
time), and a redemption watchdog (resolved-but-unpaid positions ≥ $1). No per-trade lists, no heartbeat, no BUY/SELL alerts.

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
