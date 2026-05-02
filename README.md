# polymarket-bot

Polymarket market scanner with a local dashboard, paper portfolio, and an authenticated live-trading path.

The default autonomous strategy is smart-money copy trading: it watches recent BUY trades from profitable leaderboard wallets, requires consensus across multiple wallets, filters for tight spreads and sane prices, and avoids duplicate open positions. BTC edge trading is still available as a separate optional strategy.

## Strategy To Make Money

The bot tries to make money by following informed flow instead of guessing outcomes. It assumes the best available public signal is not a single market headline, but repeated buying from wallets that have recently ranked well by PnL.

The autonomous strategy works like this:

1. Scan active Polymarket markets that are liquid, tradable, and closing soon enough to keep capital moving.
2. Pull leaderboard wallets by category and keep only traders with non-negative configured PnL.
3. Read their recent BUY trades from the public Polymarket Data API.
4. Look for consensus: at least `POLYMARKET_SMART_MIN_CONSENSUS` different profitable wallets must have bought the same token recently.
5. Enter only if the market has an open order book, a tight enough spread, enough copied trade size, and an ask price inside the configured price band.
6. Skip the trade if the local ledger already has that market/outcome open.
7. Size from live USDC balance with `POLYMARKET_TRADE_FRACTION`, capped by `POLYMARKET_SMART_MAX_TRADE_USD`.

This is not guaranteed profit. It is an edge-seeking system: copy strong public flow, avoid bad fills, keep positions sized, and refuse trades when the signal is weak.

## Run

```bash
python3 -m pip install -r requirements.txt
python3 -m polymarket_bot.main scan
python3 -m polymarket_bot.main paper-tick
python3 -m polymarket_bot.main bootstrap-creds
python3 -m polymarket_bot.main reset-ledger
python3 -m polymarket_bot.main trade-once
python3 -m polymarket_bot.main smart-money-once
python3 -m polymarket_bot.main smart-money-loop
python3 -m polymarket_bot.main auto-loop
python3 -m polymarket_bot.main btc-edge-once
python3 -m polymarket_bot.main btc-edge-loop
python3 -m polymarket_bot.main dashboard
```

Dashboard URL:

```text
http://127.0.0.1:8765
```

## Configuration

Create a local `.env` file in the project root with your wallet and trading settings.

Environment variables:

```bash
POLYMARKET_SCAN_LIMIT=200
POLYMARKET_SOON_HOURS=72
POLYMARKET_PAPER_BALANCE_USD=20
POLYMARKET_MAX_POSITION_USD=5
POLYMARKET_TRADE_FRACTION=0.10
POLYMARKET_BTC_MIN_MODEL_PROBABILITY=0.90
POLYMARKET_BTC_MIN_BUY_PRICE=0.70
POLYMARKET_BTC_MAX_BUY_PRICE=0.82
POLYMARKET_BTC_MIN_EDGE=0.08
POLYMARKET_BTC_MAX_SPREAD=0.03
POLYMARKET_BTC_MIN_TRADE_USD=1
POLYMARKET_BTC_MAX_TRADE_USD=25
POLYMARKET_BTC_VOLATILITY_DAYS=7
POLYMARKET_AUTO_INTERVAL_SECONDS=300
POLYMARKET_AUTO_MAX_TICKS=0
POLYMARKET_DATA_API_URL=https://data-api.polymarket.com
POLYMARKET_SMART_CATEGORIES=OVERALL,CRYPTO,FINANCE,ECONOMICS,TECH,POLITICS
POLYMARKET_SMART_TIME_PERIOD=WEEK
POLYMARKET_SMART_LEADERBOARD_LIMIT=15
POLYMARKET_SMART_SCAN_LIMIT=1000
POLYMARKET_SMART_SOON_HOURS=72
POLYMARKET_SMART_TRADE_LOOKBACK_MINUTES=20
POLYMARKET_SMART_MIN_CONSENSUS=2
POLYMARKET_SMART_FALLBACK_CONSENSUS=1
POLYMARKET_MIN_OPEN_POSITIONS=1
POLYMARKET_STARTER_TRADE_USD=1
POLYMARKET_SMART_MIN_TRADER_PNL=0
POLYMARKET_SMART_MIN_TRADE_USD=25
POLYMARKET_SMART_MIN_BUY_PRICE=0.08
POLYMARKET_SMART_MAX_BUY_PRICE=0.85
POLYMARKET_SMART_MAX_SPREAD=0.04
POLYMARKET_SMART_MAX_TRADE_USD=25
POLYMARKET_MIN_LIQUIDITY_USD=500
POLYMARKET_MIN_VOLUME_USD=1000
POLYMARKET_DASHBOARD_PORT=8765
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_FUNDER_ADDRESS=0x...
POLYMARKET_SIGNATURE_TYPE=0
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...
POLYMARKET_ENABLE_LIVE_TRADING=1
```

`paper-tick` opens one simulated position in the highest-ranked soon market, capped by `POLYMARKET_MAX_POSITION_USD`, then marks existing simulated positions to market.

`bootstrap-creds` derives or loads your Polymarket API credentials using the wallet key.

`reset-ledger` clears local dashboard positions and resets local cash from the live CLOB balance when credentials are available. Use this after manual trades make the dashboard ledger stale. It does not cancel or sell positions on Polymarket.

`trade-once` places one live marketable limit order against the highest-ranked eligible soon market. It refuses to run unless `POLYMARKET_ENABLE_LIVE_TRADING=1` is set.

`smart-money-once` places one live trade only when profitable leaderboard wallets recently bought the same token with enough consensus, size, and order-book quality.

`smart-money-loop` runs the smart-money strategy every `POLYMARKET_AUTO_INTERVAL_SECONDS` seconds. `auto-loop` is an alias for this default autonomous mode.

For faster scans, override the interval at runtime:

```bash
POLYMARKET_ENABLE_LIVE_TRADING=1 POLYMARKET_AUTO_INTERVAL_SECONDS=30 python3 -B -m polymarket_bot.main auto-loop
```

Each smart-money tick prints a `scan_report` with the top opportunities considered, selected signal if any, trader/trade counts, and rejection reasons when nothing qualifies. The scanner does not use Codex, Claude, or any LLM.

The smart-money universe defaults to `POLYMARKET_SMART_SOON_HOURS=72`, so it targets today, tomorrow, and the next few days instead of far-out contracts. If there are zero open positions and no normal consensus trade qualifies, the bot can use a `smart_money_starter` or `liquidity_starter` fallback to maintain at least `POLYMARKET_MIN_OPEN_POSITIONS`. Starter trades use `POLYMARKET_STARTER_TRADE_USD=1` by default and still require executable markets, spread/price filters where applicable, and duplicate-position checks.

`btc-edge-once` only trades parsable BTC above/below threshold markets when a Coinbase BTC spot/volatility model finds enough edge. It skips generic markets.

`btc-edge-loop` runs the BTC edge strategy every `POLYMARKET_AUTO_INTERVAL_SECONDS` seconds. Set `POLYMARKET_AUTO_MAX_TICKS=0` for an unlimited loop.

## API Credentials

Live CLOB order placement needs the three-part CLOB credential set:

```bash
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...
```

A relayer key is different:

```bash
RELAYER_API_KEY=...
RELAYER_API_KEY_ADDRESS=0x...
```

Relayer credentials alone are not enough for this bot's current CLOB order-placement path. If only relayer credentials are configured, `auto-loop` will scan markets but will refuse to place a live order with a clear local error instead of retrying the Cloudflare-blocked `/auth/api-key` bootstrap endpoint.

## Dashboard

Start the real-time dashboard:

```bash
python3 -B -m polymarket_bot.main dashboard
```

Open `http://127.0.0.1:8765`. It auto-refreshes every 5 seconds and shows bot mode, equity, open positions, recent bot trades, order IDs when available, and current scanner candidates.

## Agent Docs

This repo includes agent-compatible markdown:

- `AGENTS.md` for Codex-style coding agents.
- `CLAUDE.md` for Claude Code.
- `.codex/skills/polymarket-bot/SKILL.md` as a repository-local Codex skill.
- `docs/AUTONOMOUS_STRATEGY.md` for the trading rules and dashboard behavior.

## Notes

The scanner score is based on urgency, liquidity, volume, and tradability. It is not an expected-value model.
The bot uses Polymarket’s documented wallet-based auth flow. A Safari login alone is not sufficient for trading.
