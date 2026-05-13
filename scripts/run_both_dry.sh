#!/usr/bin/env bash
# Lance news + edge en DRY-RUN parallèle, plus un sidecar leaderboard.
#
# Trois processus :
#   1. [news]  → data/dry_runs/news/
#   2. [edge]  → data/dry_runs/edge/
#   3. [board] → leaderboard refresh toutes les 15 min
#
# Ctrl+C arrête tout. Logs interleaved préfixés.
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
    2>&1 | sed -u 's/^/[news]  /' &

POLYMARKET_QUIET=1 uv run pmbot auto-loop --dry-run --profile edge --run edge \
    2>&1 | sed -u 's/^/[edge]  /' &

# Sidecar: scoreboard every 15 minutes. Picks up new runs automatically
# (no restart needed) if they appear under data/dry_runs/.
uv run pmbot leaderboard --runs news,edge --interval 15 \
    2>&1 | sed -u 's/^/[board] /' &

wait
