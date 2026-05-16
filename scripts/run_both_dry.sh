#!/usr/bin/env bash
# Curated dry-run race — 26 strategies (20 + 6 claude_baseline A/B variants).
#
# Structure :
#   - 13 base strategies (race-style + edge + random control)
#   - kzerlepgm_baseline : main:baseline + 4h cap (the reference)
#   - 6 smart-money copy-trade entries (smart_money_dry, _loose, hybrid_smart_money,
#     multi_signal_consensus, wallet_cluster_correlation, whale_entry_detection)
#   - 6 claude_baseline_* A/B variants of kzerlepgm_baseline (one dimension each)
#
# Older 52-bot legacy script preserved at scripts/run_both_dry_full.sh.bak.
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

# Crypto-aware (edge has BS-from-vol on BTC/ETH thresholds)
run_bot edge                       edge                       "edge      "

# Smart-money copy-trade family — la vraie pipeline + proxies race-style.
# smart_money_dry         : pipeline canonique (leaderboard + multi-wallet consensus)
# smart_money_loose       : NEW IDEA — pipeline canonique avec consensus=1 + USDC bas
# hybrid_smart_money      : race-style approximation du spec principal
# multi_signal_consensus  : ≥3/4 signaux {momentum, volume, spread, mid-price}
# wallet_cluster_correlation : proxy correlation par volume + momentum
# whale_entry_detection   : volume ≥2.5k + momentum positif (whale-trade proxy)
run_bot kzerlepgm_baseline         kzerlepgm_baseline         "kzer_base "
run_bot smart_money_dry            smart_money_dry            "sm_real   "
run_bot smart_money_loose          smart_money_loose          "sm_loose  "
run_bot hybrid_smart_money         hybrid_smart_money         "sm_hybrid "
run_bot multi_signal_consensus     multi_signal_consensus     "sm_multi  "
run_bot wallet_cluster_correlation wallet_cluster_correlation "sm_cluster"
run_bot whale_entry_detection      whale_entry_detection      "sm_whale  "

# Claude baseline A/B family (chacune varie UNE dimension de kzerlepgm_baseline) :
# tight       : MONTH top 30, $2k PnL / $5k vol / 5% ROI, consensus=3, $150 USDC
# wide        : top 100, $300 PnL / $750 vol / 1% ROI, fallback_consensus=1
# persist     : lucky-month wallet filter ON (WEEK∩MONTH∩ALL)
# fresh       : lookback 30min (vs 240), staleness 3s, TTL 20s
# quick_exit  : SL 25%, peak-protect @+50% exit +20%, trailing @+15%
# let_run     : SL 50%, peak-protect @+200% exit +80%, trailing @+50% giveback 30%
run_bot claude_baseline_tight       claude_baseline_tight       "cb_tight  "
run_bot claude_baseline_wide        claude_baseline_wide        "cb_wide   "
run_bot claude_baseline_persist     claude_baseline_persist     "cb_persist"
run_bot claude_baseline_fresh       claude_baseline_fresh       "cb_fresh  "
run_bot claude_baseline_quick_exit  claude_baseline_quick_exit  "cb_quick  "
run_bot claude_baseline_let_run     claude_baseline_let_run     "cb_letrun "

# Control
run_bot random                     random                     "random    "

POLYMARKET_DRY_RUN=1 uv run pmbot leaderboard \
    --runs weak_holder_flush_inverse,pmlepgm_counter_panic_fade,aggressive_buyer_detection,claude_endgame_sweep,claude_resolution_sniper,claude_blue_chip,favorite,late_favorite,smart_wallet_consensus,championdumonde_breakout,pm_le_pgm_weak_holder_flush_inverse,edge,kzerlepgm_baseline,smart_money_dry,smart_money_loose,hybrid_smart_money,multi_signal_consensus,wallet_cluster_correlation,whale_entry_detection,claude_baseline_tight,claude_baseline_wide,claude_baseline_persist,claude_baseline_fresh,claude_baseline_quick_exit,claude_baseline_let_run,random \
    --interval 3 --telegram \
    2>&1 | sed -u 's/^/[board]     /' &

wait
