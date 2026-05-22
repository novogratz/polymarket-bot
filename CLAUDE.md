# Claude Code Guide

Claude Code entry point for the Polymarket bot. See also the structured skill in `.claude/skills/polymarket-bot/SKILL.md`.

The project is MIT licensed (see `LICENSE`). Tests run in CI (GitHub Actions, see `.github/workflows/test.yml`).

## Current state snapshot (2026-05-20)

**Live strategy:** `baseline_tight` — fork of `baseline` with two targeted fixes from baseline's 65-trade audit (oversized position cap + loser flush near expiry). Switched from `auto_fresh_qe_persist_stack` on 2026-05-22 after a full hard reset (all dry profiles re-baselined to $20, all state files wiped + backed up to `data/backups/`). Chose baseline because every active dry bot had negative realized PnL — baseline had the smallest absolute loss (-$1.79 on 58 trades, 59% WR) and the closest-to-neutral asymmetry, making it the least-bad pick. Real verdict: the recent market regime is unfavorable for the cohort-copy thesis.
- Engine: `smart_money` (real copy-trade pipeline + multi-wallet consensus) — canonical config, no esoteric filters
- Bankroll: **$20 USDC** baseline (fresh-start reset 2026-05-22)
- Sizing: `position_pct=0.10` (~$2/trade base), `max_position_ceiling_usd=$25`, `max_position_ceiling_pct=0.30` (~$6 cap), `cash_floor_pct=0.02`, `min_open_positions=5`, `starter_trade_usd=5.0`, `assumed_live_balance_usd=20.0`
- Cohort: WEEK top 100, `min_trader_pnl=$1k`, `min_trader_volume=$2k`, `min_trader_roi=3%`. NO persistence filter (this is the canonical baseline — keep it simple)
- Exits: 5-tier TP ladder (+25/+50/+100/+200/+300 with 15/25/50/25/15 partials), trailing +25%/50% giveback, peak-protect +100% → exit at +40%, stop_loss -40% (min-age 15min), max_hold 24h, cohort-sell, resolved at bid ≥0.97
- Filters: `min_consensus=2`, `min_copied_usdc=$75`, price 0.03–0.96, **4h hard cap** (`max_hours_to_close=4.0`)
- Live tick interval: 10s. Heartbeat includes "live vs dry top 3" comparison block.

**Bankroll:** $20 USDC starting on live + all dry (fresh-start reset 2026-05-22). State backups in `data/backups/full_state_<timestamp>.tar.gz`.

### Unified launcher — `scripts/run_all.sh`

Single command boots the whole stack with shared HTTP cache:

```bash
bash scripts/run_all.sh
```

Order of operations:
1. **Pre-warm HTTP cache** (~60s) via `scripts/cache_warmer.py` — populates `data/cache/http/` with leaderboards (3 windows × 8 categories × 4 limits) + the top wallets' recent trade histories so the bot swarm starts with a warm cache and no first-tick 429 storm.
2. **Live bot** (`baseline`, 10s tick) + `scripts/live_analyst.py` sidecar (30min Telegram report).
3. **Dry race** — ~50 curated profiles (NOT all 195 — that crashed mac), each ticking at 10min (`POLYMARKET_AUTO_INTERVAL_SECONDS=600`) with Telegram BUY/SELL alerts silenced per-subshell so only the live bot speaks.
4. **Sidecars** — `scripts/dry_analyst.py` (15min report / 1h spawn-kill) + `pmbot leaderboard --telegram` (5min summary).
5. **Background re-warmer** — re-runs `cache_warmer.py` every 8 min (cache TTL is 10 min) to keep both live + dry continuously warm.

`Ctrl+C` cleans up the whole process group. Trap is on `INT/TERM` only (NOT `EXIT`) and the `cleanup()` is idempotent via a `CLEANED_UP=1` guard — a previous bug had `set -u` crash on an unset var triggering the EXIT trap and killing every bot.

### HTTP cache layer

`polymarket_bot/smart_money.py:_get_json` wraps every data-api request behind a sha1-keyed disk cache at `data/cache/http/`. TTL defaults to 600s (env override `POLYMARKET_HTTP_CACHE_TTL_SECONDS`). With ~50 dry bots each previously firing their own `leaderboard()` + `trades()` calls, the load to data-api was ~2,500 calls/min and 70%+ failed with 429. With the shared cache that drops to ~33 calls/min from the cache-warmer's single pass.

### Recommendation framework for live

Promotion threshold for moving a dry profile to live: **≥10 closed trades AND ROI > 0** as a soft floor; `🎯 LIVE READY ✅` only at ≥30 closed. Below 10 closed = variance, not edge. Catastrophic halt (any strategy with ROI ≤ -50% regardless of sample) auto-archives the profile.

The dry-analyst `_pick_favorite` returns wording "Top of N profitable strategies" when N > 1 — never lies about "only profitable strategy" when several are positive.

**Autonomous loop — see `scripts/dry_analyst.py`:**
- Runs as a sidecar alongside `bash scripts/run_all.sh`
- **Report every 15 min** to `TELEGRAM_CHAT_ID_DRY_RUN`: full leaderboard with $start → $current / +/- $ / ROI% / WR / closed / open per strategy, top 3 trades + open positions for the favorite, plus a tiered live-readiness recommendation.
- **Spawn/kill every 1 hour** (decoupled from report rhythm).
  - Spawns 1–3 new `auto_*` profiles per cycle via `claude` CLI, derived from current winners
  - Tunes (in-place reroll) up to 2 `auto_*` per cycle
  - Kills underperformers: ROI ≤ -10% AND wr ≤ 40% AND n ≥ 8 (auto) / n ≥ 20 (human)
  - Catastrophic halt: ROI ≤ -50% kills any bot regardless of trade count
  - Killed profile → `configs/profiles/_archived/<name>_<ts>.toml` (recoverable)
- **Universal sweep** every tick across all strategies (live + dry):
  - Force-close winners at `current_price ≥ 0.97`
  - Force-close losers at `current_price ≤ 0.03`
  - Catches resolved markets that drop out of Gamma scans before per-strategy exit logic fires
- **Telegram fallback:** `_default_transport` retries with `parse_mode` stripped on HTTP 400, so MarkdownV2 escape failures never silently swallow alerts.
- **Live analyst (`scripts/live_analyst.py`)** — read-only sidecar wired into `run_all.sh` (and standalone `run_live_70.sh`); reads paper_state + dry leaderboard, calls `claude` CLI, posts executive-summary insights (open positions w/ entry→cur→PnL, top closed, dry-twin comparison) to `TELEGRAM_CHAT_ID_LIVE` every 30 min. Never spawns/modifies anything live.

**Universal exit/sizing rules** for race-style strategies:
- `stake_pct ≈ 0.10–0.25`, `max_orders_per_tick = 5`, `cash_floor_pct = 0.02`, `max_hours = 4.0` (hard 4h-only rule)
- Exits per profile: TP, SL, trailing, peak-protect (varies by strategy)
- Resolved exits at bid ≥ 0.97 (and now also ≤ 0.03 via universal sweep)
- Daily DD halt at -15% of starting equity (race + edge)

**Dry race composition (curated in `scripts/run_all.sh:DRY_PROFILES`):** ~50 representative strategies
- Baseline family: `baseline`, `kzerlepgm_baseline`, `claude_baseline_*` (tight, fresh, persist, quick_exit, let_run)
- Smart-money + insider: `smart_money_dry`, `smart_money_loose`, `insider_whales`, `insider_millionaires`
- Race strategies (one per thesis): `aggressive_buyer_detection`, `hybrid_smart_money`, `smart_wallet_consensus`, `wallet_cluster_correlation`, `early_momentum_detection`, `mean_reversion_fade`, `pmlepgm_counter_panic_fade`, `weak_holder_flush_inverse`, etc.
- Claude race batch: `claude_anti_favorite`, `claude_mid_dump_fade`, `claude_resolution_sniper`, etc.
- Momentum family: 8 distinct exit/sizing combos
- Control: `random`
- The live profile (`baseline`) ALSO runs in the dry race for direct apples-to-apples comparison. Live and dry use separate state files (`paper_state.json` vs `data/dry_runs/<name>/state.json`) so they don't conflict.

The previous "all 195 profiles in dry" mode was retired because it crashed macOS and the data-api couldn't keep up. The curated roster covers every thesis family without the bloat.

**Recent code-level fixes (since 2026-05-15):**
- HTTP cache layer in `smart_money.py:_get_json` (TTL 600s) + pre-warm via `scripts/cache_warmer.py`, re-warm loop every 8min in `run_all.sh`
- `scripts/run_all.sh` — single launcher for live + dry race + sidecars + cache pre-warm + re-warm loop. Trap on INT/TERM only (not EXIT), idempotent cleanup, no `set -u` (crashed on harmless unset vars)
- `_force_close_resolved_positions` runs in `strategy_loop` — universal across all strategy modes
- `live_available_balance` smart fallback: when pUSD RPC fails, reads ledger cash and caps by `assume - sum(open_positions_cost)`; defends against live-sync importing positions without debiting cash. RPC failure log throttled to once per 5 min.
- Per-position sizing bug fixed (`ceiling = min(...)` was `max(...)`, allowed $25 BUY on $29.90 bankroll)
- Live analyst now sets `POLYMARKET_PROFILE_LABEL` BEFORE the sidecar spawns (else logs "(unknown)")
- `load_live_snapshot` prefers `current_price × shares` for equity calculation, falls back to size_usd → notional_usd → stake → cost_basis
- Telegram leaderboard truncated to top 15 + bottom 5, all plain text (no MarkdownV2 escape literals)
- Dry-bot Telegram alerts silenced per-subshell via `TELEGRAM_ALERT_*=0` env vars in `run_dry_bot()` — only the live bot speaks
- `_pick_favorite` tier 3 wording: "Top of N profitable strategies" when N > 1 (was always "Only profitable", which lied)
- Analyst journal counter accepts both `realized_pnl_usd` (sweep) and `realized_pnl` (race/smart_money)
- Hard reset workflow: backups kept in `data/backups_full_<ts>_<reset>/`

## Safety

- Never reveal `.env` values, private keys, API secrets, or passphrases.
- Never run `pmbot auto-loop --live --yes` from a chat session. Live trading
  requires the user-initiated interactive prompt (or an explicit script
  invocation like `bash scripts/run_live_70.sh`). The `--yes` flag exists
  only for that script and automation.
- `POLYMARKET_DRY_RUN` and `POLYMARKET_ENABLE_LIVE_TRADING` env vars are
  no longer accepted as user input — `pmbot` warns if they are set in
  the environment. Use `--dry-run` or `--live` flags instead. Internally
  the bot still propagates the `--dry-run` flag to a few modules
  (notifications, dashboard, doctor) via `POLYMARKET_DRY_RUN`; this is an
  implementation detail, not a user-facing toggle.
- Do not implement random or unfiltered live trades. The `noise_fallback` path is the only forced-trade lane and is hard-capped at $10/trade and 4 trades/tick.
- Preserve the local ledger `data/paper_state.json` unless the user explicitly asks for a reset.
- Preserve `data/trade_journal.jsonl` and `data/strategy_overrides.json` unless explicitly asked to reset them.
- No LLM call (Claude, Codex, anything else) in the scanning or trade-selection path. The scanner stays deterministic Python over Polymarket APIs.
- The bot does not have the capability to write or push source code on its own.

## Project map

- `polymarket_bot/main.py` — CLI commands and strategy loops. Tick orchestration, sizing helpers, trade-journal writer, `journal-stats` and `tune-strategy` commands.
- `polymarket_bot/smart_money.py` — leaderboard fetching, parallel trade fetching, signal grouping, scoring, reverse-lookup helper.
- `polymarket_bot/auto_tuner.py` — reads the trade journal each tick and computes bounded strategy overrides (defensive only — tightens after losses).
- `polymarket_bot/bitcoin.py` — BTC threshold edge model (Black-Scholes-from-volatility).
- `polymarket_bot/trading.py` — authenticated live BUY/SELL order placement and final stake computation.
- `polymarket_bot/dashboard.py` — local real-time HTML dashboard at `http://127.0.0.1:8765`.
- `polymarket_bot/portfolio.py` — local ledger with cash, open positions, pending orders, and exit records.
- `polymarket_bot/gamma.py` — Gamma client (market scan + reverse-lookup by clob_token_ids).
- `polymarket_bot/strategy.py` — candidate ranking from Gamma payloads.
- `polymarket_bot/models.py` — shared dataclasses and parsing helpers.
- `scripts/run_all.sh` — preferred launcher: live + dry race + sidecars + HTTP cache pre-warm + 8min re-warm loop.
- `scripts/run_live_70.sh` — live-only runner (loads `whale_entry_detection.toml`, $45 bankroll).
- `scripts/cache_warmer.py` — pre-fetches leaderboards + wallet trade histories into `data/cache/http/` so the bot swarm starts warm.
- `scripts/dry_analyst.py` — autonomous analyst sidecar: 15min reports, 1h spawn/kill via `claude` CLI.
- `scripts/live_analyst.py` — read-only live analyst sidecar; 30min executive-summary Telegram reports.
- `scripts/winner_consistency.py` — sliding-window analyzer (30min windows over 8h lookback).
- `tests/test_strategy.py` — 52 tests covering scoring, sizing, exit plans, auto-tuner rules.
- `docs/PROFILES.md` — référence exhaustive des clés TOML des profils (sections, types, défauts, rôles).
- `docs/STRATEGIES.md` — document maître des 6 lanes d'achat et des 9 conditions d'exit.

## Off-line analysis tools (read-only, no SDK calls)

Scripts dans `scripts/` qui produisent des CSV (`data/`, gitignored) et des
rapports markdown (`reports/`). Ils n'utilisent que les APIs publiques
Polymarket et ne modifient **jamais** `polymarket_bot/*.py`.

- `wallet_history_ytd.py` — ranking YTD des wallets leaderboard (FIFO)
- `analyze_top_wallets.py` — cohortes, distribution PnL, suggestions de filtres
- `market_reaction_time.py` — détection des jumps endogènes ≥5¢
- `news_reaction.py` — latence news-ancrée (BLS/FOMC/Truth Social)
- `wallet_edge_directional.py` — Étude C, edge directionnel par BUY (pct_ahead, mean_edge)
- `analyze_wallet_8b52.py` — profil détaillé d'un wallet (positions, distribution, top markets)
- `rank_copyable_wallets.py` — classement composite Z-score pct_ahead + mean_edge

56 tests synthétiques dans `tests/test_{wallet_history_ytd,market_reaction_time,news_reaction,wallet_edge_directional}.py`.

## Development workflow

Run tests:

```bash
uv run python -B -m unittest discover -s tests
```

Quick CLI snapshots (read-only, no SDK calls):

```bash
uv run pmbot status              # mode, equity, open positions, journal path/count
uv run pmbot positions           # CLI table of open positions, sorted by PnL desc
uv run pmbot --version           # version
```

`status` and `positions` automatically read the dry-run ledger when
the `--dry-run` flag is passed. Output is colorized on a TTY; `NO_COLOR=1`
disables ANSI codes, `POLYMARKET_FORCE_COLOR=1` forces them through pipes.

Dashboard:

```bash
uv run pmbot dashboard
```

Trade-journal stats (per-bucket P&L, win rate, suggested tightenings):

```bash
uv run pmbot journal-stats
```

Run the auto-tuner manually (writes `data/strategy_overrides.json`):

```bash
uv run pmbot tune-strategy
```

Live smart-money loop (interactive confirmation requested unless `--yes` is
passed; the `--yes` flag is intended for scripts and automation only):

```bash
uv run pmbot auto-loop --live --profile live-90
```

Dry-run smart-money loop (simulates orders without spending any cash;
writes a separate ledger and journal):

```bash
uv run pmbot auto-loop --dry-run --profile baseline
```

In dry-run mode every BUY/SELL is short-circuited (no SDK call), live
position sync is skipped, and state is persisted to
`data/dry_run_state.json` + `data/dry_run_journal.jsonl` so the live
paper-trading ledger stays untouched.

Quiet mode (compresses each tick to a one-line readable footer plus
optional `→ BUY/SELL/NOISE/BTC` lines for executed actions, capped at 6;
suppresses leaderboard pulls, trade fetches, reverse-lookup chatter, and
BUY/SELL JSON dumps; the full tick payload is no longer printed in this
mode):

```bash
POLYMARKET_QUIET=1 uv run pmbot auto-loop --live --profile live-90
```

Combine with dry-run for a clean simulation feed:

```bash
POLYMARKET_QUIET=1 uv run pmbot auto-loop --dry-run --profile baseline
```

## Recommended live command

Use `run_all.sh` (preferred — boots live + dry race + sidecars + cache pre-warm):

```bash
bash scripts/run_all.sh
```

Or just the live bot alone (no dry race, no cache pre-warm — only do this if you don't want the cache benefit):

```bash
bash scripts/run_live_70.sh
```

Both scripts load `configs/profiles/baseline.toml` as the single source of truth for the live config. Current settings:

- Profile: `baseline` (canonical smart_money pipeline — no esoteric filters, the reference control strategy)
- `POLYMARKET_SYNC_LIVE_POSITIONS=1`, `POLYMARKET_AUTO_INTERVAL_SECONDS=10`
- Sizing ($20 fresh-start baseline 2026-05-22): `starting_cash=20.0`, `assumed_live_balance_usd=20.0`, `position_pct=0.10` (~$2/trade), `max_position_ceiling_usd=25.0`, `max_position_ceiling_pct=0.30` (~$6 cap on $20 equity), `cash_floor_pct=0.02`, `min_open_positions=5`, `starter_trade_usd=5.0`
- Cohort: WEEK top 100, `min_trader_pnl=$1k`, `min_trader_volume=$2k`, `min_trader_roi=3%`. No persistence filter (canonical baseline)
- Entry filters: `min_consensus=2`, `min_copied_usdc=$75`, `max_chase_premium=0.13`, price 0.03–0.96, **4h hard cap** (`max_hours_to_close=4.0`)
- Exits: 5-tier TP ladder (+25/+50/+100/+200/+300 with 15/25/50/25/15 partials), trailing arms +25% / 50% giveback, peak-protect arms +100% / exits +40%, stop_loss -40% (min-age 15min), max_hold 24h, cohort-sell, resolved at bid ≥0.97. SELLs rejected with "balance is not enough" trigger an automatic cancel of the resting CLOB order on that token and retry on the next tick.
- Live analyst sidecar (`scripts/live_analyst.py`) launches alongside; posts read-only insights every 30 min to `TELEGRAM_CHAT_ID_LIVE`.
- Universal sweep closes positions at price ≥0.97 OR ≤0.03 every tick across all strategy modes.
- HTTP cache shared with the dry race at `data/cache/http/` (TTL 600s), refreshed every 8min by the background loop in `run_all.sh`.

Dashboard at `http://127.0.0.1:8765` by default.

## Tick sequence

Each tick prints structured progress to stdout, followed by a JSON summary. Order:

1. Auto-tune: read journal, compute overrides if ≥30 closed trades, apply on top of env-var settings.
2. Load Gamma markets (scan + keyword scan).
3. Sync live Polymarket positions into the local ledger.
4. Refresh live USDC cash from CLOB.
5. Cohort-exit detection (active SELL by entry wallets, or no fresh BUYs).
6. Sell strategy: take-profit ladder, trailing stop, peak-protect, stop-loss, cohort exits, near-expiry, max-hold-time.
7. Smart-money scan: strict → relaxed → deep fallback. One leaderboard+trades fetch shared across all three.
8. Reverse-lookup high-flow tokens not in current candidates; merge into the eligible pool.
9. Place trades from the opportunity list with dynamic per-slot sizing toward the cash floor.
10. Noise fallback if enabled, smart-money fired zero trades this tick, and either below `MIN_OPEN_POSITIONS` or cash share above the cash-pressure threshold.
11. BTC edge tick if enabled.
12. Persist portfolio + write journal entries for any closed positions.
13. Print JSON result, sleep `AUTO_INTERVAL_SECONDS`.

## Winning strategy

The default strategy is smart-money copy-trading. The bot does not invent an opinion on every market — it waits for public order-flow evidence that profitable wallets are buying the same token, then mirrors that flow with bounded sizing.

### The edge

Wallets at the top of monthly Polymarket leaderboards with positive PnL and meaningful volume have, on average, an informational or analytical edge on the markets they trade. When several of those wallets buy the same token in a short window (30 minutes), the collective signal is stronger than a single wallet. The bot mirrors this flow.

Risks the strategy avoids:

- **Fake edge** — one lucky wallet on an isolated trade. Filtered by ROI / volume / multi-wallet consensus.
- **Bad execution** — paying the spread erases the edge. Filtered by absolute and relative spread, chase premium.
- **Concentration** — six bets on the same event. Filtered by per-market and per-event-slug dedupe.
- **Round-trip to flat** — a winner that gives back to zero. Filtered by take-profit ladder + trailing stop + peak-protect.
- **Drawdown without exit** — a loser bleeding slowly. Filtered by stop-loss after min-age.
- **Cohort flip** — entry wallets selling. Filtered by active cohort-sell detection.

### Entry conditions

- Recent BUY trades from leaderboard wallets that pass PnL / volume / ROI floors.
- Multi-wallet consensus on the same token (relaxed in fallback passes when below the open-positions target).
- Enough copied USDC, scaled by conviction tier.
- Tradable market: tight absolute and relative spreads, ask within configured price band, not too close to expiry.
- No existing open position on the same market or token. Sports respect a per-event concentration cap.
- Explicit `--live` flag on `pmbot auto-loop` (with `--yes` only when invoked from a script).
- Conviction-weighted sizing: weak signals near the floor; very-high-conviction signals (5+ wallets, $5k+ copied) up to 2.5× the base, capped by the per-position ceiling.

### Exits (run before every new entry)

- Take-profit ladder at +25% / +50% / +100% / +200% / +300% with partial sells (15% / 25% / 50% / 25% / 15%).
- Trailing stop arms at +25% peak, exits on 50% giveback while still positive.
- Peak-protect arms at +100% peak, exits on giveback to +40%.
- Stop-loss at -40% after 15 minutes in position (does not fire if peak-protect already armed).
- Cohort-sell exit when any entry wallet has SOLD the token within the lookback window; cohort-silent exit when no cohort wallet has re-bought.
- Near-expiry positive-PnL exit at ≥+5% within 20 minutes of close.
- Max-hold-time force-close at 24 hours when no other reason fires.

### Sizing by conviction

```
crypto micro                     -> 0.55x
weak (<2-wallet $250)            -> 0.7x
2-wallet $250+                   -> 0.9x
2-wallet $1k+                    -> 1.1x
3-wallet $250+                   -> 1.1x
3-wallet $500+                   -> 1.3x
4-wallet $1k+                    -> 1.6x
4-wallet $2k+                    -> 2.0x
5-wallet $5k+                    -> 2.5x
```

The multiplier is applied to `cash * SMART_POSITION_PCT`, capped by the larger of `SMART_MAX_POSITION_CEILING_USD` or `equity * SMART_MAX_POSITION_CEILING_PCT`, and bounded by `SMART_CRYPTO_MICRO_MAX_TRADE_USD` for crypto-micros.

### Defensive auto-tuner

The auto-tuner reads the trade journal each tick. From 30 closed trades on:

- Stop-loss > 40% of trades: tighten `MAX_CHASE_PREMIUM` ×0.80 and `MAX_RELATIVE_SPREAD` ×0.85.
- Consensus=2 trades avg PnL < -$0.30 (≥20 sample): raise `MIN_CONSENSUS` to 3.
- Sports avg PnL < -$0.30 (≥15 sample): bump `SPORTS_SCORE_PENALTY` ×1.5.
- Win rate < 30%: raise `MIN_COPIED_USDC` ×1.5.
- Avg PnL < -$0.20: reduce `POSITION_PCT` ×0.75.

Defensive only: tightens after losses, never loosens after wins. Loosening based on a noise-biased sample = amplifying noise.

### Not guaranteed profit

The expected edge comes from copying strong public flow while avoiding bad execution. **This is not guaranteed profit.** No-signal / no-trade is a valid position. The bot is not meant to trade 24/7; quiet hours stay quiet.
