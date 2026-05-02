# Autonomous Strategy

The bot's default autonomous mode is `smart-money-loop`. It is designed to avoid random trades and only enter when there is a repeatable signal.

## Money-Making Thesis

The core thesis is that profitable Polymarket wallets sometimes reveal information through their order flow. One good trader buying a token can be noise. Multiple profitable wallets buying the same token in a short window is a stronger signal.

The bot is built to monetize that signal by:

- Copying recent BUY flow from leaderboard wallets with configured positive PnL.
- Requiring consensus across multiple wallets before entry.
- Avoiding expensive fills by enforcing spread and price-band limits.
- Keeping trade size bounded by live balance and a max dollar cap.
- Refusing duplicate positions so it does not accidentally pyramid into the same outcome.

The strategy does not predict every market from scratch. It follows public smart-money activity only when execution quality is acceptable.

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

## When The Bot Refuses To Trade

No trade is a valid outcome. The bot should skip if:

- Fewer than the required number of wallets bought the same token.
- The best ask is too low, too high, missing, or stale.
- The spread is wider than `POLYMARKET_SMART_MAX_SPREAD`.
- Copied trades are too small to matter.
- The market is not accepting orders.
- The ledger already has the same market and outcome open.
- Live trading is not explicitly enabled.

This refusal logic is intentional. Forced trades are how the bot ends up in weak, random markets.

## Automation

Run:

```bash
POLYMARKET_ENABLE_LIVE_TRADING=1 python3 -B -m polymarket_bot.main auto-loop
```

The loop wakes every `POLYMARKET_AUTO_INTERVAL_SECONDS` seconds. The default is 300 seconds.

For faster scans, override the interval when starting the loop:

```bash
POLYMARKET_ENABLE_LIVE_TRADING=1 POLYMARKET_AUTO_INTERVAL_SECONDS=30 python3 -B -m polymarket_bot.main auto-loop
```

Each tick prints a `scan_report` with:

- The selected opportunity, if one qualified.
- The top opportunities considered.
- Trader and trade counts.
- Eligible trade and grouped token counts.
- Rejection reasons when nothing qualified.

The scan path is deterministic Python code calling Polymarket APIs. It does not use Codex, Claude, or any LLM.

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
