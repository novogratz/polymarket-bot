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
- **Entry:** ask ∈ **[0.85, 0.97]**, **dynamic window** ≤ 4 h preferred,
  widening 4 → 6 → 8 → 10 → 12 → **24 h**, then end of tomorrow (UTC) for
  daily markets, only when nothing is actionable (`max_hours=4`,
  `max_hours_cap=24`, `daily_expiry_fallback`). **One bet per GAME**
  (`_dedup_same_game` on date-truncated event slug + team names — one game
  spans several Polymarket events; `_open_game_keys` blocks across ticks;
  `EVENT_EXPOSURE_CAP=1`); soccer prefers the **under-4.5** line over the
  rest of the game. Spread ≤ 4¢, liquidity
  ≥ $500, 24 h volume ≥ $300. NO price-movement gates (user 2026-06-10): day-
  change, day-momentum, and 1h gates all removed — fast movers stay tradeable,
  values logged in the forward net only, pinned by tests.
  Scan paginates Gamma past its 100-row cap; held/pending/capped markets are
  dropped before pick-slot truncation.
- **Sizing (dynamic):** hard cap **20% of equity per bet** (`stake_pct`); per-bet
  target = available cash spread across the actionable opportunities (cash/N),
  full cap when the market is slow. Near-resolution boost never pierces the cap.
  Depth-capped entries top up later toward the same cap.
- **Exits:**
  - Resolved-exit: sell at **live CLOB book** bid ≥ `resolved_exit_threshold`
    (0.99; briefly 0.98 on 2026-06-10, reverted — "98¢" on the site is usually
    the midpoint with a real bid at 0.96x, and settlement pays 1.00). The exit
    loop probes the live book per position (`live_best_bid`) — Gamma
    quotes/`curPrice` lag and held winners past 0.99. Probe fail-open →
    cached price.
  - **Controlled stop-loss: −25 %, confirmed over 3 consecutive ticks,
    SOCCER MONEYLINES ONLY** (`sl_pct`, `sl_confirm_ticks`,
    `_is_soccer_moneyline_position`). Min age 5 min. Everything else (O/U,
    elections, …) rides to resolution.
  - **Hard rule: never sell below entry** (floor in `trading.execute_live_sell`).
    Only `race_stop_loss_confirmed` is exempt. Other losers ride to resolution.
  - **Winner floor (0.99)**: winner-exit orders below 0.99 are refused
    (`winner_floor` in `execute_live_sell`, sweep clamped to 0.99, tuner
    bounds pinned (0.99, 0.99)) — resolved winners sell at 0.99 or settle
    at 1.00, never 0.97/0.98.
  - No EOD flatten, no loss-sweep; the winners-only sweep uses
    max(smart, race) thresholds (0.99) and can never front-run the race exit.
  - FOK BUY capped to 90% of executable ask depth; true fill booked; depth-
    capped entries top up later toward the 20% per-bet cap.
  - Expiry never force-closes a market still `acceptingOrders` (uses a live
    lookup + `gameStartTime`, since Gamma `endDate` is often pre-kickoff).
  - **Daily drawdown halt: disabled** (`POLYMARKET_RACE_DAILY_DRAWDOWN_PCT=0`).
- **Tradeable by decision (test-pinned):** elections/primaries/mayoral races
  and fast-moving markets (no 1h gate).
- **Excluded markets (`models.is_excluded_market`):** ALL crypto
  (bitcoin/btc/ethereum/solana/… + Up-Down),
  weather/°C/°F, exact-score, O/U low (0.5–3.5) + high (5.5+) lines, Asian-handicap
  "Spread:", draw markets, halftime markets.
  **Conditional (2026-06-12, "ongoing only"):** esports tradeable ONLY while
  the game is live (gameStartTime past, ≤8h); stocks/indices tradeable ONLY
  during the regular NYSE session (Mon–Fri 09:30–16:00 ET) for that day's
  close (endDate ≤12h). Otherwise both stay excluded.
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
rate**, and open positions (sorted by expiry, each with its estimated end time). No per-trade lists, no heartbeat, no BUY/SELL alerts.

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
