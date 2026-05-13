# Claude Code Guide

Claude Code entry point for the Polymarket bot. See also the structured skill in `.claude/skills/polymarket-bot/SKILL.md`.

The project is MIT licensed (see `LICENSE`). Tests run in CI (GitHub Actions, see `.github/workflows/test.yml`).

## Safety

- Never reveal `.env` values, private keys, API secrets, or passphrases.
- Never run `pmbot auto-loop --live --yes` from a chat session. Live trading
  requires the user-initiated interactive prompt (or an explicit script
  invocation like `bash scripts/run_live_70.sh`). The `--yes` flag exists
  only for that script and automation.
- `POLYMARKET_DRY_RUN` and `POLYMARKET_ENABLE_LIVE_TRADING` env vars are
  no longer consulted. Use `--dry-run` or `--live` flags.
- Do not implement random or unfiltered live trades. The `noise_fallback` path is the only forced-trade lane and is hard-capped at $10/trade and 4 trades/tick.
- Preserve the local ledger `data/paper_state.json` unless the user explicitly asks for a reset.
- Preserve `data/trade_journal.jsonl` and `data/strategy_overrides.json` unless explicitly asked to reset them.
- No LLM call (Claude, Codex, anything else) in the scanning or trade-selection path. The scanner stays deterministic Python over Polymarket APIs.
- The bot does not have the capability to write or push source code on its own.

## Project map

- `polymarket_bot/main.py` — CLI commands and strategy loops. Tick orchestration, sizing helpers, trade-journal writer, `journal-stats` and `tune-strategy` commands.
- `polymarket_bot/mirror.py` — mirror-mode strategy: whale discovery, trade polling, eligibility filters, conviction sizing, buy/sell execution, daily/weekly drawdown limits.
- `polymarket_bot/smart_money.py` — legacy smart-money strategy: leaderboard fetching, parallel trade fetching, signal grouping, scoring, reverse-lookup.
- `polymarket_bot/dry_run_runs.py` — named dry-run lifecycle (`--run <name>`): isolated ledger, journal, tick state, mirror state, equity curve.
- `polymarket_bot/auto_tuner.py` — reads the trade journal each tick and computes bounded strategy overrides (defensive only — tightens after losses).
- `polymarket_bot/bitcoin.py` — BTC threshold edge model (Black-Scholes-from-volatility).
- `polymarket_bot/trading.py` — authenticated live BUY/SELL order placement, FOK market orders, GTC limit sells, balance/allowance checks, SDK client wrapper with `cancel_active_orders_for_token` (falls back from SDK v2 to legacy REST).
- `polymarket_bot/dashboard.py` — local real-time HTML dashboard at `http://127.0.0.1:8765`.
- `polymarket_bot/portfolio.py` — local ledger with cash, open positions, pending orders, and exit records.
- `polymarket_bot/gamma.py` — Gamma client (market scan + reverse-lookup by clob_token_ids).
- `polymarket_bot/strategy.py` — candidate ranking from Gamma payloads.
- `polymarket_bot/models.py` — shared dataclasses and parsing helpers.
- `polymarket_bot/profiles.py` — TOML profile loader, env-var injection, `ProfileConfig` dataclass.
- `polymarket_bot/live_confirm.py` — interactive live-trade confirmation prompt with full config recap.
- `scripts/run_live_70.sh` — canonical live runner for ~$90 bankroll.
- `tests/test_mirror.py` — tests for mirror mode filters, exits, daily pause.
- `tests/test_strategy.py` — tests covering scoring, sizing, exit plans, auto-tuner rules.
- `tests/test_dry_run_runs.py` — tests for named dry-run lifecycle and path isolation.
- `docs/PROFILES.md` — exhaustive TOML profile key reference (sections, types, defaults).
- `docs/STRATEGIES.md` — master document for all strategies and exit conditions.

## Commands

Run tests:

```bash
uv run python -B -m unittest discover -s tests
```

Quick CLI snapshots (read-only, no SDK calls):

```bash
uv run pmbot status              # mode, equity, open positions, journal path/count
uv run pmbot positions           # CLI table of open positions, sorted by PnL desc
uv run pmbot --version           # version
uv run pmbot doctor              # health check: .env, auth, endpoints, local state
```

`status` and `positions` automatically read the dry-run ledger when
the `--dry-run` flag is passed. Output is colorized on a TTY; `NO_COLOR=1`
disables ANSI codes, `POLYMARKET_FORCE_COLOR=1` forces them through pipes.

Dashboard:

```bash
uv run pmbot dashboard                          # reads live ledger
uv run pmbot dashboard --run mirror-whales      # reads a specific dry-run ledger
```

Trade-journal stats (per-bucket P&L, win rate, suggested tightenings):

```bash
uv run pmbot journal-stats
```

Run the auto-tuner manually (writes `data/strategy_overrides.json`):

```bash
uv run pmbot tune-strategy
```

Live mirror loop (interactive confirmation requested unless `--yes` is passed):

```bash
uv run pmbot auto-loop --live --profile copy-advanced
```

Dry-run mirror loop (simulates orders, isolated ledger per `--run`):

```bash
POLYMARKET_PAPER_BALANCE_USD=100 uv run pmbot auto-loop --profile copy-advanced --dry-run --run mirror-whales
```

In dry-run mode every BUY/SELL is short-circuited (no SDK call), live
position sync is skipped, and state is persisted to
`data/dry_runs/<run>/` so the live ledger stays untouched.

Quiet mode (one-line tick footer, suppresses leaderboard/trade/JSON chatter):

```bash
POLYMARKET_QUIET=1 uv run pmbot auto-loop --live --profile copy-advanced
```

## Recommended live command

```bash
uv run pmbot auto-loop --profile copy-advanced --live --yes
```

The `copy-advanced` profile is the reference config:

- `starting_cash = 1000.0`, `mode = "mirror"`.
- Static targets: `bossoskil1, Pestle, surfandturf, JewishNinja, swisstony, RN1, Oddn, anoin123, wan123, Talvez10`.
- Whale discovery enabled every 6h, min PnL $10k.
- Sizing: `copy_ratio = 0.20`, tiered up to 0.35 for $100k+ PnL whales. Per-position cap at 25% of equity, per-category cap at 50%.
- Entry filters: `min_target_stake_usd = $100`, `min_liquidity_usd = $10k`, `max_chase_premium = 0.05`, price band 0.02–0.98, max 12 open positions.
- Drawdown protection: daily -5%, weekly -12%.
- Order handling: FOK market BUYs, GTC limit SELLs. Stale GTC orders auto-cancelled via SDK v2 `get_open_orders`. Live positions + USDC cash synced from Polymarket each tick.
- Interval: 30s between ticks.

Dashboard at `http://127.0.0.1:8765` by default.

## Tick sequence (mirror mode)

Each tick prints structured progress to stdout, followed by a JSON summary. Order:

1. Load mirror state (`seen`, `last_ts`, daily/weekly anchors, discovered targets).
2. Run target discovery (leaderboard scan every 6h) → merge static + discovered targets.
3. Load local portfolio ledger.
4. _(live only)_ Sync live Polymarket positions: close stale local positions, import/update live positions, fetch on-chain pUSD balance via `read_pusd_balance` → update `portfolio.cash`.
5. Poll trades for every target wallet. Filter eligible: age, already-seen, min stake, buy price band.
6. Sort eligible trades chronologically (oldest first) → ensures SELL before BUY for same token.
7. Build exit candidates from open positions → mark-to-market.
8. Sync daily/weekly drawdown anchors from current equity.
9. Run exit waterfall: take-profit, trailing stop, peak-protect, stop-loss, cohort-sell, near-expiry, max-hold. GTC sell orders with `status: "live"/"delayed"` are recorded as exits immediately (position closed locally, cash credited). Stale GTC orders are auto-cancelled via SDK `get_open_orders`.
10. For each eligible trade: check no-market, daily-pause, overcrowded, chase premium, liquidity, category cap, max open positions, duplicate event/position.
11. Execute BUY via FOK market order. If `status: "delayed"` (unfilled), skip and retry next tick. If filled, record position.
12. Execute SELL via GTC limit order. `status: "live"/"delayed"` = exit recorded, position closed. Sync won't re-open it.
13. Persist mirror state + portfolio. Print JSON result, sleep `AUTO_INTERVAL_SECONDS`.

## Mirror strategy: whale copy-trading

The bot watches top Polymarket whales. When **multiple** profitable wallets buy the same token in a short window, that's the signal — copy with bounded sizing.

### Entry conditions

- Recent BUY trades from leaderboard wallets (discovered or static target list).
- Multi-wallet consensus: ≥2 whales buying the same token.
- Whale stake ≥ $100, whale PnL ≥ $10k (discovery threshold).
- Tradable market: ask within 0.02–0.98, chase premium ≤ 5%, liquidity ≥ $10k.
- No existing position on the same market/token. Sports capped at 50% category exposure.
- Max 12 open positions.
- Sizing: `min(base = whale_stake × copy_ratio, equity × 25%, size_usd cap, cash)`.

### Exits (run before every new entry)

1. **Take-profit ladder** — +25% sell 15%, +50% sell 25%, +100% sell 50%, +200% sell 25%, +300% sell 15%.
2. **Trailing stop** — arms at +25% peak, exits on 50% giveback while still positive.
3. **Peak-protect** — arms at +100% peak, exits on giveback to +40%.
4. **Stop-loss** — -40% after 15 min in position.
5. **Cohort sell** — if entry whales sold the token, follow.
6. **Near-expiry** — close winners at ≥+5% within 20 min of market close.
7. **Max-hold** — force-close at 24h.

### Mirror sell mechanics

Sells are placed as GTC limit orders. `status: "live"/"delayed"` means the order is on the CLOB book — the bot immediately records the exit (deducts shares, credits cash, closes position). On the next tick, `_sync_live_positions` skips re-opening positions that have exit records or a `pending_sell_order_id` flag, preventing duplicate sell attempts. If a sell is rejected with `"not enough balance"` (stale order exists), `cancel_active_orders_for_token` uses the SDK v2 `get_open_orders` endpoint to find and cancel the resting order, then retries next tick.

### Sizing

```
whale_stake × copy_ratio (default 0.20, tiered up to 0.35)
capped by → equity × max_position_pct (25%)
capped by → size_usd (hard cap $250)
capped by → available cash
```

### Defensive auto-tuner

Reads the trade journal each tick. From 30 closed trades on:

- Stop-loss > 40% of trades: tighten `MAX_CHASE_PREMIUM` ×0.80, `MAX_RELATIVE_SPREAD` ×0.85.
- Consensus=2 trades avg PnL < -$0.30 (≥20 sample): raise `MIN_CONSENSUS` to 3.
- Sports avg PnL < -$0.30 (≥15 sample): bump `SPORTS_SCORE_PENALTY` ×1.5.
- Win rate < 30%: raise `MIN_COPIED_USDC` ×1.5.
- Avg PnL < -$0.20: reduce `POSITION_PCT` ×0.75.

Defensive only: tightens after losses, never loosens after wins.

### Not guaranteed profit

The expected edge comes from copying strong public flow while avoiding bad execution. **This is not guaranteed profit.** No-signal / no-trade is a valid position.
