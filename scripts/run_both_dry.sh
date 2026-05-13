#!/usr/bin/env bash
# La grande race dry-run — 10 stratégies + leaderboard sidecar.
#
# Each $100 starting cash, own ledger + journal:
#   1.  [news]       — momentum on near-expiry markets
#   2.  [edge]       — multi-lane: arb + crypto + near-cert
#   3.  [baseline]   — smart-money default (conservative)
#   4.  [random]     — control: random picks
#   5.  [contra]     — bet against today's momentum
#   6.  [favorite]   — buy heavy favorites (bid ≥ 0.65)
#   7.  [breakout]   — momentum + volume confirmed
#   8.  [late_fav]   — favorites < 30min to expiry
#   9.  [panic_fade] — fade extreme intraday moves (≥15¢)
#   10. [underdog]   — buy rising underdogs (ask ≤ 0.30 + momentum)
#
# Plus :
#   [board]          — leaderboard refresh every 15 min (+ Telegram)
#
# Ctrl+C arrête tout. Logs interleaved préfixés.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

cleanup() {
    echo ""
    echo "[race] stopping all background jobs..."
    kill 0 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

run_bot() {
    local profile="$1"
    local run="$2"
    local prefix="$3"
    POLYMARKET_QUIET=1 POLYMARKET_SUPPRESS_BUY_LOGS=1 \
        uv run pmbot auto-loop --dry-run --profile "$profile" --run "$run" \
        2>&1 | sed -u "s/^/[${prefix}] /" &
}

run_bot news          news          "news      "
run_bot edge          edge          "edge      "
run_bot baseline      baseline      "baseline  "
run_bot random        random        "random    "
run_bot contrarian    contrarian    "contra    "
run_bot favorite      favorite      "favorite  "
run_bot breakout      breakout      "breakout  "
run_bot late_favorite late_favorite "late_fav  "
run_bot panic_fade    panic_fade    "panic_fade"
run_bot underdog      underdog      "underdog  "

POLYMARKET_DRY_RUN=1 uv run pmbot leaderboard \
    --runs news,edge,baseline,random,contrarian,favorite,breakout,late_favorite,panic_fade,underdog \
    --interval 15 --telegram \
    2>&1 | sed -u 's/^/[board]     /' &

wait
