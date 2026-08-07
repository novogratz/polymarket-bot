# Strategy Evaluation

The repository includes tooling for comparing deterministic strategy profiles in dry-run mode. This evaluation lane is separate from production execution and must not automatically promote a strategy to live trading.

## Evaluation principles

- Use isolated ledgers for every profile.
- Compare realized return, drawdown, calibration, execution rate, and concentration—not win rate alone.
- Require a meaningful number of resolved trades before drawing conclusions.
- Include missed and rejected opportunities from the decision journal to detect overly restrictive filters.
- Account for fees, spread, fill probability, and unresolved capital.
- Prefer walk-forward and out-of-sample evaluation over tuning on the same observations.

## Promotion checklist

A candidate strategy is eligible for review only when:

1. Its behavior is deterministic and documented.
2. Entry, sizing, and exit behavior have focused tests.
3. Results remain acceptable across multiple dates, cities, and market conditions.
4. Maximum loss and exposure are understood.
5. A reviewer approves the configuration change through a pull request.

The dry-run leaderboard is an operational aid, not evidence that a strategy will be profitable in production.
