#!/usr/bin/env bash
# Lance la stratégie "edge" en LIVE (ordres réels).
#
# Quatre lanes (arb, crypto, near-cert, scalp-off). Cap 15%/trade,
# halt à -20% daily DD, cash floor 10%. Seuls les marchés ≤ 4h.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export POLYMARKET_SYNC_LIVE_POSITIONS=1
export POLYMARKET_QUIET=${POLYMARKET_QUIET:-1}

exec uv run pmbot auto-loop --live --profile edge --yes
