# Claude Code Guide

Claude Code entry point for the Polymarket bot. See also the structured skill in `.claude/skills/polymarket-bot/SKILL.md`.

The project is MIT licensed (see `LICENSE`). Tests run in CI (GitHub Actions, see `.github/workflows/test.yml`).

## New machine / fresh account setup (2026-05-30)

1. **Install uv**: `curl -LsSf https://astral.sh/uv/install.sh | sh` → open a new terminal.
2. **Install v2 SDK**: `uv add py-clob-client-v2` — required since Polymarket CLOB v2 (old SDK gives `order_version_mismatch`).
3. **Create `.env`** from `.env.example`. Critical fields:
   - `POLYMARKET_SIGNATURE_TYPE=3` — all new accounts (2026+) use the deposit wallet flow (POLY_1271), not POLY_PROXY (type 1).
   - `POLYMARKET_FUNDER_ADDRESS` — your wallet address as shown on polymarket.com profile page.
   - `POLYMARKET_PRIVATE_KEY` — your EOA private key (the key that controls the deposit wallet).
   - `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID_LIVE` — create bot via @BotFather, get chat_id from `getUpdates` after messaging the bot.
4. **Generate API credentials**:
   ```bash
   uv run python -c "
   from py_clob_client_v2.client import ClobClient
   c = ClobClient('https://clob.polymarket.com', chain_id=137, key='<PRIVATE_KEY>', signature_type=3, funder='<FUNDER_ADDRESS>')
   creds = c.create_or_derive_api_key()
   print('KEY:', creds.api_key)
   print('SECRET:', creds.api_secret)
   print('PASS:', creds.api_passphrase)
   "
   ```
5. **Approve CLOB allowance** (first time only):
   ```bash
   uv run python -c "
   from py_clob_client_v2.client import ClobClient
   from py_clob_client_v2.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
   creds = ApiCreds(api_key='...', api_secret='...', api_passphrase='...')
   c = ClobClient('https://clob.polymarket.com', chain_id=137, key='<PRIVATE_KEY>', creds=creds, signature_type=3, funder='<FUNDER_ADDRESS>')
   c.set_api_creds(creds)
   c.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
   "
   ```
6. **Make one manual trade on polymarket.com** — new accounts must place at least one trade through the UI to register the maker address with the CLOB. Without this, all API orders fail with `maker address not allowed, please use the deposit wallet flow`.
7. Run: `bash scripts/run_live_70.sh`

## Current state snapshot (2026-05-29)

**Live strategy:** `grinder` — heavy-favorite, ride-to-resolution. Single source-of-truth profile at `configs/profiles/grinder.toml`.

- Engine: `race` (selector = `select_grinder` in `polymarket_bot/race_strategies.py`)
- Thesis: buy a binary outcome at bid 0.87–0.95 within 4h of close, hold until bid ≥ 0.99. The edge is the implied-probability gap between the current bid and the binary outcome resolving at 1.0. No stop-loss — the exclusion filters, price-stability gate, and momentum filter are the risk controls.
- Bankroll: **$75.02 USDC** (2026-05-30 — down from $123 deposit, Vissel Kōbe O/U 4.5 Over resolved causing $58 loss).
- Sizing: **all-in (95%)**, one position at a time. `race_stake_pct=0.95`, `max_orders_per_tick=1`, `cash_floor_pct=0.02`. Each win = ~+6% on account. Stake scales automatically with bankroll. Second slot removed — idle cash was sitting at 50% when only one qualifying market existed.
- Entry filters (in `_build_eligible_candidates`):
  - `race_min_price=0.87`, `race_max_price=0.95`
  - `race_max_hours=4.0`
  - `race_max_spread=0.02`
  - `race_min_liquidity_usd=500`, `race_min_volume_24h_usd=300`
  - `race_max_day_change_pct=0.10` — price-stability gate: skip markets that moved >10% today (live-game gap risk)
  - `race_min_outcome_momentum=-0.05` — momentum filter: skip outcomes that fell >5% today (trending away from resolution)
- Global exclusions (`models.py:is_excluded_market`): crypto Up/Down binaries, esports (CS/valorant/league of legends + the "LoL:" title prefix/dota/BO1-BO5), temperature/weather (°C + °F), exact score, O/U 0.5/1.5/2.5/3.5 (low-line — any goal kills it), O/U 5.5/6.5/7.5, halftime leading/score, **Spread:** (Asian handicap — gap identical to exact-score), draw markets ("end in a draw", "win or draw")
- Exits: resolved_exit at bid ≥ 0.99, universal sweep at bid ≤ 0.03, max-hold 4.5h. No TP ladder, no SL.
- Daily DD halt at -15% of starting equity (`POLYMARKET_RACE_DAILY_DRAWDOWN_PCT`)
- Tick interval: 10s on live (was 30s — 3× faster, catches more fleeting entries), 600s on dry twin
- Selector ranking: `score = best_bid / max(hours_to_close, 1/60)`

**Bankroll:** $123 USDC. State backups in `data/backups_reset_<timestamp>/`.

**Why no stop-loss:** SL can't catch gap moves (a soccer exact-score "No" at 0.94 can gap to 0.44 in one tick when a goal is scored — the SL fires at 0.80 but execution is at 0.44). The exclusion filters prevent entering these market types entirely. For markets that do pass the filters, catastrophic flips are rare enough that no SL is the correct policy.

**Why 50% stake, not all-in:** at 95% stake, one bad outcome wipes the account. At 50% stake, a single loss hurts but one follow-up win recovers most of it.

**Momentum filter:** `race_min_outcome_momentum=-0.05` blocks outcomes falling >5% today. A market at 0.91 that was 0.96 this morning is moving *away* from resolution — worse edge than one sitting stably at 0.91. The stability gate (abs 10%) wasn't catching directional decline within that range.

**Binary arb scanner:** every tick a second pass scans all markets for `YES_ask + NO_ask < 0.97` using actual CLOB ask prices (BUY side). When found, both legs are opened — guaranteed 3%+ profit regardless of outcome. Sizing uses **proportional stakes**: face value P where YES_stake = P×YES_ask and NO_stake = P×NO_ask, so the guaranteed payout is P regardless of which side wins. P is sized so the larger leg ≤ `race_arb_max_stake_usd` ($5 cap). Previous bugs: (1) used SELL-side (bid) prices — false signals; (2) used equal dollar stakes — net loss when the expensive/likely leg won. Both fixed. Arb positions are tagged `is_arb=True`, skip TP/SL, and ride to resolution. **Currently disabled in grinder.toml** (`arb_threshold=0.0`) — set to 0.97 to re-enable.

**Realistic performance:** 5–7%/day on active days. With 50% stake and wider band, 2 wins = ~9%, 3 wins = ~13%. Weekly target 20–30%. 10%+ days happen often on active markets; the constraint is finding 2–3 qualifying markets per day, not the per-trade math.

### Unified launcher — `scripts/run_all.sh`

Single command boots the whole stack with shared HTTP cache:

```bash
bash scripts/run_all.sh
```

Order of operations:
1. **Pre-warm HTTP cache** (~60s) via `scripts/cache_warmer.py` — populates `data/cache/http/` with leaderboards (3 windows × 8 categories × 4 limits) + the top wallets' recent trade histories so the bot swarm starts with a warm cache and no first-tick 429 storm.
2. **Live bot** (`grinder`, 30s tick) + `scripts/live_analyst.py` sidecar (30min Telegram report).
3. **Dry race** — auto-discovers all profiles from `configs/profiles/*.toml`. Post-2026-05-25 reset there's only `grinder.toml`, so the dry race launches a single grinder dry-twin at `POLYMARKET_AUTO_INTERVAL_SECONDS=600`, Telegram BUY/SELL alerts silenced per-subshell so only the live bot speaks. If `dry_analyst.py` spawns `auto_*` variants over time the dry race expands accordingly.
4. **Sidecars** — `scripts/dry_analyst.py` (15min report / 1h spawn-kill) + `pmbot leaderboard --telegram` (5min summary).
5. **Background re-warmer** — re-runs `cache_warmer.py` every 8 min (cache TTL is 10 min) to keep both live + dry continuously warm.

`Ctrl+C` cleans up the whole process group. Trap is on `INT/TERM` only (NOT `EXIT`) and the `cleanup()` is idempotent via a `CLEANED_UP=1` guard — a previous bug had `set -u` crash on an unset var triggering the EXIT trap and killing every bot.

### HTTP cache layer

`polymarket_bot/smart_money.py:_get_json` wraps every data-api request behind a sha1-keyed disk cache at `data/cache/http/`. TTL defaults to 600s (env override `POLYMARKET_HTTP_CACHE_TTL_SECONDS`). With ~50 dry bots each previously firing their own `leaderboard()` + `trades()` calls, the load to data-api was ~2,500 calls/min and 70%+ failed with 429. With the shared cache that drops to ~33 calls/min from the cache-warmer's single pass.

### Recommendation framework for live

Promotion threshold for moving a dry profile to live: **≥10 closed trades AND ROI > 0** as a soft floor; `🎯 LIVE READY ✅` only at ≥30 closed. Below 10 closed = variance, not edge. Catastrophic halt (any strategy with ROI ≤ -50% regardless of sample) auto-archives the profile.

The dry-analyst `_pick_favorite` returns wording "Top of N profitable strategies" when N > 1 — never lies about "only profitable strategy" when several are positive.

**Autonomous loop — see `scripts/dry_analyst.py`:** (NO AI — deterministic only, 2026-05-26)
- Runs as a sidecar alongside `bash scripts/run_all.sh`
- **Report every 15 min** to `TELEGRAM_CHAT_ID_DRY_RUN`: full leaderboard with $start → $current / +/- $ / ROI% / WR / closed / open per strategy, top 3 trades + open positions for the favorite, plus a tiered live-readiness recommendation. The "Insights" narrative is built deterministically from the metrics — no LLM call. Open-position lines include the side and close ETA so the ongoing market is visible.
- **Spawning/tuning removed.** The analyst no longer generates or rerolls strategies (that path was LLM-driven via Codex/Ollama and has been deleted). It only reports and prunes clear losers.
- **Loser-kill pass every 1 hour** (deterministic thresholds, decoupled from report rhythm):
  - Kills underperformers: ROI ≤ -25% AND wr ≤ 30% AND n ≥ 25 (auto) / n ≥ 50 (human)
  - Catastrophic halt: ROI ≤ -30% kills any bot regardless of trade count (relaxed from -50% to give strategies more room)
  - Killed profile → `configs/profiles/_archived/<name>_<ts>.toml` (recoverable)
- **Universal sweep** every tick across all strategies (live + dry):
  - Force-close winners at `current_price ≥ 0.97`
  - Force-close losers at `current_price ≤ 0.03`
  - Catches resolved markets that drop out of Gamma scans before per-strategy exit logic fires
- **Telegram fallback:** `_default_transport` retries with `parse_mode` stripped on HTTP 400, so MarkdownV2 escape failures never silently swallow alerts.
- **Live analyst (`scripts/live_analyst.py`)** — read-only sidecar in `run_live_70.sh`; reads paper_state + realized_trade_cache, posts a deterministic LIVE-ONLY executive-summary (open positions w/ entry→cur→PnL, top closed) to `TELEGRAM_CHAT_ID_LIVE` every 30 min. NO AI, no dry comparison. Never spawns/modifies anything live.

**Universal exit/sizing rules** for race-style strategies:
- `stake_pct ≈ 0.10–0.25`, `max_orders_per_tick = 5`, `cash_floor_pct = 0.02`, `max_hours = 4.0` (hard 4h-only rule)
- Exits per profile: TP, SL, trailing, peak-protect (varies by strategy)
- Resolved exits at bid ≥ 0.97 (and now also ≤ 0.03 via universal sweep)
- Daily DD halt at -15% of starting equity (race + edge)

**Dry race composition:** auto-discovered from `configs/profiles/*.toml` (95 profiles) by both `scripts/run_all.sh` and `scripts/run_both_dry.sh`. Skips special profiles `copy-wallet` and `live-90`. Covers every thesis family across the restored archive + the 7 always-active profiles. Live and dry use separate state files (`paper_state.json` vs `data/dry_runs/<name>/state.json`) so they don't conflict.

**Recent code-level fixes (since 2026-05-15):**
- HTTP cache layer in `smart_money.py:_get_json` (TTL 600s) + pre-warm via `scripts/cache_warmer.py`, re-warm loop every 8min in `run_all.sh`
- `scripts/run_all.sh` — single launcher for live + dry race + sidecars + cache pre-warm + re-warm loop. Trap on INT/TERM only (not EXIT), idempotent cleanup, no `set -u` (crashed on harmless unset vars)
- `_force_close_resolved_positions` runs in `strategy_loop` — universal across all strategy modes
- `live_available_balance` smart fallback: when pUSD RPC fails, reads ledger cash and caps by `assume - sum(open_positions_cost)`; defends against live-sync importing positions without debiting cash. RPC failure log throttled to once per 5 min.
- Per-position sizing bug fixed (`ceiling = min(...)` was `max(...)`, allowed $25 BUY on $29.90 bankroll)
- Live analyst now sets `POLYMARKET_PROFILE_LABEL` BEFORE the sidecar spawns (else logs "(unknown)")
- `load_live_snapshot` prefers `current_price × shares` for equity calculation, falls back to size_usd → notional_usd → stake → cost_basis
- Telegram leaderboard truncated to top 15 + bottom 5, all plain text (no MarkdownV2 escape literals)
- Dry-bot Telegram alerts silenced per-subshell via `TELEGRAM_ALERT_*=0` env vars in `run_dry_bot()` and `TELEGRAM_CHAT_ID_DRY_RUN=""` on `dry_analyst.py` — only the live bot speaks, in both `run_live_70.sh` and `run_all.sh`
- `_pick_favorite` tier 3 wording: "Top of N profitable strategies" when N > 1 (was always "Only profitable", which lied)
- Analyst journal counter accepts both `realized_pnl_usd` (sweep) and `realized_pnl` (race/smart_money)
- Hard reset workflow: backups kept in `data/backups_full_<ts>_<reset>/`
- **Arb sizing fixed (2026-05-29):** was equal dollar stakes per leg → net loss when favorite wins. Now proportional: face value P sized so larger leg ≤ $5 cap; guaranteed payout P regardless of outcome.
- **Heartbeat display fixed (2026-05-29):** headline now shows "P&L vs start" (equity − starting_cash) instead of cumulative realized PnL %. Cumulative realized includes pre-deposit losses that inflate the negative % while equity is actually up.

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
- No LLM call (Claude, Codex, anything else) in the scanning or trade-selection path. The scanner stays deterministic Python over Polymarket APIs. (The `auto-improve` self-tuner runs OFFLINE and never enters the live trade loop — see below.)
- Source-code autonomy is bounded and opt-in (2026-06-05). `scripts/auto_improve.py` + `.github/workflows/auto-improve.yml` use the Claude Code CLI to open PRs that tune the LIVE `grinder.toml`, then auto-merge once CI is green. It may ONLY change EXIT/SIZING knobs (`tp_pct`, `stake_pct`, `max_orders_per_tick`, `resolved_exit_threshold`, `max_hold_hours`), each hard-clamped. The ENTRY/bet-selection filters (price band, spread, hours, day-change, momentum, liquidity/volume) are FROZEN — `_audit_frozen` aborts if they move — so the win rate is protected. A stop-loss can NEVER be introduced (`sl_pct`/`stop_loss_pct` not tunable; honours "never sell losing positions"). It never edits any other file (`grinder_b.toml`, kzer profile, `.env`, source). This is the one sanctioned exception to "no LLM / no self-written source" — it runs OFFLINE, never in the live trade loop. Full design + switches in `docs/AUTONOMY.md`.

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
- `scripts/run_live_70.sh` — **canonical launcher (live only)**: live grinder bot + live analyst + live-only leaderboard. Loads `configs/profiles/grinder.toml`, $123 bankroll. Does NOT reset the ledger/journal.
- `scripts/run_all.sh` — legacy live+dry launcher. **Do not use for live** — it resets the ledger on startup and runs the retired dry race.
- `scripts/cache_warmer.py` — pre-fetches leaderboards + wallet trade histories into `data/cache/http/` so the bot swarm starts warm.
- `scripts/dry_analyst.py` — autonomous analyst sidecar: 15min deterministic reports + 1h deterministic loser-kill pass. No AI/LLM (spawning/tuning removed 2026-05-26).
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

```bash
bash scripts/run_live_70.sh
```

Boots: live grinder (30 s tick) + live analyst sidecar (30 min Telegram) + live-only leaderboard (5 min Telegram) + dry grinder twin (paper, 10 min tick, Telegram silenced) + autonomous dry analyst (15 min report, deterministic). No AI anywhere.

**Do NOT use `run_all.sh` for live trading** — it resets the ledger on startup and launches the retired 95-profile dry race.

`run_live_70.sh` loads `configs/profiles/grinder.toml`. Current settings:

- `POLYMARKET_SYNC_LIVE_POSITIONS=1`, `POLYMARKET_AUTO_INTERVAL_SECONDS=30`
- Bankroll: **$123** (2026-05-29). `starting_cash=123.0`, `assumed_live_balance_usd=123.0`.
- Sizing: `race_stake_pct=0.50`, `max_orders_per_tick=2`, `cash_floor_pct=0.05`
- Entry: `race_min_price=0.87`, `race_max_price=0.95`, `race_max_hours=4.0`, `race_max_spread=0.02`, `race_max_day_change_pct=0.10`
- Exits: resolved_exit at bid ≥0.99, universal sweep at ≤0.03, max-hold 4.5h. No TP, no SL.
- Universal sweep closes positions at price ≥0.97 OR ≤0.03 every tick.

Dashboard at `http://127.0.0.1:8765`.

## Tick sequence

Each tick prints structured progress to stdout, followed by a JSON summary. Order:

1. Auto-tune: read journal, compute overrides if ≥30 closed trades, apply on top of env-var settings.
2. Load Gamma markets (scan + keyword scan).
3. Sync live Polymarket positions into the local ledger.
4. Refresh live USDC cash from CLOB.
5. Cohort-exit detection (active SELL by entry wallets, or no fresh BUYs).
6. Sell strategy: take-profit ladder, trailing stop, peak-protect, cohort exits, near-expiry, max-hold-time. No stop-loss exits in the current grinder/live stack.
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
- **Open drawdown** — a loser bleeding slowly. The current grinder/live stack relies on cohort, resolved, and max-hold exits rather than stop-loss.
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
