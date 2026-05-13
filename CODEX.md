# Codex Guide

Codex entry point for the Polymarket bot. See also the structured skill in `.codex/skills/polymarket-bot/SKILL.md`. The Claude Code version (equivalent content) lives in `CLAUDE.md` and `.claude/skills/polymarket-bot/SKILL.md`.

The project is MIT licensed (see `LICENSE`). Tests run in CI (GitHub Actions, see `.github/workflows/test.yml`).

## Safety

- Never reveal `.env` values, private keys, API secrets, or passphrases.
- Do not bypass `POLYMARKET_ENABLE_LIVE_TRADING=1`.
- Do not implement random or unfiltered live trades. The `noise_fallback` path is the only forced-trade lane and is hard-capped at $10 per trade and 4 trades per tick.
- Preserve `data/paper_state.json`, `data/trade_journal.jsonl`, and `data/strategy_overrides.json` unless explicitly asked to reset them.
- No LLM call (Codex, Claude, anything else) in the scanning or trade-selection path.
- The bot must not have the capability to write or push source code on its own.

## Project map

- `polymarket_bot/main.py` — CLI commands and strategy loops.
- `polymarket_bot/mirror.py` — mirror-mode strategy: whale discovery, trade polling, eligibility, conviction sizing, buy/sell, drawdown limits.
- `polymarket_bot/smart_money.py` — legacy smart-money strategy.
- `polymarket_bot/dry_run_runs.py` — named dry-run lifecycle (`--run <name>`).
- `polymarket_bot/auto_tuner.py` — defensive strategy overrides from trade journal.
- `polymarket_bot/trading.py` — live BUY/SELL placement, SDK/legacy client wrapper, order cancellation via SDK v2 `get_open_orders`.
- `polymarket_bot/portfolio.py` — local ledger with cash, positions, exits.
- `polymarket_bot/gamma.py` — Gamma client + reverse-lookup.
- `polymarket_bot/strategy.py` — candidate ranking.
- `polymarket_bot/profiles.py` — TOML profile loader.
- `scripts/run_live_70.sh` — canonical live runner.
- `tests/test_mirror.py` — mirror mode tests.
- `tests/test_strategy.py` — strategy tests.
- `docs/PROFILES.md` — TOML profile reference.
- `docs/STRATEGIES.md` — strategy docs.

## Commands

Tests:

```bash
uv run python -B -m unittest discover -s tests
```

Live loop:

```bash
uv run pmbot auto-loop --profile copy-advanced --live --yes
```

Dry-run with same strategy:

```bash
POLYMARKET_PAPER_BALANCE_USD=100 uv run pmbot auto-loop --profile copy-advanced --dry-run --run mirror-whales
```

Doctor, status, positions, dashboard, journal-stats, tune-strategy: see `CLAUDE.md`.

## Mirror strategy: whale copy-trading

Copies the collective moves of proven Polymarket whales. Scans leaderboards, discovers high-PnL wallets, polls their trades, and copies when ≥2 profitable wallets buy the same token.

### Entry

- Whale stake ≥ $100, bid-ask filters, chase premium ≤ 5%, liquidity ≥ $10k.
- Sizing: `whale_stake × 0.20` (tiered up to 0.35), capped at 25% of equity and $250 hard cap.
- Max 12 open positions, 50% per category.

### Exits

Take-profit ladder (+25% to +300%), trailing stop, peak-protect, stop-loss (-40%), cohort follow, near-expiry, max-hold 24h. Sells are GTC limit orders — recorded as exits immediately even if unfilled.

### Sell mechanics

GTC sells with `status: "live"/"delayed"` are recorded as exits (shares deducted, cash credited). Stale GTC orders auto-cancelled via SDK v2. Live position + cash sync each tick prevents duplicate sell attempts.

### Auto-tuner

Defensive only. Tightens filters after losing patterns. Reads journal from 30 closed trades.

### Not guaranteed profit

No-signal / no-trade is a valid position.
