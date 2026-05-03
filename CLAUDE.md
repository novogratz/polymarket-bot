# Claude Code Guide

Use this file as the Claude Code entry point for the Polymarket bot.

## Safety

- Never reveal `.env` values, private keys, API secrets, or passphrases.
- Do not bypass `POLYMARKET_ENABLE_LIVE_TRADING=1`.
- Do not implement random or unfiltered live trades.
- Preserve the local ledger in `data/paper_state.json` unless the user explicitly asks to reset it.

## Project Map

- `polymarket_bot/main.py`: CLI commands and strategy loops.
- `polymarket_bot/smart_money.py`: autonomous smart-money copy-trading filters.
- `polymarket_bot/bitcoin.py`: optional BTC threshold edge model.
- `polymarket_bot/trading.py`: authenticated live BUY/SELL order placement and sizing.
- `polymarket_bot/dashboard.py`: local real-time HTML dashboard.
- `polymarket_bot/portfolio.py`: local ledger for paper and live positions.
- `tests/test_strategy.py`: strategy and order-building tests.

## Development Workflow

Run:

```bash
python3 -B -m unittest discover -s tests
```

Run the dashboard:

```bash
python3 -B -m polymarket_bot.main dashboard
```

Run the autonomous smart-money loop:

```bash
POLYMARKET_ENABLE_LIVE_TRADING=1 python3 -B -m polymarket_bot.main auto-loop
```

Run it faster:

```bash
POLYMARKET_ENABLE_LIVE_TRADING=1 POLYMARKET_AUTO_INTERVAL_SECONDS=30 python3 -B -m polymarket_bot.main auto-loop
```

Each smart-money tick prints a `scan_report` explaining selected opportunities, considered opportunities, counts, rejection reasons, and sell exits. Scanning and trade selection are deterministic Python rules over Polymarket APIs; do not add Claude, Codex, or any LLM call to the scan path.

The dashboard is served at `http://127.0.0.1:8765` by default.

## Strategy To Make Money

The default strategy is smart-money copy trading. The bot does not try to invent an opinion on every market. It waits for public order-flow evidence that profitable wallets are buying the same token.

The live entry should require:

- Recent BUY trades from profitable leaderboard wallets.
- Consensus from multiple distinct wallets on the same token.
- Enough copied USDC size to matter.
- A tradable market with acceptable spread and ask price.
- No existing open position for the same market and outcome.
- Explicit live-trading enablement.
- $5-capped order sizing by default, repeated across qualified opportunities until funds, per-tick cap, or signal exhaustion stops the tick.
- Profit-taking exits before new buys: default +100%/+200%/+300% partial sells, plus peak giveback protection.

The expected edge comes from copying strong public flow while avoiding bad execution. This is not guaranteed profit; no-signal/no-trade is part of the strategy.
