"""Polymarket smart-money copy-trading bot.

This package provides:

- A deterministic smart-money signal engine that ranks profitable leaderboard
  wallets, fetches their recent BUY trades in parallel, and groups them by
  asset to find multi-wallet consensus signals.
- A live order placement layer with conviction-weighted percentage sizing,
  cash-floor targeting, and dynamic per-slot redistribution.
- Multi-level exit logic (take-profit ladder, trailing stop, peak-protect,
  stop-loss, cohort-sell, near-expiry, max-hold-time).
- A persistent JSONL trade journal and a defensive auto-tuner that reads it
  to compute bounded parameter overrides.
- An optional integrated BTC threshold edge model with retry-and-fallback
  Coinbase client.
- A read-only local HTML dashboard.

The trading scan path is deterministic Python over the public Polymarket
APIs. No LLM call is made during scanning or trade selection.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
