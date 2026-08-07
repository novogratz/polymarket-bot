# Operations Guide

This guide covers routine production operation. Complete a dry-run evaluation and review the [strategy](STRATEGIES.md) before using real funds.

## Preflight

```bash
uv sync --extra dev
uv run python -B -m unittest discover -s tests
uv run pmbot status
uv run pmbot positions
uv run pmbot journal-stats
```

Confirm that:

- `.env` exists locally and is not tracked by Git.
- The selected wallet, profile, ledger, and Telegram destination belong to the intended bot.
- Venue cash and approvals are sufficient.
- No duplicate launcher is already running.
- Existing positions are understood and reconciled.

## Start and stop

```bash
# Bot 1
bash scripts/run_live_70.sh

# Bot 2
bash scripts/run_live_b.sh

# Bot 3
bash scripts/run_live_c.sh
```

Run one launcher per configured bot. Stop a foreground stack with `Ctrl+C`. The launcher trap should terminate its child processes; verify this before restarting.

## Healthy first scan

A normal scan reports:

- A nonzero raw-market count.
- The number of eligible opportunities, which may legitimately be zero.
- Any rejected signals with a concrete reason.
- Orders only after eligibility and executable-depth checks.
- A portfolio heartbeat whose venue and ledger totals reconcile closely.

Zero orders are normal when no market passes the weather, time, price, forecast, exposure, or executable-depth requirements. The decision journal should explain the result; do not remove controls merely to force activity.

The raw count is produced from three fully paginated Gamma batches and then deduplicated. There is no configured 1,500-market ceiling. A later-page API failure returns the partial inventory collected so far and is visible in the process log.

## Monitoring

Review the following throughout a live session:

- `data/logs/run_live_YYYY-MM-DD.log` for the composed stack.
- `data/logs/live_YYYY-MM-DD.log` for live-engine events.
- Telegram reports for cash, equity, realized performance, and every open position.
- `pmbot positions` for local position metadata.
- The Polymarket account for venue-authoritative balances and orders.

## Common incidents

### Ledger and venue equity differ

Stop new execution if the difference is material. Inspect venue balances and positions, then use the maintained reconciliation commands or scripts. Do not manually edit JSON state while a live process is running.

### Eligible market, no order

Read `rejected_signals`. Common causes are insufficient executable depth, the venue minimum order, stale quotes, concentration limits, or an existing opposite position.

### Market is past its nominal end time

Some markets remain open during resolution. Weather positions are held instead of being force-sold below entry. Confirm the market's actual status on the venue.

### External API failure

Fresh forecast-gated entries fail closed when required forecast data is unavailable. Gamma discovery batches fail independently, and a failure after the first page returns the partial batch already collected. Repeated errors warrant stopping the stack and diagnosing connectivity, rate limits, or credentials.

## Recovery and maintenance

- Back up local state before a manual recovery operation.
- Use purpose-built scripts rather than editing ledger structures by hand.
- Never delete trade history to disguise performance; reset baselines only as an explicit, documented operational event.
- Never run multiple writers against the same ledger.
- Re-run tests and preflight checks after upgrading.

## Daily review

Use the decision journal and realized cache to compare:

- Trades taken versus missed opportunities.
- Forecast probability versus actual resolution.
- Expected edge versus realized return.
- Concentration by city, date, price, and outcome.
- Fill quality and rejected executable depth.

Four or five wins do not establish a profitable strategy. Evaluate calibration and drawdown over a materially larger, out-of-sample set.
