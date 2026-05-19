# Claude Code Guide

Claude Code entry point for the Polymarket bot. See also the structured skill in `.claude/skills/polymarket-bot/SKILL.md`.

The project is MIT licensed (see `LICENSE`). Tests run in CI (GitHub Actions, see `.github/workflows/test.yml`).

## Current state snapshot (2026-05-19)

**Live strategy:** `claude_baseline_quick_exit` — defensive variant of `kzerlepgm_baseline` (which is `main:baseline.toml` + 4h hard cap). Real smart_money copy-trade pipeline with tighter exits.
- Diff vs parent: SL -25% (vs -40%), peak-protect arms @+50% exit @+20% (vs +100%/+40%), trailing arms @+15% (vs +25%), `stop_loss_min_age_minutes` = 5 (vs 15).

**Bankroll:** $20 USDC starting baseline (all profiles share this).

### Strategy inventory (170 active profiles)

| Family | Count | Description |
|---|---|---|
| `auto_*` | 111 | Analyst-spawned variants (derived from current winners via `claude` CLI proposals) |
| `claude_*` | 19 | Hand-curated A/B variants — exit/cohort/persistence experiments |
| `momentum_*` | 9 | Momentum-themed (user's favorite thesis): breakout, exhaustion, continuation, panic |
| `copy-*` | 4 | Mirror-mode single-wallet copy-trade |
| `insider_*` | 2 | Top lifetime-PnL whale copy (≥$1M or ≥$500k all-time PnL) |
| Other | 25 | Base race strategies (panic_fade, contrarian, favorite, etc.) + `edge`, `news`, `baseline`, `live-90` |
| Archived | 31 | Catastrophic-loss kills (ROI ≤ -50%) — moved to `configs/profiles/_archived/` |

### Best strategies right now (rated, sorted by equity)

| Strategy | Equity | PnL | WR | Closed | Comment |
|---|---|---|---|---|---|
| `claude_baseline_tight` | $39.31 | +$19.31 | 100% | 3 | MONTH top 30, consensus=3, $150 USDC — strict cohort |
| `auto_baseline_tight_microladder` | $36.12 | +$16.12 | 100% | 2 | Analyst-spawned variant of tight, micro TP ladder |
| **`claude_baseline_quick_exit` (LIVE)** | **$31.89** | **+$11.89** | **50%** | **4** | Defensive exits, currently on live |
| `claude_baseline_persist` | $31.70 | +$11.70 | 60% | 5 | Most balanced sample, persistence filter ON |
| `auto_baseline_sizeup` | $21.91 | +$1.91 | 0% | 0 | Unrealized only, 3 open positions winning |
| `claude_baseline_fresh` | $20.38 | +$0.38 | 50% | 2 | 30min lookback variant |
| `claude_strong_breakout` | $20.31 | +$0.31 | 50% | 6 | Race-style, the only race strategy in the green |

### Recommendation for live

**Stay on `claude_baseline_quick_exit`.** Reasons:
1. **Already profitable** (+59% ROI on $20 base, 4 closed)
2. **Defensive exit profile** = bounded downside while we wait for more sample
3. **Switching costs sample continuity** — every restart resets the journal
4. The leaders (`claude_baseline_tight` +97%, `auto_baseline_tight_microladder` +81%) have **tiny samples (2-3 trades)** — variance dominates. Could be statistical luck.
5. The race needs ≥30 closed trades on any candidate before `🎯 LIVE READY ✅` triggers in the analyst report. None there yet.

If forced to switch, the runner-up is `claude_baseline_persist` (5 closed, 60% wr, +$11.70) — it has the most balanced sample of the leaders. But the marginal upgrade isn't worth the switch right now.

### What's working — pattern across all top strategies

**The smart_money pipeline is the only thesis earning money.** All 4 leaderboard top performers (`claude_baseline_tight`, `auto_baseline_tight_microladder`, `claude_baseline_quick_exit`, `claude_baseline_persist`) are smart_money mode variants. Race-style bots (panic_fade, contrarian, momentum_*) mostly destroyed themselves and got auto-killed by the catastrophic-equity-halt (ROI ≤ -50%). The 31 archived strategies are almost all race-style.

**Key insight:** copy-trading on multi-wallet consensus + defensive exits beats every other thesis the race has tested. The differentiators between top performers are exit aggressiveness (let_run vs quick_exit) and cohort filter (tight vs wide vs persistence vs freshness). The smart_money cohort itself is the alpha source.

**Autonomous loop — see `scripts/dry_analyst.py`:**
- Runs as a sidecar alongside `bash scripts/run_both_dry.sh`
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
- **Live analyst (`scripts/live_analyst.py`)** — read-only sidecar wired into `run_live_70.sh`; reads paper_state + dry leaderboard, calls `claude` CLI, posts insights to `TELEGRAM_CHAT_ID_LIVE` every 30 min. Never spawns/modifies anything live.

**Universal exit/sizing rules** for race-style strategies:
- `stake_pct ≈ 0.10–0.25`, `max_orders_per_tick = 5`, `cash_floor_pct = 0.02`, `max_hours = 4.0` (hard 4h-only rule)
- Exits per profile: TP, SL, trailing, peak-protect (varies by strategy)
- Resolved exits at bid ≥ 0.97 (and now also ≤ 0.03 via universal sweep)
- Daily DD halt at -15% of starting equity (race + edge)

**Dry race composition:** ~60–130 strategies depending on auto_* spawns
- 13 base strategies (original 10 + cpanic + edge + random control)
- ~20 claude_* race variants
- 6 framework rules (hybrid_smart_money, smart_wallet_consensus, whale_entry_detection, etc.)
- 10 momentum_* (user's favorite thesis, distinct exit/sizing combos)
- 6 claude_baseline_* A/B variants (tight, wide, persist, fresh, quick_exit, let_run)
- `kzerlepgm_baseline` (main:baseline + 4h cap, real smart_money pipeline)
- N `auto_*` spawned dynamically by the analyst

**Recent code-level fixes (since 2026-05-15):**
- Auto-discover leaderboard (`pmbot leaderboard --auto-discover`) rebuilds run list from `data/dry_runs/*` each refresh — auto_* bots show up without restart
- Telegram leaderboard truncated to top 15 + bottom 5 (was exceeding 4096-char cap with 100+ strategies)
- Analyst plain-text fallback on HTTP 400; HTTPError body logged for diagnosis
- `_force_close_resolved_positions` runs in `strategy_loop` — universal across all strategy modes
- Analyst journal counter accepts both `event=position_closed` AND any entry with `closed_at` (race/smart_money/news use different field conventions)
- `claude_baseline_*` family + `momentum_*` family added
- Hard reset workflow: all profiles bumpable between $20/$100, tests adapt, backups kept in `data/backups_full_<ts>_<reset>/`

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
- `scripts/run_live_70.sh` — canonical live runner for ~$90 bankroll.
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

Use the canonical script to avoid copy-paste pitfalls:

```bash
bash scripts/run_live_70.sh
```

The script is the single source of truth for the live config. Current settings:

- Profile: `claude_baseline_quick_exit` (smart_money mode, defensive exits)
- `POLYMARKET_SYNC_LIVE_POSITIONS=1`, `POLYMARKET_AUTO_INTERVAL_SECONDS=10`
- Sizing (from `configs/profiles/claude_baseline_quick_exit.toml`): `position_pct=0.10`, `max_position_ceiling_usd=25`, `cash_floor_pct=0.05`, `min_open_positions=5`, `assumed_live_balance_usd=20`
- Trader cohort: leaderboard `WEEK`, top 50, `min_trader_pnl=$500`, `min_trader_volume=$1k`, `min_trader_roi=2%`. Concurrency 16.
- Entry filters: `min_consensus=2`, `min_copied_usdc=$50`, `max_chase_premium=0.12`, price band 0.03–0.97, absolute spread ≤10c, relative spread ≤35%, signal staleness ≤10 min, 4h hard cap.
- Three-pass scan per tick: strict → relaxed → deep fallback.
- Defensive exits (this is what differs from baseline): stop_loss -25%, peak-protect arms @+50% exits @+20%, trailing arms @+15% with 50% giveback, stop_loss_min_age 5min. SELLs rejected with "balance is not enough" trigger an automatic cancel of the resting CLOB order on that token and retry on the next tick.
- Live analyst sidecar (`scripts/live_analyst.py`) launches alongside; posts read-only insights every 30 min to `TELEGRAM_CHAT_ID_LIVE`.
- Universal sweep closes positions at price ≥0.97 OR ≤0.03 every tick across all strategy modes.
- BTC edge integrated: at the end of every smart-money tick `btc_edge_once` runs with $5/trade cap and 8% minimum modeled edge over market.
- Noise fallback: up to 4 trades at $10 each, **only when smart-money executed zero trades in the tick** AND (positions below `MIN_OPEN_POSITIONS` OR cash share above 35% of equity). Tagged `noise_fallback` in the journal.
- Auto-tune: `SMART_AUTO_TUNE_ENABLED=1` (paused below 30 closed trades; defensive only).

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
