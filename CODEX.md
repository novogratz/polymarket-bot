# Codex Guide

Use this file as the Codex entry point for the Polymarket bot.

## Safety

- Never reveal `.env` values, private keys, API secrets, or passphrases.
- Keep live trading gated by `POLYMARKET_ENABLE_LIVE_TRADING=1`.
- Do not add random or unfiltered live trade entry.
- Preserve `data/paper_state.json` unless the user explicitly asks to reset local state.

## Project Map

- `polymarket_bot/main.py`: CLI commands and strategy loops.
- `polymarket_bot/smart_money.py`: autonomous smart-money copy-trading filters.
- `polymarket_bot/bitcoin.py`: optional BTC threshold edge model.
- `polymarket_bot/trading.py`: authenticated live order placement and sizing.
- `polymarket_bot/dashboard.py`: local real-time HTML dashboard.
- `polymarket_bot/portfolio.py`: local ledger for paper and live positions.
- `tests/test_strategy.py`: strategy and order-building tests.

## Commands

Run tests:

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

Each smart-money loop tick prints a `scan_report` with the selected opportunity, top considered opportunities, trader/trade counts, grouped tokens, and rejection reasons. The scan path is deterministic Python plus Polymarket APIs; do not add Codex, Claude, or LLM calls to scanning or trade selection.

## Strategy

The default autonomous strategy is smart-money copy trading. It requires profitable leaderboard wallets, recent BUY consensus on the same token, size filters, spread filters, price-band filters, and duplicate-position checks before live entry.

BTC edge trading is optional and separate.

## Money-Making Logic

The bot should try to make money by following high-quality public order flow, not by guessing or forcing trades. The smart-money strategy looks for multiple profitable leaderboard wallets buying the same token recently, then checks that the market can be entered without a bad fill.

Keep these requirements intact:

- Consensus beats single-wallet signals.
- Tight spreads beat illiquid markets.
- Size caps protect the account from one bad thesis.
- Duplicate-position checks prevent accidental overexposure.
- Refusing to trade is correct when the signal is weak.

Never describe this as guaranteed profit. Describe it as an edge-seeking copy-trading system with execution and risk filters.
