# polymarket-bot

[![tests](https://github.com/novogratz/polymarket-bot/actions/workflows/test.yml/badge.svg)](https://github.com/novogratz/polymarket-bot/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

Polymarket smart-money copy-trading bot with a local dashboard, persistent ledger, trade journal, defensive auto-tuner, and an optional BTC edge model. The default strategy watches recent buys from profitable leaderboard wallets, requires multi-wallet consensus on the same token, applies tight execution filters (absolute spread, relative spread, price band, freshness, chase premium), then sizes each trade as a percentage of the bankroll weighted by signal conviction. Exits run before each new entry: take-profit ladder, trailing stop, peak-protect, stop-loss, cohort-sell.

> ⚠️ **Financial disclaimer.** This bot places real-money trades when configured to do so. It is not financial advice and there is no guarantee of profit. The user assumes all risk. Read the [Disclaimer](#disclaimer) section before running it live.

## Install

From source, in development mode:

```bash
python3 -m pip install -e .
```

Or just runtime dependencies:

```bash
python3 -m pip install -r requirements.txt
```

With dev tools (ruff):

```bash
python3 -m pip install -e ".[dev]"
```

Configure your wallet and API credentials in `.env` at the project root. Use `.env.example` as a template. See [API credentials](#api-credentials) for the required keys.

## Run live

Preferred (live + dry race + sidecars + shared HTTP cache, all in one):

```bash
bash scripts/run_all.sh
```

Auto-discovers all 95 profiles in `configs/profiles/` for the dry race — includes every restored archive strategy.

Or live alone (no dry race, no cache pre-warm):

```bash
bash scripts/run_live_70.sh
```

Both scripts read `configs/profiles/claude_baseline_let_run.toml` as the live config. Run in the foreground; `Ctrl+C` to stop the whole process group.

## CLI commands

```bash
uv run pmbot --version           # print pmbot version and exit
uv run pmbot status              # quick snapshot: mode, equity, open positions, journal
uv run pmbot positions           # CLI table of open positions, sorted by PnL desc
uv run pmbot auto-loop           # live loop (what the script invokes)
uv run pmbot dashboard           # local dashboard at http://127.0.0.1:8765
uv run pmbot doctor              # read-only health check (.env, auth, endpoints, local state)
uv run pmbot journal-stats       # aggregate stats from the trade journal
uv run pmbot tune-strategy       # run the auto-tuner manually
uv run pmbot bootstrap-creds     # derive CLOB credentials from the wallet
uv run pmbot reset-ledger        # rebuild the local ledger from live state
```

The `pmbot` console script is registered via `[project.scripts]` in
`pyproject.toml`. After `uv tool install -e .` it can be invoked directly as
`pmbot <command>` from any directory; `python3 -B -m polymarket_bot.main <command>`
remains supported as a fallback.

`status` and `positions` are read-only and never call the SDK — they only
read `data/paper_state.json` (or `data/dry_run_state.json` under
`POLYMARKET_DRY_RUN=1`). Output is colorized when stdout is a TTY; set
`NO_COLOR=1` to disable ANSI codes, or `POLYMARKET_FORCE_COLOR=1` to keep
them when piping.

## Dry-run mode

To watch the smart-money loop run end-to-end on real Polymarket data
without spending any cash, set `POLYMARKET_DRY_RUN=1` instead of
`POLYMARKET_ENABLE_LIVE_TRADING=1`:

```bash
POLYMARKET_DRY_RUN=1 uv run pmbot auto-loop
```

In dry-run mode the bot:

- Bypasses the live-trading guard so the loop starts.
- Short-circuits every CLOB BUY and SELL — the SDK call is skipped and a
  `{"success": True, "status": "matched", "dry_run": True}` response is
  injected so the rest of the pipeline (sizing, ledger writes, exits)
  runs identically to a real fill.
- Skips live position sync (the dry-run ledger is the source of truth).
- Writes state to `data/dry_run_state.json` and trades to
  `data/dry_run_journal.jsonl` so your real paper-trading ledger and
  journal are not polluted.

Reset the dry-run ledger with `rm data/dry_run_state.json` and run the
loop again. `POLYMARKET_DRY_RUN=1 uv run pmbot doctor` prints the swap
and verdict so you can confirm the simulation is correctly wired.

## Quiet output

Set `POLYMARKET_QUIET=1` to compress each tick to a one-line summary
(`▶ tick start` plus a readable footer line, with one extra indented
`→` line per executed BUY / SELL / NOISE / BTC trade — capped at 6,
then `+N more action(s)`). Quiet mode suppresses the per-leaderboard
pulls, parallel trade fetch progress, reverse-lookup chatter,
balance-check banner, and the BUY/SELL JSON response dumps while still
printing one-line `🚀 BUY` / `💸 SELL` records, errors, and warnings.
The full tick payload is no longer printed in quiet mode — switch back
to verbose if you need the raw JSON. Combine with dry-run for a
minimal simulation feed:

```bash
POLYMARKET_DRY_RUN=1 POLYMARKET_QUIET=1 uv run pmbot auto-loop
```

## Winning strategy

### The edge

The hypothesis: wallets at the top of monthly Polymarket leaderboards with positive PnL and meaningful volume have, on average, an informational or analytical edge on the markets they trade. When several of those wallets buy the same token in a short window (30 minutes), the collective signal is stronger than a single wallet. The bot mirrors that flow with bounded sizing.

Risks the strategy explicitly avoids:

- **Fake edge** — one lucky wallet on an isolated trade. Filtered by trader ROI / volume floors and multi-wallet consensus.
- **Bad execution** — paying the full spread erases the entire edge. Filtered by absolute spread, relative spread, max chase premium.
- **Concentration** — six bets on the same event. Filtered by per-market dedupe and per-event-slug dedupe for sports.
- **Round-trip to flat** — a winner that gives back to zero. Filtered by take-profit ladder + trailing stop + peak-protect.
- **Drawdown without exit** — a loser that bleeds slowly. Filtered by stop-loss after a minimum hold age.
- **Cohort flip** — entry wallets actively selling. Filtered by cohort-sell detection (reads cohort SELL trades within the lookback window).

### Entry conditions

- Recent BUY trades from leaderboard wallets that pass the PnL ($1k+), volume ($2k+), and ROI (3%+) floors.
- Multi-wallet consensus on the same token (relaxed in fallback passes when below the open-positions target).
- Enough copied USDC to matter, scaled by conviction tier.
- Tradable market: tight spreads (absolute ≤8c, relative ≤45%), ask within 0.03–0.96, not too close to expiry.
- No existing open position on the same market or token. Sports respect a per-event concentration cap.
- Explicit `POLYMARKET_ENABLE_LIVE_TRADING=1`.

### Conviction-weighted sizing

Each trade = `cash * SMART_POSITION_PCT (0.18) * conviction_multiplier`, capped by `SMART_MAX_POSITION_CEILING_USD ($150)` or `equity * SMART_MAX_POSITION_CEILING_PCT (0.30)` — whichever is bigger.

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

At $90 cash: weak signal ~$11, 4-wallet $1k flow signal ~$26, 5-wallet $5k+ ~$40.

`SMART_CASH_FLOOR_PCT=0.05` dynamically redistributes the remaining deploy budget across the remaining opportunities of the tick to target ~95% deployment.

### Exits (before every new entry)

- **Take-profit ladder** at +25% / +50% / +100% / +200% / +300% with partial sells (15% / 25% / 50% / 25% / 15%).
- **Trailing stop** arms at +25% peak, exits on 50% giveback while still positive.
- **Peak-protect** arms at +100% peak, exits on giveback to +40%.
- **Stop-loss** at -40% after the position has been open for at least 15 minutes.
- **Cohort-sell exit** if any entry wallet has SOLD the token within the lookback window.
- **Cohort-silent exit** if no cohort wallet has re-bought.
- **Near-expiry positive-PnL exit** within 20 minutes of close at ≥+5%.
- **Max-hold-time** force-close at 24 hours when nothing else has fired.

### Defensive auto-tuner

The bot adapts its parameters from real journal outcomes:

| If… | Action |
|---|---|
| Stop-loss > 40% of trades | Tighten `MAX_CHASE_PREMIUM` ×0.80 and `MAX_RELATIVE_SPREAD` ×0.85 |
| Consensus=2 trades avg PnL < -$0.30 (≥20 sample) | Raise `MIN_CONSENSUS` to 3 |
| Sports avg PnL < -$0.30 (≥15 sample) | Bump `SPORTS_SCORE_PENALTY` ×1.5 |
| Win rate < 30% | Raise `MIN_COPIED_USDC` ×1.5 |
| Avg PnL < -$0.20 | Reduce `POSITION_PCT` ×0.75 |

Paused below 30 closed trades to avoid overfitting. **Defensive only**: tightens after losses, never loosens after wins. Disable via `POLYMARKET_SMART_AUTO_TUNE_ENABLED=0`.

### What the strategy does NOT do

- Invent an opinion on a market with no signal. **No-signal / no-trade** is a valid position.
- Trade 24/7. Quiet hours stay quiet.
- Modify its own source code or push to git.
- Call any LLM in the trading loop.
- Guarantee profit. It is an edge-seeking system.

## Bot capabilities

- **Smart-money copy-trading** — multi-category leaderboard scan, parallel trade fetching, token grouping, multi-wallet consensus requirement.
- **Reverse-lookup** — when profitable wallets buy tokens not in the initial Gamma scan, the bot fetches the missing markets in batches of 20 token-ids and merges them into the eligible pool.
- **Three-pass scan per tick** — strict, relaxed (consensus floor relaxed), deep fallback (consensus=1 with looser filters). One leaderboard+trades fetch shared across all three passes.
- **Percentage sizing** — each trade = `cash * SMART_POSITION_PCT * conviction_multiplier`, with absolute ceiling, equity-pct ceiling, cash floor, and dynamic redistribution of remaining budget across remaining opportunities.
- **Conviction multipliers** — weak 0.7x, mid 0.9x, strong-3-wallet 1.1–1.3x, high-4-wallet 1.6–2.0x, very-high-5-wallet+ 2.5x, crypto-micro 0.55x.
- **Multi-level exits** — partial take-profit ladder (+25% / +50% / +100% / +200% / +300%), trailing stop (arms +25%, 50% giveback), peak-protect (+100% arm, exits below +40%), stop-loss -40% (after 15 min), resolved-market exit (bid ≥ 0.97), cohort-sell active SELL detection (120 min lookback), cohort-silent (no fresh BUY), near-expiry positive exit, max-hold 24h. When a SELL is rejected with "balance is not enough", the bot auto-cancels the resting CLOB order on that token and retries on the next tick.
- **Trade journal** — every closed position writes a JSON line to `data/trade_journal.jsonl` with full entry signal metadata, exit reason, and realized PnL.
- **Defensive auto-tuner** — every tick, reads the journal and applies bounded overrides to `data/strategy_overrides.json` when filters are too loose. Paused below 30 closed trades. Defensive only: tightens after losses, never loosens after wins.
- **BTC edge integrated** — after every smart-money tick, the Black-Scholes-from-volatility model in `bitcoin.py` runs. If model edge over market price exceeds `BTC_MIN_EDGE` (default 8%), a small $5 trade is placed. Disciplined — not "buy 0.95 it's free money."
- **Noise fallback** — when all three smart-money scans return 0 AND the bot is below `MIN_OPEN_POSITIONS` OR cash share of equity exceeds the cash-pressure threshold, up to 4 trades at $10 each are placed on top-scored Gamma candidates. Tagged `noise_fallback` in the journal so the cost can be measured.
- **Live sync** — every tick syncs live Polymarket positions into the ledger and refreshes live USDC cash from the CLOB.
- **Dashboard** — `http://127.0.0.1:8765`, refreshes every 5 seconds with equity, open positions, recent trades, candidates, and last-tick rejections.

## Auto-tune from journal data

The bot reads `data/trade_journal.jsonl` every tick and writes bounded overrides to `data/strategy_overrides.json`. Overrides are applied on top of env-var settings. The bot does **not** push code to git on its own. Strategy adjustments are auditable data, not code edits.

To inspect the journal:

```bash
uv run pmbot journal-stats
```

To run the auto-tuner manually (writes the overrides file once):

```bash
uv run pmbot tune-strategy
```

## Main environment variables

```bash
# Authentication (required for live)
POLYMARKET_ENABLE_LIVE_TRADING=1
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_FUNDER_ADDRESS=0x...
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...

# Sizing
POLYMARKET_SMART_POSITION_PCT=0.18
POLYMARKET_SMART_MAX_POSITION_CEILING_USD=150
POLYMARKET_SMART_MAX_POSITION_CEILING_PCT=0.30
POLYMARKET_SMART_CASH_FLOOR_PCT=0.05
POLYMARKET_SMART_HIGH_CONVICTION_BALANCE_FRACTION=0.15
POLYMARKET_MAX_POSITION_USD=7
POLYMARKET_SMART_MAX_TRADE_USD=7

# Trader cohort
POLYMARKET_SMART_TIME_PERIOD=MONTH
POLYMARKET_SMART_LEADERBOARD_LIMIT=100
POLYMARKET_SMART_MIN_TRADER_PNL=1000
POLYMARKET_SMART_MIN_TRADER_VOLUME=2000
POLYMARKET_SMART_MIN_TRADER_ROI=0.03
POLYMARKET_SMART_TRADE_FETCH_CONCURRENCY=24

# Entry filters
POLYMARKET_SMART_MIN_CONSENSUS=2
POLYMARKET_SMART_FALLBACK_CONSENSUS=2
POLYMARKET_SMART_MIN_COPIED_USDC=75
POLYMARKET_SMART_MAX_CHASE_PREMIUM=0.13
POLYMARKET_SMART_MAX_ENTRY_SLIPPAGE=0.12
POLYMARKET_SMART_MIN_BUY_PRICE=0.03
POLYMARKET_SMART_MAX_BUY_PRICE=0.96
POLYMARKET_SMART_MAX_SPREAD=0.08
POLYMARKET_SMART_MAX_RELATIVE_SPREAD=0.45
POLYMARKET_SMART_MAX_SIGNAL_AGE_MINUTES=10
POLYMARKET_SMART_TRADE_LOOKBACK_MINUTES=30

# Discovery
POLYMARKET_SMART_REVERSE_LOOKUP_ENABLED=1
POLYMARKET_SMART_REVERSE_LOOKUP_MAX_TOKENS=100
POLYMARKET_SMART_REVERSE_LOOKUP_MIN_COPIED_USDC=50

# Activity floors
POLYMARKET_MIN_OPEN_POSITIONS=7
POLYMARKET_SMART_DEEP_FALLBACK_ENABLED=1
POLYMARKET_SMART_DEEP_FALLBACK_MIN_COPIED_USDC=25
POLYMARKET_SMART_NOISE_FALLBACK_ENABLED=1
POLYMARKET_SMART_NOISE_FALLBACK_MAX_TRADES_PER_TICK=8
POLYMARKET_SMART_NOISE_FALLBACK_MAX_TRADE_USD=15
POLYMARKET_SMART_NOISE_FALLBACK_CASH_PRESSURE_PCT=0.25

# Exits
POLYMARKET_SMART_TAKE_PROFIT_TIERS=0.25:0.15,0.5:0.25,1.0:0.50,2.0:0.25,3.0:0.15
POLYMARKET_SMART_PEAK_PROTECT_TRIGGER=1.0
POLYMARKET_SMART_PEAK_PROTECT_FLOOR=0.40
POLYMARKET_SMART_TRAILING_STOP_ARM_PCT=0.25
POLYMARKET_SMART_TRAILING_STOP_GIVEBACK_PCT=0.50
POLYMARKET_SMART_STOP_LOSS_PCT=0.40
POLYMARKET_SMART_STOP_LOSS_MIN_AGE_MINUTES=15
POLYMARKET_SMART_MAX_HOLD_HOURS=24
POLYMARKET_SMART_COHORT_EXIT_ENABLED=1
POLYMARKET_SMART_COHORT_EXIT_LOOKBACK_MINUTES=120
POLYMARKET_SMART_COHORT_EXIT_MIN_AGE_MINUTES=20
POLYMARKET_SMART_EXIT_MINUTES_TO_CLOSE=20
POLYMARKET_SMART_EXIT_MIN_PROFIT=0.05

# BTC edge integrated
POLYMARKET_BTC_EDGE_INTEGRATED=1
POLYMARKET_BTC_MAX_TRADE_USD=5
POLYMARKET_BTC_MIN_EDGE=0.08
POLYMARKET_BTC_MAX_SPREAD=0.04

# Auto-tuner
POLYMARKET_SMART_AUTO_TUNE_ENABLED=1
POLYMARKET_SMART_AUTO_TUNE_MIN_TRADES=30

# Paths
POLYMARKET_STATE_PATH=data/paper_state.json
POLYMARKET_TRADE_JOURNAL_PATH=data/trade_journal.jsonl
POLYMARKET_STRATEGY_OVERRIDES_PATH=data/strategy_overrides.json

# Loop
POLYMARKET_AUTO_INTERVAL_SECONDS=10
POLYMARKET_SYNC_LIVE_POSITIONS=1
```

The complete list lives in `polymarket_bot/config.py`. The canonical source of truth for live config is `configs/profiles/claude_baseline_let_run.toml`, loaded by `scripts/run_live_70.sh` (or `scripts/run_all.sh`).

## API credentials

Live order placement requires the three CLOB credentials:

```bash
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...
```

Relayer credentials (`RELAYER_API_KEY`, `RELAYER_API_KEY_ADDRESS`) are different and not sufficient for the current CLOB order path. If only relayer credentials are configured, the bot scans markets but refuses to place live orders with a clear local error.

## Trade journal and tuning

After the bot has been running for a while:

```bash
uv run pmbot journal-stats
```

Prints global win rate, total PnL, breakdown by category / consensus / strategy / exit reason / entry-price bucket, and tightening suggestions when the sample exceeds 30 trades.

```bash
uv run pmbot tune-strategy
```

Runs the tuner manually and writes `data/strategy_overrides.json`. The same logic also runs automatically every tick when `POLYMARKET_SMART_AUTO_TUNE_ENABLED=1`.

## Dashboard

```bash
uv run pmbot dashboard
```

Open `http://127.0.0.1:8765`. Refreshes every 5 seconds: bot mode, equity, open positions, recent trades, order IDs, scanner candidates, last-tick rejection reasons.

## Tests and CI

```bash
python3 -B -m unittest discover -s tests
```

GitHub Actions runs the full test suite on Python 3.11 / 3.12 and `ruff` lint on every push (see `.github/workflows/test.yml`).

## Versioning and changelog

The project follows [Semantic Versioning](https://semver.org/). User-visible changes are documented in `CHANGELOG.md`.

## Contributing and security

- Contributions: see `CONTRIBUTING.md`.
- Security disclosures: see `SECURITY.md`.

## Agent docs

- `CLAUDE.md` — Claude Code entry point.
- `.claude/skills/polymarket-bot/SKILL.md` — Claude structured skill.
- `CODEX.md` — Codex entry point.
- `.codex/skills/polymarket-bot/SKILL.md` — Codex structured skill.
- `AGENTS.md` — generic agent docs.
- `docs/AUTONOMOUS_STRATEGY.md` — strategy and dashboard rules.

## Notes

- This is not guaranteed profit. It is a system that seeks edge by following strong public flow, filtering execution, and tightening exits. No-signal / no-trade is the default position.
- Scanning and trade selection are deterministic Python rules over Polymarket APIs. There is no LLM call in the trading loop.
- The scanner score is based on urgency, liquidity, volume, and tradability. It is not an expected-value model.
- The bot has no capability to write or push source code on its own. Strategy adjustments are auditable data files, not code modifications.

## License

MIT. See `LICENSE`.

## Disclaimer

**This software places real-money trades when configured to do so.**

- It is not financial advice.
- There is no guarantee of profit. Losses are possible and likely over some time horizons.
- The user is solely responsible for all trading decisions and committed funds.
- The user is responsible for complying with applicable laws and Polymarket's terms of service in their jurisdiction.
- The author and contributors disclaim all liability for losses or damages arising from the use of this software. See the full clause in `LICENSE`.

Before the first live run, exercise the bot in a controlled environment, verify the filters, and limit the initial bankroll to an amount you can afford to lose entirely.
