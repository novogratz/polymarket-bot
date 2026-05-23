# Autonomous Strategy

The bot's default autonomous mode is `auto-loop`. It is designed to avoid random trades and only enter when there is repeatable signal evidence in the public order flow. This document describes the per-tick decision flow, the sizing model, the exit waterfall, the auto-tuner, and the dashboard surface.

## Money-making thesis

The core hypothesis: profitable Polymarket wallets sometimes reveal information through their order flow. A single trader buying a token can be noise. Multiple profitable wallets buying the same token in a short window is a stronger signal.

The bot monetises that signal by:

- Copying recent BUY flow from leaderboard wallets that pass PnL, volume, and ROI floors.
- Requiring multi-wallet consensus on the same token before entry.
- Filtering execution: absolute spread, relative spread, price band, freshness, chase premium.
- Sizing each trade as a percentage of the bankroll, weighted by signal conviction.
- Refusing duplicate positions per market, per token, and per event-slug for sports.
- Taking profits with partial SELL orders at staged thresholds rather than waiting for resolution.
- Exiting losers proactively via stop-loss, trailing stop, cohort-sell, and max-hold-time caps.
- Reconciling local ledger state from live Polymarket positions every tick.

The strategy does not predict every market from scratch. It follows public smart-money activity, only when execution quality is acceptable.

## Per-tick flow

Each tick, in order:

1. Auto-tune from the journal (no-op below 30 closed trades).
2. Load Gamma markets (general scan + keyword scan).
3. Sync live Polymarket positions into the local ledger.
4. Refresh live USDC balance from the CLOB.
5. Detect cohort-exit signals (active SELL by entry wallets, or cohort silence).
6. Run the sell waterfall (see below).
7. Execute the smart-money scan: strict → relaxed → deep fallback (one shared leaderboard+trades fetch).
8. Reverse-lookup high-flow tokens not yet in the candidate pool; merge them in.
9. Place trades from the opportunity list with dynamic per-slot sizing toward the cash-floor target.
10. Run the noise-fallback lane if enabled and we are below `MIN_OPEN_POSITIONS` or holding too much cash.
11. Run the BTC edge tick if enabled.
12. Persist portfolio + write journal entries for newly closed positions.
13. Print a JSON tick result; sleep `AUTO_INTERVAL_SECONDS`.

## Entry filters

Each candidate signal must pass:

- Recent BUY trades from at least `MIN_CONSENSUS` distinct wallets (relaxed in fallback passes).
- Cohort PnL / volume / ROI floors.
- Total copied USDC at or above the configured floor.
- Spread filters: absolute (`MAX_SPREAD`) and relative (`MAX_RELATIVE_SPREAD`).
- Price band: `MIN_BUY_PRICE` ≤ ask ≤ `MAX_BUY_PRICE`.
- Freshness: latest cohort BUY within `MAX_SIGNAL_AGE_MINUTES`.
- Chase premium: ask not too far above the cohort average copy price.
- No existing position on the same market, token, or sports event-slug.

## Sizing

Each trade size is computed as:

```
size = available_cash * SMART_POSITION_PCT * conviction_multiplier
```

with the per-slot dynamic adjustment:

```
remaining_to_deploy = total_equity * (1 - SMART_CASH_FLOOR_PCT) - current_invested
per_slot = remaining_to_deploy / remaining_opportunities
size = max(size, per_slot * conviction_multiplier)
```

and the ceiling:

```
ceiling = max(SMART_MAX_POSITION_CEILING_USD, total_equity * SMART_MAX_POSITION_CEILING_PCT)
size = min(size, ceiling, available_cash)
```

Conviction multipliers:

| Signal type | Multiplier |
|---|---|
| Crypto micro | 0.55x |
| Weak (<2-wallet $250 flow) | 0.7x |
| 2-wallet $250+ | 0.9x |
| 2-wallet $1k+ | 1.1x |
| 3-wallet $250+ | 1.1x |
| 3-wallet $500+ | 1.3x |
| 4-wallet $1k+ | 1.6x |
| 4-wallet $2k+ | 2.0x |
| 5-wallet $5k+ | 2.5x |

## Exit waterfall

For every open live position, in order, before any new entry is placed:

1. **Stop-loss** at `-SMART_STOP_LOSS_PCT` after the position has been open at least `STOP_LOSS_MIN_AGE_MINUTES`. Skipped if peak-protect has already armed.
2. **Peak-protect**: once peak PnL exceeded `SMART_PEAK_PROTECT_TRIGGER` (default +100%), close on giveback to `SMART_PEAK_PROTECT_FLOOR` (default +40%).
3. **Trailing stop**: once peak PnL exceeded `SMART_TRAILING_STOP_ARM_PCT` (default +25%), close on giveback of `SMART_TRAILING_STOP_GIVEBACK_PCT` (default 50%) while still positive.
4. **Take-profit ladder**: partial sells at +25% / +50% / +100% / +200% / +300% (15% / 25% / 50% / 25% / 15% of initial shares).
5. **Resolved-market exit**: when the live bid is at or above `SMART_RESOLVED_EXIT_THRESHOLD` (default 0.97), force-close all remaining shares so terminal-price winners do not pin capital.
6. **Max-hold-time**: force-close any position older than `SMART_MAX_HOLD_HOURS` (default 24h) when no other rule has fired.
7. **Near-expiry positive exit**: close at ≥+5% within 20 minutes of market close.
8. **Cohort-sell exit**: if any wallet from the entry cohort has actively SOLD the token within `SMART_COHORT_EXIT_LOOKBACK_MINUTES`, close. The cohort-trade fetch is parallelised across the configured concurrency.
9. **Cohort-silent exit**: if no wallet from the entry cohort has re-bought within the lookback window, close.

When a SELL is rejected by the CLOB with "balance is not enough" (a previous resting sell on the same token is still active), the bot calls `cancel_active_orders_for_token` to cancel that resting order and retries the sell on the next tick.

## Defensive auto-tuner

Every tick, the auto-tuner reads `data/trade_journal.jsonl` and writes bounded overrides to `data/strategy_overrides.json`. Rules:

- Stop-loss share above 40% of trades: tighten `MAX_CHASE_PREMIUM` ×0.80 and `MAX_RELATIVE_SPREAD` ×0.85.
- Consensus=2 trades averaging worse than -$0.30 PnL (≥20 sample): raise `MIN_CONSENSUS` to 3.
- Sports trades averaging worse than -$0.30 PnL (≥15 sample): bump `SPORTS_SCORE_PENALTY` ×1.5.
- Win rate below 30%: raise `MIN_COPIED_USDC` ×1.5.
- Average PnL below -$0.20: shrink `POSITION_PCT` ×0.75.

Defensive only — the tuner never loosens after wins. Disabled by default below 30 closed trades to avoid overfitting on noise.

## Dashboard

`python3 -B -m polymarket_bot.main dashboard` starts a read-only HTTP server on `http://127.0.0.1:8765` that refreshes every 5 seconds. It shows:

- Bot mode and current configuration summary.
- Equity, cash, invested, unrealized PnL, open position count.
- Open positions with entry price, current price, peak PnL, and exit history.
- Recent trades with order IDs.
- Last-tick scanner candidates and rejection breakdown.

The dashboard is passive. It never places orders.

## Autonomous analyst sidecar (`scripts/dry_analyst.py`)

Auto-launched by `scripts/run_both_dry.sh`. Runs alongside the dry race.

**Two cadences:**
- **Report every 15 min** (`ANALYST_CYCLE_SECONDS=900`): full status to Telegram, including:
  - Profitable strategies (all of them, with `$start → $current`, +/-$, +/-%, WR, closed, open)
  - Top 5 by PnL + Bottom 3
  - Codex CLI narrative with Ollama fallback (1–3 paragraphs analysing top + bottom)
  - 🎯 *Favorite for live*: a 4-tier recommendation (live-ready / best risk-adjusted / any-profitable / least-bad) with the favorite's top 3 closed trades + open positions appended
  - 🆕 Spawned / 🔧 Tuned / 💀 Killed events from the last action cycle
- **Spawn/kill every 1 hour** (`ANALYST_SPAWN_KILL_INTERVAL_SECONDS=3600`):
  - Spawns up to 3 new `auto_*` profiles per cycle, derived from current winners via Codex/Ollama proposals
  - Tunes up to 2 existing `auto_*` profiles in place
  - Kills underperformers:
    - Catastrophic: ROI ≤ -50% (any sample size)
    - Sustained: ROI ≤ -10% AND wr ≤ 40% AND n ≥ 8 (auto) / n ≥ 20 (human)
  - Killed TOMLs → `configs/profiles/_archived/<name>_<ts>.toml` (recoverable)
  - Hard cap: 150 total bots (`ANALYST_MAX_BOTS`)

**Kill switch:** `echo '{"enabled":false}' > data/autonomous_state.json` halts new spawns + kills (reports continue).

**Live analyst (`scripts/live_analyst.py`)** is a separate, read-only sidecar launched by `run_all.sh` (or `run_live_70.sh` when running live alone). It cross-compares the live profile to the dry leaderboard and posts executive-summary insights (open positions w/ entry→current→PnL, top closed trades, dry-twin comparison) to `TELEGRAM_CHAT_ID_LIVE` every 30 min. NEVER spawns/kills/modifies anything live.

**Universal sweep** runs every tick in `strategy_loop._force_close_resolved_positions`:
- Closes any open position with cached `current_price ≥ 0.97` (winner)
- Closes any open position with cached `current_price ≤ 0.03` (loser)
- Works for all strategy modes (smart_money, race, edge, news) in both live + dry
- Catches resolved markets that drop out of Gamma scans before per-strategy exit logic fires

**Shared HTTP cache** — `polymarket_bot/smart_money.py:_get_json` wraps every data-api request behind a sha1-keyed disk cache at `data/cache/http/` (TTL 600s, override via `POLYMARKET_HTTP_CACHE_TTL_SECONDS`). `scripts/cache_warmer.py` pre-fetches leaderboards + top wallet trade histories at startup, and `run_all.sh` re-runs it every 8 min so live + dry bots never see a cold cache. Without this, 50+ dry bots each firing their own `leaderboard()` + `trades()` calls saturated the data-api with ~2,500 req/min and 70%+ 429s on the first tick.
