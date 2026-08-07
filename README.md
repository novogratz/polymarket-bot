# Polymarket Weather Bot

A deterministic Python trading engine for short-dated Polymarket temperature markets. It combines market data, multi-model Open-Meteo forecasts, portfolio controls, live order execution, persistent decision records, and read-only operational reporting.

> [!WARNING]
> Prediction-market trading can result in a complete loss of capital. Forecasts and historical results do not guarantee future performance. Review the strategy, test in dry-run mode, and comply with all laws and platform terms that apply to you.

## Highlights

- Weather-only live universe; cryptocurrency and unrelated markets are excluded.
- Deterministic selection path with no LLM calls during scanning or execution.
- Forecast edge required for every fresh live entry; held-line redistribution is a separately documented top-up path.
- Equal-weight deployment with per-position concentration limits.
- Persistent trade and decision journals for post-trade analysis.
- Three production profiles, isolated named dry runs, Telegram reporting, and a local dashboard.
- Unit-tested strategy filters, sizing, exits, and reconciliation behavior.

## Strategy at a glance

The live profiles currently apply the same core policy:

| Control | Live policy |
| --- | --- |
| Universe | Temperature and weather markets only |
| Candidate quote | Executable ask from 0.90 through 0.97 |
| Order price guard | Candidate ask plus one tick, capped at 0.99 |
| Time window | Close or game start within 6 hours; same-target-day stale weather deadlines may remain eligible while orders are accepted |
| Forecast gate | For fresh entries, model probability must be at least `candidate ask + 0.02` |
| Market activity | At least $50 reported liquidity and $50 reported 24-hour volume |
| Discovery volume | No client-side market-count ceiling; each Gamma query paginates until exhausted |
| Diversification | At most two positions per city and target date |
| Sizing | Equal-weight full deployment, 5% soft line cap and 10% redistribution cap, each floored at $5 for small accounts |
| Exit | Hold until an executable bid of 0.99 or market settlement |
| Stop loss | Disabled for weather positions; the engine never intentionally sells them below entry |

The market can remain open after a nominal end timestamp while an event is resolving. Same-target-day weather markets can therefore remain eligible with a displayed `hours_to_close` of zero when Polymarket still accepts orders. Full deployment is a target, not a promise: cash can remain idle when no eligible or executable market exists.

When a tick has no fresh actionable market, the full-deployment path may add cash to existing eligible lines up to the 10% cap. That held-line redistribution uses a relaxed pool: it retains the weather, price, activity, timing, and accepting-orders checks but disables the forecast-edge and bracket-margin checks for the top-up. It never creates a fresh position.

See [Strategy](docs/STRATEGIES.md) and [Profiles](docs/PROFILES.md) for the complete policy.

## Architecture

```text
Polymarket APIs + Open-Meteo
            │
            ▼
  deterministic scanner
            │
            ▼
 forecast and policy gates
            │
            ▼
 sizing → execution → journal
            │
            ├── Telegram reports
            └── local dashboard
```

The scanner and order path are deterministic. The optional daily improvement process runs outside the live path and is constrained by tests and configuration fences. See [Architecture](docs/ARCHITECTURE.md).

## Requirements

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- A Polymarket account and API credentials for live execution
- USDC and the required venue approvals for live trading

## Installation

```bash
git clone git@github.com:novogratz/polymarket-bot.git
cd polymarket-bot
cp .env.example .env
uv sync --extra dev
uv run pmbot --help
```

Populate `.env` locally. Never commit it or paste credentials into logs, issues, or support messages.

## Safe first run

Inspect the account and run the strategy without submitting orders:

```bash
uv run pmbot status
uv run pmbot positions
uv run pmbot auto-loop --dry-run --profile grinder --run customer-evaluation
```

Run the test suite before enabling live execution:

```bash
uv run python -B -m unittest discover -s tests
```

## Live operation

Live execution requires the explicit `--live` flag. The maintained launcher for bot 1 is:

```bash
bash scripts/run_live_70.sh
```

Bots 2 and 3 use `scripts/run_live_b.sh` and `scripts/run_live_c.sh`. Each launcher selects its own profile, label, and reporting process. The launchers do **not** set distinct live state paths: by default they all use `data/paper_state.json`, `data/trade_journal.jsonl`, and the colocated realized cache. Run them from separate deployments or explicitly set unique state and journal paths before running more than one bot from the same checkout. Stop a foreground stack with `Ctrl+C` and confirm that all child processes exit.

Before every production start:

1. Confirm the intended profile and wallet.
2. Run tests and inspect `pmbot status` and `pmbot positions`.
3. Verify available cash, allowance, and existing positions.
4. Start one stack only for each configured wallet/profile pair.
5. Watch the first complete scan and reconcile its portfolio heartbeat.

The `--yes` option only suppresses the interactive confirmation for maintained automation scripts. It does not enable live trading by itself.

## Data and observability

Runtime state is local and excluded from source control:

- `data/paper_state.json` — live ledger and open-position metadata
- `data/trade_journal.jsonl` — trade lifecycle and realized close records
- `data/decision_journal.jsonl` — per-scan weather outcomes and the deepest decision state reached that tick
- `data/forward_eligible_log.jsonl` — broad near-favorite observations for later reconciliation
- `data/realized_trade_cache.jsonl` — deduplicated closed-trade outcomes
- `data/logs/` — dated process logs
- `data/dry_runs/` — isolated simulation ledgers

Useful commands:

```bash
uv run pmbot status
uv run pmbot positions
uv run pmbot journal-stats
```

See [Operations](docs/OPERATIONS.md) for launch, monitoring, incident, and recovery guidance.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Strategy](docs/STRATEGIES.md)
- [Profiles](docs/PROFILES.md)
- [Operations](docs/OPERATIONS.md)
- [Offline improvement process](docs/AUTONOMY.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [Changelog](CHANGELOG.md)

## License

Released under the [MIT License](LICENSE).
