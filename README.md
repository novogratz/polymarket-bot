# Polymarket Weather Bot

A deterministic Python trading engine for short-dated Polymarket temperature markets. It combines market data, multi-model Open-Meteo forecasts, portfolio controls, live order execution, persistent decision records, and read-only operational reporting.

> [!WARNING]
> Prediction-market trading can result in a complete loss of capital. Forecasts and historical results do not guarantee future performance. Review the strategy, test in dry-run mode, and comply with all laws and platform terms that apply to you.

## Highlights

- Weather-only live universe; cryptocurrency and unrelated markets are excluded.
- Deterministic selection path with no LLM calls during scanning or execution.
- Forecast edge required for every live entry.
- Equal-weight deployment with per-position concentration limits.
- Persistent trade and decision journals for post-trade analysis.
- Three isolated bot profiles, dry-run support, Telegram reporting, and a local dashboard.
- Unit-tested strategy filters, sizing, exits, and reconciliation behavior.

## Strategy at a glance

The live profiles currently apply the same core policy:

| Control | Live policy |
| --- | --- |
| Universe | Temperature and weather markets only |
| Entry ask | 0.90–0.97; absolute cap 0.97 |
| Time window | At most 6 hours before the configured close |
| Forecast gate | Model probability must exceed the ask by at least 2 percentage points |
| Diversification | At most two positions per city and target date |
| Sizing | Equal-weight full deployment, 5% target and 10% hard line cap, subject to a $5 venue minimum |
| Exit | Hold until an executable bid of 0.99 or market settlement |
| Stop loss | Disabled for weather positions; the engine never intentionally sells them below entry |

The market can remain open after a nominal end timestamp while an event is resolving. In that state the bot holds the position instead of forcing a loss. Full deployment is a target, not a promise: cash can remain idle when no eligible or executable market exists.

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

Bots 2 and 3 use `scripts/run_live_b.sh` and `scripts/run_live_c.sh`. Each launcher selects its own profile, ledger, label, and reporting process. Stop a foreground stack with `Ctrl+C` and confirm that all child processes exit.

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
- `data/decision_journal.jsonl` — selected, rejected, missed, and later-resolved decisions
- `data/realized_trade_cache.json` — reconciled closed-trade outcomes
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
