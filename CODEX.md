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

- `polymarket_bot/main.py` — CLI commands and tick orchestration. Sizing helpers, trade-journal writer, `journal-stats` and `tune-strategy` commands.
- `polymarket_bot/smart_money.py` — leaderboards, parallel trade fetching, token grouping, scoring, reverse-lookup helper.
- `polymarket_bot/auto_tuner.py` — bounded overrides from the trade journal (defensive only).
- `polymarket_bot/bitcoin.py` — BTC threshold edge model (Black-Scholes-from-volatility).
- `polymarket_bot/trading.py` — live BUY/SELL order placement and stake computation.
- `polymarket_bot/dashboard.py` — local dashboard at `http://127.0.0.1:8765`.
- `polymarket_bot/portfolio.py` — local ledger, positions, pending orders, exits.
- `polymarket_bot/gamma.py` — Gamma client + reverse-lookup by clob_token_ids.
- `polymarket_bot/strategy.py` — candidate ranking.
- `scripts/run_live_70.sh` — canonical live runner.
- `tests/test_strategy.py` — 52 tests.

## Commands

Tests:

```bash
python3 -B -m unittest discover -s tests
```

Live loop (what `scripts/run_live_70.sh` invokes):

```bash
POLYMARKET_ENABLE_LIVE_TRADING=1 python3 -B -m polymarket_bot.main auto-loop
```

Dashboard, journal stats, manual auto-tuner, bootstrap-creds, and reset-ledger: see `CLAUDE.md`. The CLI is limited to 6 commands: `auto-loop`, `dashboard`, `journal-stats`, `tune-strategy`, `bootstrap-creds`, `reset-ledger`.

## Recommended live command

```bash
bash scripts/run_live_70.sh
```

See `CLAUDE.md` for the full parameter list and tick sequence.

## Winning strategy

Smart-money copy-trading. The bot waits for profitable wallets (top monthly leaderboard, PnL ≥ $1k, volume ≥ $2k, ROI ≥ 3%) to buy the same token in a short window (30 minutes), then mirrors that flow.

### The edge

Wallets at the top of monthly leaderboards with meaningful PnL and volume have, on average, an informational edge on the markets they trade. When several buy the same token simultaneously, the collective signal is stronger than any single wallet. The bot copies that flow.

### Entry conditions

- Recent BUY trades from qualified wallets (PnL / volume / ROI / recency).
- Multi-wallet consensus on the same token.
- Enough copied USDC.
- Tradable market: tight absolute and relative spread, ask within configured price band, not too close to expiry.
- No duplicate per market or per event-slug (sports).
- Conviction-weighted sizing (0.55x to 2.5x base).

### Exits

- Take-profit ladder at +50% / +100% / +200% / +300% with partial sells.
- Trailing stop arms at +25% peak, exits on 50% giveback while still positive.
- Peak-protect arms at +100% peak, exits below +40%.
- Stop-loss at -40% after 15 minutes in position.
- Cohort-sell exit (active SELL detection in 120 min lookback) or cohort-silent (no fresh BUY).
- Near-expiry positive-PnL exit at ≥+5% within 20 minutes of close.
- Max-hold-time force-close at 24 hours.

### Auto-tuner

Reads the trade journal each tick. Paused below 30 closed trades. Tightens filters and sizing after losing patterns. **Defensive only.**

### Not guaranteed profit

The expected edge comes from copying strong public flow while avoiding bad execution. No-signal / no-trade is a valid position.
