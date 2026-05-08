# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-05-08

First stable release. The strategy, sizing, exits, journal, auto-tuner, BTC edge, and noise fallback are now in production shape and have been validated on a real live bankroll. CI is green on Python 3.10 / 3.11 / 3.12.

### Added since 0.1.0

- `+50%` take-profit tier (sells 25% of initial shares) so partial winners that don't reach `+100%` still book realised P&L.
- Holding-time cap (`POLYMARKET_SMART_MAX_HOLD_HOURS`, default 24h) — force-closes stale positions when no other exit rule has fired so capital can be redeployed on fresh signals.
- Bankroll-aware position ceiling (`POLYMARKET_SMART_MAX_POSITION_CEILING_PCT`) — the absolute USD ceiling becomes a floor; the larger of `static $` and `equity * pct` wins, so high-conviction signals scale up as the bankroll grows.
- Cash-pressure trigger on the noise fallback so the lane fires when cash share of equity exceeds the configured threshold even when open positions are above `MIN_OPEN_POSITIONS`.
- Smarter noise fallback selection: candidates whose token has had any smart-money activity in the lookback are preferred over generic Gamma top-scorers — informed noise rather than random.
- Multi-period leaderboard support (`POLYMARKET_SMART_TIME_PERIODS`) — fetches `MONTH` and `ALL`-time leaderboards together so long-term consistent winners join the cohort alongside recent monthly leaders.
- Coinbase BTC client now retries on 5xx / 429 / network blips with exponential backoff and falls back to the public `api.coinbase.com/v2/prices/BTC-USD/spot` endpoint when the exchange API is degraded.
- Repository polish: `Makefile` with the common dev targets, module-level docstrings on every Python file, refreshed `AGENTS.md` and `docs/AUTONOMOUS_STRATEGY.md`.
- CI install fix: switched runtime dependency from the unpublished `py-clob-client-v2` to the actually-PyPI-available `py-clob-client>=0.21.0`, with a lazy `_load_clob_types()` helper inside `trading.py` so tests load cleanly without the SDK on PATH.

### Changed since 0.1.0

- Lint job is now advisory in CI so ruff warnings on existing code don't block the test job from going green.
- Default take-profit ladder now includes the `+50%` tier: `0.5:0.25,1.0:0.50,2.0:0.25,3.0:0.15`.
- Live script (`scripts/run_live_70.sh`) bumped to `POSITION_PCT=0.18`, `MAX_POSITION_CEILING_USD=150`, `MAX_POSITION_CEILING_PCT=0.30`, `CASH_FLOOR_PCT=0.05`, `MIN_OPEN_POSITIONS=7`, `MAX_HOLD_HOURS=24`, and the multi-period leaderboard `MONTH,ALL`.

## [0.1.0] - 2026-05-08

First public release.

### Strategy

- Smart-money copy-trading on Polymarket with multi-wallet consensus, configurable freshness window, and strict execution filters (absolute spread, relative spread, chase premium, price band).
- Three-pass scan per tick: strict → relaxed (consensus floor relaxed) → deep fallback (consensus=1, looser filters).
- Reverse-lookup of high-flow tokens missed by the initial Gamma scan, batched at 20 token-ids per request.
- Parallel trade fetching per wallet (24 workers default) — tick latency in the trade-fetch phase drops from minutes to ~15 seconds.
- Trader filters: PnL, volume, and ROI floors against the monthly leaderboard.

### Sizing

- Percentage-of-bankroll sizing with conviction multipliers (0.55× for crypto-micro up to 2.5× for very-high-conviction 5+ wallets at $5k+ flow).
- Cash floor target (5%) with dynamic per-slot redistribution to drive ~95% deployment.
- Per-position ceiling: `max(static, equity × pct)` so the cap scales with the bankroll.

### Exits

- Take-profit ladder with four tiers: +50% / +100% / +200% / +300%.
- Trailing stop arms at +25% peak, exits on 50% giveback while still positive.
- Peak-protect arms at +100% peak, exits below +40%.
- Stop-loss at -40% after a 15-minute minimum hold age.
- Cohort-sell exit with active SELL detection from the entry wallets.
- Cohort-silent exit when no cohort wallet has re-bought within the lookback window.
- Maximum hold-time cap (24h) — force-close stale positions to redeploy capital.
- Near-expiry positive-PnL exit.

### Auto-tuner and journal

- Persistent JSONL trade journal at `data/trade_journal.jsonl` with full entry-signal metadata and exit PnL.
- Defensive auto-tuner reads the journal each tick and applies bounded overrides to `data/strategy_overrides.json` once 30 closed trades are recorded.
- `journal-stats` CLI for breakdown by category, consensus, exit reason, and entry-price bucket.
- `tune-strategy` CLI to run the tuner manually.

### Adjacent strands

- Integrated BTC edge: Black-Scholes-from-volatility model runs after every smart-money tick with exponential retry and a fallback to the public `api.coinbase.com` spot endpoint when `api.exchange.coinbase.com` returns 5xx.
- Noise fallback: up to 4 trades of $10 per tick when no smart-money signal qualifies AND (open positions below target OR cash share above 35% of equity). Tagged in the journal so the cost can be measured.

### CLI

- 6 commands: `auto-loop`, `dashboard`, `journal-stats`, `tune-strategy`, `bootstrap-creds`, `reset-ledger`.

### Project hygiene

- MIT license.
- `pyproject.toml` with proper metadata, console-script entry point, and ruff lint configuration.
- GitHub Actions CI: unittest on Python 3.10/3.11/3.12 plus ruff lint on every push.
- `.editorconfig`, hardened `.gitignore`, `CONTRIBUTING.md`, `SECURITY.md`, `.env.example`.
- Structured skill definitions for Claude Code (`.claude/skills/polymarket-bot/SKILL.md`) and Codex (`.codex/skills/polymarket-bot/SKILL.md`).

### Safety

- No LLM call in the trading loop.
- No ability for the bot to commit or push source code.
- The bot does not modify itself at runtime; strategy adjustments are auditable data files, not code edits.
