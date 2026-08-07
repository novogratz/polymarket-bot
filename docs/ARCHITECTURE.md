# Architecture

The application separates market discovery, deterministic decision-making, execution, persistence, and reporting so that each layer can be tested and audited independently.

## Runtime flow

```text
Gamma/CLOB market data      Open-Meteo models
          │                       │
          └──────────┬────────────┘
                     ▼
             market normalization
                     ▼
          weather and forecast gates
                     ▼
          portfolio-aware allocation
                     ▼
              CLOB order execution
                     ▼
        ledger + journal + reconciliation
                     ▼
          CLI, logs, Telegram, dashboard
```

## Components

- `polymarket_bot/` contains the CLI, configuration model, market clients, strategy engine, execution logic, ledgers, reporting, and dashboard.
- `polymarket_bot/weather_forecast.py` maps supported temperature contracts to multi-model forecast probabilities.
- `configs/profiles/` contains versioned strategy defaults.
- `scripts/run_live_*.sh` compose one live process with its reporting and analysis sidecars.
- `scripts/` also contains reconciliation, reporting, backtest, and maintenance utilities.
- `tests/` covers strategy behavior and operational invariants.

## State boundaries

Source-controlled files define code and profile defaults. Local state is stored under `data/` and credentials in `.env`; both are excluded from version control. The maintained launchers do not assign unique live state paths and therefore default to the same `data/paper_state.json` and `data/trade_journal.jsonl`. Multiple bots must run from separate deployments or receive explicit unique state, journal, realized-cache, decision-log, and tick-state paths.

The live ledger is an operational cache, not the ultimate source of truth. Portfolio reporting reconciles it with venue balances and positions. A mismatch is surfaced in the heartbeat and must be investigated before relying on local equity totals.

## Safety boundaries

- Live orders require `pmbot auto-loop --live`.
- Maintained automation passes `--yes` only after the launcher has selected an explicit production profile.
- Missing forecasts fail closed for fresh forecast-gated entries. Held-line leftover-cash redistribution deliberately disables the forecast-edge and bracket-margin gates for top-ups only.
- Every strategy behavior change requires a unit test.
- LLM-assisted analysis, when enabled, is offline and fenced from live selection.

## External dependencies

The bot depends on Polymarket availability, Polygon RPC access, Open-Meteo responses, and local network connectivity. Any of these can be delayed, incomplete, or inconsistent. The execution path therefore validates current order-book data immediately before placing an order.
