# polymarket-bot

Read-only Polymarket market scanner with a local live dashboard and paper portfolio.

This project intentionally does not place real-money orders. A Safari login is not usable for API trading, and an unchecked all-in bot is a fast way to lose the whole account. The code uses Polymarket public APIs for market discovery and keeps trading simulation in `data/paper_state.json`.

## Run

```bash
python3 -m polymarket_bot.main scan
python3 -m polymarket_bot.main paper-tick
python3 -m polymarket_bot.main dashboard
```

Dashboard URL:

```text
http://127.0.0.1:8765
```

## Configuration

Environment variables:

```bash
POLYMARKET_SCAN_LIMIT=200
POLYMARKET_SOON_HOURS=72
POLYMARKET_PAPER_BALANCE_USD=20
POLYMARKET_MAX_POSITION_USD=5
POLYMARKET_MIN_LIQUIDITY_USD=500
POLYMARKET_MIN_VOLUME_USD=1000
POLYMARKET_DASHBOARD_PORT=8765
```

`paper-tick` opens one simulated position in the highest-ranked soon market, capped by `POLYMARKET_MAX_POSITION_USD`, then marks existing simulated positions to market.

## Notes

The scanner score is based on urgency, liquidity, volume, and tradability. It is not an expected-value model and should not be treated as financial advice.
