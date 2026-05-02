# polymarket-bot

Polymarket market scanner with a local dashboard, paper portfolio, and an authenticated live-trading path.

## Run

```bash
python3 -m pip install -r requirements.txt
python3 -m polymarket_bot.main scan
python3 -m polymarket_bot.main paper-tick
python3 -m polymarket_bot.main bootstrap-creds
python3 -m polymarket_bot.main trade-once
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
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_FUNDER_ADDRESS=0x...
POLYMARKET_SIGNATURE_TYPE=0
POLYMARKET_ENABLE_LIVE_TRADING=1
```

`paper-tick` opens one simulated position in the highest-ranked soon market, capped by `POLYMARKET_MAX_POSITION_USD`, then marks existing simulated positions to market.

`bootstrap-creds` derives or loads your Polymarket API credentials using the wallet key.

`trade-once` places one live marketable limit order against the highest-ranked eligible soon market. It refuses to run unless `POLYMARKET_ENABLE_LIVE_TRADING=1` is set.

## Notes

The scanner score is based on urgency, liquidity, volume, and tradability. It is not an expected-value model.
The bot uses Polymarket’s documented wallet-based auth flow. A Safari login alone is not sufficient for trading.
