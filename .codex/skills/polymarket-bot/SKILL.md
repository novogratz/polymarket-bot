# Polymarket Bot Skill

Use this skill when working on this repository's Polymarket trading bot, strategy filters, live-trading commands, or dashboard.

## Guardrails

- Never print or commit `.env`, private keys, API secrets, or passphrases.
- Keep live trading behind `POLYMARKET_ENABLE_LIVE_TRADING=1`.
- Do not add random trade entry logic.
- Any new live strategy must define entry criteria, spread filters, size caps, and duplicate-position checks.
- Update tests when strategy behavior changes.

## Main Commands

```bash
python3 -B -m unittest discover -s tests
python3 -B -m polymarket_bot.main scan
python3 -B -m polymarket_bot.main dashboard
POLYMARKET_ENABLE_LIVE_TRADING=1 python3 -B -m polymarket_bot.main smart-money-once
POLYMARKET_ENABLE_LIVE_TRADING=1 python3 -B -m polymarket_bot.main auto-loop
```

For faster scans:

```bash
POLYMARKET_ENABLE_LIVE_TRADING=1 POLYMARKET_AUTO_INTERVAL_SECONDS=30 python3 -B -m polymarket_bot.main auto-loop
```

Smart-money ticks print a `scan_report` with selected and considered opportunities, trader/trade counts, grouped token counts, and rejection reasons. The scan and trade-selection path must stay deterministic and must not call Codex, Claude, or any LLM.

## Strategy Defaults

The default autonomous strategy is smart-money copy trading:

1. Load active soon-closing Polymarket candidates.
2. Pull profitable leaderboard wallets from the configured categories.
3. Inspect recent BUY trades.
4. Require consensus from at least `POLYMARKET_SMART_MIN_CONSENSUS` distinct wallets on the same token.
5. Require minimum copied trade size, buy price band, open order book, and max spread.
6. Avoid opening a duplicate position already present in the local ledger.
7. Size by live balance fraction capped by `POLYMARKET_SMART_MAX_TRADE_USD`.

BTC edge trading is optional and remains available through `btc-edge-once` and `btc-edge-loop`.

## Money-Making Logic

The expected edge is public order-flow following:

- One wallet buying can be noise.
- Multiple profitable wallets buying the same token in a short window is a stronger signal.
- A good signal can still be a bad trade if the spread is wide or the ask is outside the configured band.
- Risk control matters: size by balance fraction and cap max trade dollars.
- Skipping is a valid action when the setup is not clean.

When editing strategy code, preserve this hierarchy: consensus first, execution quality second, sizing discipline third. Never replace it with random market selection.
