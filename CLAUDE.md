# Claude Code Guide

Claude Code entry point for the Polymarket bot. See also the structured skill in `.claude/skills/polymarket-bot/SKILL.md`.

The project is MIT licensed (see `LICENSE`). Tests run in CI (GitHub Actions, see `.github/workflows/test.yml`).

## Safety

- Never reveal `.env` values, private keys, API secrets, or passphrases.
- Do not bypass `POLYMARKET_ENABLE_LIVE_TRADING=1`.
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

## Development workflow

Run tests:

```bash
uv run python -B -m unittest discover -s tests
```

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

Live smart-money loop:

```bash
POLYMARKET_ENABLE_LIVE_TRADING=1 uv run pmbot auto-loop
```

## Recommended live command

Use the canonical script to avoid copy-paste pitfalls:

```bash
bash scripts/run_live_70.sh
```

The script is the single source of truth for the live config. Current settings:

- `POLYMARKET_ASSUME_LIVE_BALANCE_USD=90`, `POLYMARKET_SYNC_LIVE_POSITIONS=1`.
- Sizing: `POSITION_PCT=0.18`, `MAX_POSITION_CEILING_USD=150`, `MAX_POSITION_CEILING_PCT=0.30`, `CASH_FLOOR_PCT=0.05` (~95% deployment), `MIN_OPEN_POSITIONS=7`.
- Trader cohort: leaderboard `MONTH`, top 100, `MIN_TRADER_PNL=$1k`, `MIN_TRADER_VOLUME=$2k`, `MIN_TRADER_ROI=3%`. Parallel fetch with `TRADE_FETCH_CONCURRENCY=24`.
- Discovery: standard Gamma scan + keyword scan + reverse-lookup of the top 100 tokens with $50+ smart-money flow that aren't already in the scan.
- Entry filters: `MIN_CONSENSUS=2`, `MIN_COPIED_USDC=$75`, `MAX_CHASE_PREMIUM=0.13`, price band 0.03–0.96, absolute spread ≤8c, relative spread ≤45%, signal staleness ≤10 min.
- Three-pass scan per tick: strict → relaxed → deep fallback.
- Exits: take-profit ladder `0.25:0.15,0.5:0.25,1.0:0.50,2.0:0.25,3.0:0.15`, peak-protect arming at +100% and exiting below +40%, trailing stop arming at +25% with 50% giveback, stop-loss -40% (after 15 min in position), resolved-market exit when bid ≥ 0.97, max-hold-time 24h, cohort-sell exit (active SELL detection in 120 min lookback, parallel fetch), near-expiry positive exit. SELLs that are rejected with "balance is not enough" trigger an automatic cancel of the resting CLOB order on that token and retry on the next tick.
- BTC edge integrated: at the end of every smart-money tick `btc_edge_once` runs with $5/trade cap and 8% minimum modeled edge over market.
- Noise fallback: up to 4 trades at $10 each when all three smart-money scans return 0 AND (positions below `MIN_OPEN_POSITIONS` OR cash share above 35% of equity). Tagged `noise_fallback` in the journal.
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
10. Noise fallback if enabled and either below `MIN_OPEN_POSITIONS` or cash share above the cash-pressure threshold.
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
- Explicit `POLYMARKET_ENABLE_LIVE_TRADING=1`.
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
