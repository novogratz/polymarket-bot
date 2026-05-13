#!/usr/bin/env bash
# Lance la race dry-run : news + edge + baseline + sidecar leaderboard.
#
# Quatre processus :
#   1. [news]     → data/dry_runs/news/      (momentum 4h)
#   2. [edge]     → data/dry_runs/edge/      (arb + crypto + near-cert)
#   3. [baseline] → data/dry_runs/baseline/  (smart-money default)
#   4. [board]    → leaderboard refresh toutes les 15 min (+ Telegram si configuré)
#
# Ctrl+C arrête tout. Logs interleaved préfixés.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

cleanup() {
    echo ""
    echo "[race] stopping background jobs..."
    kill 0 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

POLYMARKET_QUIET=1 uv run pmbot auto-loop --dry-run --profile news --run news \
    2>&1 | sed -u 's/^/[news]     /' &

POLYMARKET_QUIET=1 uv run pmbot auto-loop --dry-run --profile edge --run edge \
    2>&1 | sed -u 's/^/[edge]     /' &

POLYMARKET_QUIET=1 uv run pmbot auto-loop --dry-run --profile baseline --run baseline \
    2>&1 | sed -u 's/^/[baseline] /' &

# Sidecar leaderboard with Telegram broadcast every 15 min.
uv run pmbot leaderboard --runs news,edge,baseline --interval 15 --telegram \
    2>&1 | sed -u 's/^/[board]    /' &

wait
