#!/usr/bin/env bash
# Lance la stratégie "edge" en DRY-RUN.
#
# Quatre lanes : arb, crypto directional, near-cert, scalp (off).
# Seule contrainte dure: marchés expirant dans ≤ 4h.
# Sizing fractional-Kelly, cap 15%/trade, halt à -20% daily DD.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export POLYMARKET_QUIET=${POLYMARKET_QUIET:-1}

exec uv run pmbot auto-loop --dry-run --profile edge --run edge
