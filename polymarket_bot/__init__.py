"""Deterministic Polymarket weather trading engine.

This package provides:

- Forecast-gated selection for short-dated temperature markets.
- Percentage-based portfolio sizing and live order execution.
- Persistent JSONL trade and decision journals.
- Three aligned production profiles and isolated dry-run tooling.
- Read-only reporting and dashboard surfaces.

The trading scan path is deterministic Python over the public Polymarket
APIs. No LLM call is made during scanning or trade selection.
"""

__all__ = ["__version__"]

__version__ = "6.1.0"
