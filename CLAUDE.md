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
- `polymarket_bot/trading.py`: authenticated live order placement and sizing.
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

The dashboard is served at `http://127.0.0.1:8765` by default.
