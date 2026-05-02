# Autonomous Strategy

The bot's default autonomous mode is `smart-money-loop`. It is designed to avoid random trades and only enter when there is a repeatable signal.

## Signal

The smart-money strategy scans recent BUY trades from profitable Polymarket leaderboard wallets. A candidate trade is eligible only when:

- The market is active, soon-closing, liquid enough, and tradable by the scanner.
- At least `POLYMARKET_SMART_MIN_CONSENSUS` distinct leaderboard wallets bought the same token recently.
- Each copied trade is at least `POLYMARKET_SMART_MIN_TRADE_USD`.
- The candidate's ask price is inside `POLYMARKET_SMART_MIN_BUY_PRICE` and `POLYMARKET_SMART_MAX_BUY_PRICE`.
- The order-book spread is no wider than `POLYMARKET_SMART_MAX_SPREAD`.
- The local ledger does not already have an open position for the same market and outcome.

## Sizing

Live order size is based on available USDC balance:

- Base size: `POLYMARKET_TRADE_FRACTION` of available balance.
- Cap: `POLYMARKET_SMART_MAX_TRADE_USD`.
- Minimum: Polymarket's $1 practical minimum.

This is risk control, not a profit guarantee.

## Automation

Run:

```bash
POLYMARKET_ENABLE_LIVE_TRADING=1 python3 -B -m polymarket_bot.main auto-loop
```

The loop wakes every `POLYMARKET_AUTO_INTERVAL_SECONDS` seconds. The default is 300 seconds.

## Dashboard

Run:

```bash
python3 -B -m polymarket_bot.main dashboard
```

Open `http://127.0.0.1:8765`. The dashboard auto-refreshes and shows:

- Live/paper mode status.
- Equity, cash, invested capital, open positions, and unrealized PnL from the local ledger.
- Open positions.
- Recent bot trades and order IDs when available.
- Current soon-market scanner candidates.
