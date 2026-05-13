#!/usr/bin/env bash
# Lance news + edge en DRY-RUN parallèle.
#
# Chaque stratégie a son propre dossier (data/dry_runs/news/ et
# data/dry_runs/edge/) — ledger, journal, équity séparés. Ctrl+C
# arrête les deux. Logs interleaved préfixés [news] / [edge].
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

cleanup() {
    echo ""
    echo "[both] stopping background jobs..."
    kill 0 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

POLYMARKET_QUIET=1 uv run pmbot auto-loop --dry-run --profile news --run news \
    2>&1 | sed -u 's/^/[news] /' &

POLYMARKET_QUIET=1 uv run pmbot auto-loop --dry-run --profile edge --run edge \
    2>&1 | sed -u 's/^/[edge] /' &

wait
