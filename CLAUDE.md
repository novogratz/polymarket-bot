# Claude Code Guide

Use this file as the Claude Code entry point for the Polymarket bot.

## Safety

- Never reveal `.env` values, private keys, API secrets, or passphrases.
- Do not bypass `POLYMARKET_ENABLE_LIVE_TRADING=1`.
- Do not implement random or unfiltered live trades. The `noise_fallback` path is the only forced-trade lane and is hard-capped at $5/trade and 2 trades/tick.
- Preserve the local ledger in `data/paper_state.json` unless the user explicitly asks to reset it.
- Preserve `data/trade_journal.jsonl` and `data/strategy_overrides.json` unless the user explicitly asks to reset them.
- Do not add Claude, Codex, or any LLM call to the trading scan path. Scanning and trade selection stay deterministic Python rules over Polymarket APIs.
- Do not give the bot the ability to commit or push source code on its own.

## Project Map

- `polymarket_bot/main.py`: CLI commands and strategy loops. Owns `smart_money_once` (the per-tick orchestrator), sizing helpers, the trade-journal writer, and the `journal-stats` / `tune-strategy` commands.
- `polymarket_bot/smart_money.py`: leaderboard fetching, parallel trade fetching, signal grouping, scoring, reverse-lookup helper.
- `polymarket_bot/auto_tuner.py`: reads the trade journal each tick and computes bounded strategy overrides (defensive only — tightens after losses).
- `polymarket_bot/bitcoin.py`: BTC threshold edge model (Black-Scholes-from-volatility).
- `polymarket_bot/trading.py`: authenticated live BUY/SELL order placement and final stake computation.
- `polymarket_bot/dashboard.py`: local real-time HTML dashboard at `http://127.0.0.1:8765`.
- `polymarket_bot/portfolio.py`: local ledger with cash, open positions, pending orders, and exit records.
- `polymarket_bot/gamma.py`: Gamma client (market scan + reverse-lookup by clob_token_ids).
- `polymarket_bot/strategy.py`: candidate ranking from Gamma payloads.
- `polymarket_bot/models.py`: shared dataclasses and parsing helpers.
- `scripts/run_live_70.sh`: canonical live runner for ~$90 bankroll.
- `tests/test_strategy.py`: 49 tests covering scoring, sizing, exit plans, auto-tuner rules.

## Development Workflow

Run tests:

```bash
python3 -B -m unittest discover -s tests
```

Run the dashboard:

```bash
python3 -B -m polymarket_bot.main dashboard
```

Inspect the trade journal (per-bucket P&L, win rate, suggested tightenings):

```bash
python3 -B -m polymarket_bot.main journal-stats
```

Run the auto-tuner once manually (writes `data/strategy_overrides.json`):

```bash
python3 -B -m polymarket_bot.main tune-strategy
```

Run the autonomous smart-money loop (default scan path):

```bash
POLYMARKET_ENABLE_LIVE_TRADING=1 python3 -B -m polymarket_bot.main auto-loop
```

## Recommended Live Command

Use the canonical script to avoid copy-paste issues:

```bash
bash scripts/run_live_70.sh
```

The script is the single source of truth for the production env. It currently sets:

- `POLYMARKET_ASSUME_LIVE_BALANCE_USD=90`, `POLYMARKET_SYNC_LIVE_POSITIONS=1`.
- Sizing: `POSITION_PCT=0.18`, `MAX_POSITION_CEILING_USD=150`, `CASH_FLOOR_PCT=0.05` (drives ~95% deployment), `MIN_OPEN_POSITIONS=7`.
- Trader cohort: leaderboard `MONTH` window, top 100, `MIN_TRADER_PNL=$1k`, `MIN_TRADER_VOLUME=$2k`, `MIN_TRADER_ROI=3%`. Pulled in parallel with `TRADE_FETCH_CONCURRENCY=24`.
- Discovery: standard Gamma scan + keyword scan + reverse-lookup of top 100 tokens with $50+ smart-money flow not already in scan (`REVERSE_LOOKUP_*`).
- Entry filters: `MIN_CONSENSUS=2`, `MIN_COPIED_USDC=$75`, `MAX_CHASE_PREMIUM=0.13`, price band 0.03–0.96, abs spread ≤8c, relative spread ≤45%, signal staleness ≤10 min.
- Three-pass scan per tick: strict → relaxed (consensus floor) → deep fallback (consensus=1, looser, $50 min copied).
- Exits: take-profit ladder `1.0:0.50,2.0:0.25,3.0:0.15`, peak-protect at +100% triggering at +40% giveback, trailing stop arming at +25% with 50% giveback, stop-loss at -40% (after 15 min in position), cohort-sell exit (active SELL detection in 120 min lookback) and cohort-silent fallback, near-expiry exit at +5% within 20 min of close.
- BTC edge integrated: at the end of every smart-money tick, `btc_edge_once` runs with $5/trade cap and 8% minimum modeled edge over market.
- Noise fallback: when all three smart-money scans return 0 and open positions < `MIN_OPEN_POSITIONS`, up to 2 trades at $5 each are placed on top-scored candidates. Tagged `noise_fallback` in the journal so the bleed can be measured.
- Auto-tune: `SMART_AUTO_TUNE_ENABLED=1` (paused until 30 closed trades; defensive only).

The dashboard is served at `http://127.0.0.1:8765` by default.

## Tick Sequence

Each tick prints structured progress to stdout, then a JSON summary at the end. The order:

1. Auto-tune: read journal, compute overrides if ≥30 closed trades, apply on top of env-var settings.
2. Load Gamma markets (scan + keyword scan).
3. Sync live Polymarket positions into the local ledger.
4. Refresh live USDC cash from CLOB.
5. Cohort-exit detection (active SELL by entry wallets, or no fresh BUYs).
6. Sell strategy: take-profit ladder, trailing stop, peak-protect, stop-loss, cohort exits, near-expiry.
7. Smart-money scan: strict → relaxed → deep fallback. Reuses one leaderboard+trades fetch.
8. Reverse-lookup of high-flow tokens not in current candidates; merge into eligible pool.
9. Place trades from opportunity list with dynamic per-slot sizing toward the cash floor.
10. Noise fallback (if enabled and below `MIN_OPEN_POSITIONS`).
11. BTC edge tick (if enabled).
12. Persist portfolio + write journal entries for any closed positions.
13. Print JSON result, sleep `AUTO_INTERVAL_SECONDS`.

## Strategy To Make Money

The default strategy is smart-money copy trading. The bot does not invent an opinion on every market — it waits for public order-flow evidence that profitable wallets are buying the same token, then mirrors that flow with bounded sizing.

Live entries require:

- Recent BUY trades from leaderboard wallets that pass PnL, volume, and ROI floors.
- Multi-wallet consensus on the same token (relaxed in fallback passes when below the open-positions target).
- Enough copied USDC to matter, scaled with conviction tier.
- A tradable market: tight absolute and relative spread, ask within configured price band, not too close to expiry.
- No existing open position for the same market or token; sports markets respect a per-event concentration cap.
- Explicit `POLYMARKET_ENABLE_LIVE_TRADING=1`.
- Conviction-weighted sizing: weak signals stay near the floor; very-high-conviction signals (5+ wallets, $5k+ copied) can scale up to 2.5× the base position percentage, still capped by the per-position ceiling.

Exits run before new entries:

- Take-profit ladder (default +100%/+200%/+300%, partial sells).
- Trailing stop arms at +25% peak, exits on 50% giveback while still positive.
- Peak-protect arms at +100% peak, exits on giveback to +40%.
- Stop-loss at -40% after 15 minutes in position (does not fire if peak-protect already armed).
- Cohort-sell exit if any entry wallet has SOLD the token within the lookback window; cohort-silent exit if none of the cohort has re-bought.
- Near-expiry positive-PnL exit within 20 minutes of close at ≥+5%.

The expected edge comes from copying strong public flow while avoiding bad execution. **This is not guaranteed profit.** No-signal/no-trade is part of the strategy. The auto-tuner is defensive only — it tightens filters after losses but does not loosen them after wins, because that would amplify variance.
