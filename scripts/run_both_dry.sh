#!/usr/bin/env bash
# Curated dry-run race — 12 strategies selected for clean signal.
#
# Why 12: 52 strategies was too noisy and most were variations of the
# same thesis. This set keeps the dry-validated winners, the research-
# backed ones, distinct theses, and a random control. The full 52-bot
# legacy script is preserved at scripts/run_both_dry_full.sh.bak.
#
# Each $20 starting cash, own ledger + journal. Telegram alerts stream
# to TELEGRAM_CHAT_ID_DRY_RUN. Ctrl+C stops everything.
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
    POLYMARKET_QUIET=1 \
        POLYMARKET_SUPPRESS_BUY_LOGS=1 \
        uv run pmbot auto-loop --dry-run --profile "$profile" --run "$run" \
        2>&1 | sed -u "s/^/[${prefix}] /" &
}

# Dry-validated winners (60%+ wr in earlier sample)
run_bot weak_holder_flush_inverse  weak_holder_flush_inverse  "whfi      "
run_bot pmlepgm_counter_panic_fade pmlepgm_counter_panic_fade "cpanic    "
run_bot aggressive_buyer_detection aggressive_buyer_detection "aggbuy    "

# Research-backed (calibration data / smart-money literature)
run_bot claude_endgame_sweep       claude_endgame_sweep       "cl_endgame"
run_bot claude_resolution_sniper   claude_resolution_sniper   "cl_snipe  "
run_bot claude_blue_chip           claude_blue_chip           "cl_blue   "

# Distinct theses to A/B against
run_bot favorite                   favorite                   "favorite  "
run_bot late_favorite              late_favorite              "late_fav  "
run_bot smart_wallet_consensus     smart_wallet_consensus     "swallet   "
run_bot championdumonde_breakout   championdumonde_breakout   "cdm_break "

# Current live strategy (track its dry twin)
run_bot pm_le_pgm_weak_holder_flush_inverse pm_le_pgm_weak_holder_flush_inverse "pm_whfi   "

# Control
run_bot random                     random                     "random    "

POLYMARKET_DRY_RUN=1 uv run pmbot leaderboard \
    --runs weak_holder_flush_inverse,pmlepgm_counter_panic_fade,aggressive_buyer_detection,claude_endgame_sweep,claude_resolution_sniper,claude_blue_chip,favorite,late_favorite,smart_wallet_consensus,championdumonde_breakout,pm_le_pgm_weak_holder_flush_inverse,random \
    --interval 3 --telegram \
    2>&1 | sed -u 's/^/[board]     /' &

wait
