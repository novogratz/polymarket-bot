#!/usr/bin/env bash
# La grande race dry-run — toutes les stratégies + leaderboard sidecar.
#
# Each $100 starting cash, own ledger + journal. Per-trade Telegram alerts
# are fully suppressed (TELEGRAM_ALERT_TRADES=0); the only Telegram output
# is the 15-min leaderboard refresh.
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
    POLYMARKET_QUIET=1 \
        POLYMARKET_SUPPRESS_BUY_LOGS=1 \
        TELEGRAM_ALERT_TRADES=0 \
        TELEGRAM_ALERT_TRADES_BUY=0 \
        TELEGRAM_ALERT_HEARTBEAT=0 \
        TELEGRAM_ALERT_THRESHOLDS=0 \
        TELEGRAM_ALERT_ERRORS=0 \
        uv run pmbot auto-loop --dry-run --profile "$profile" --run "$run" \
        2>&1 | sed -u "s/^/[${prefix}] /" &
}

# Original 10
run_bot news                       news                       "news      "
run_bot edge                       edge                       "edge      "
run_bot baseline                   baseline                   "baseline  "
run_bot random                     random                     "random    "
run_bot contrarian                 contrarian                 "contra    "
run_bot favorite                   favorite                   "favorite  "
run_bot championdumonde_breakout   championdumonde_breakout   "cdm_break "
run_bot late_favorite              late_favorite              "late_fav  "
run_bot panic_fade                 panic_fade                 "panic_fade"
run_bot underdog                   underdog                   "underdog  "
run_bot pmlepgm_counter_panic_fade pmlepgm_counter_panic_fade "cpanic    "

# Framework-rules 18 (race-mode approximations of the spec)
run_bot hybrid_smart_money         hybrid_smart_money         "hybrid    "
run_bot smart_wallet_consensus     smart_wallet_consensus     "swallet   "
run_bot whale_entry_detection      whale_entry_detection      "whale     "
run_bot wallet_cluster_correlation wallet_cluster_correlation "wcluster  "
run_bot early_momentum_detection   early_momentum_detection   "earlymom  "
run_bot liquidity_vacuum_breakout  liquidity_vacuum_breakout  "vacuum    "
run_bot mean_reversion_fade        mean_reversion_fade        "meanrev   "
run_bot range_channel_trading      range_channel_trading      "range     "
run_bot aggressive_buyer_detection aggressive_buyer_detection "aggbuy    "
run_bot orderbook_imbalance        orderbook_imbalance        "obimb     "
run_bot late_momentum_chase        late_momentum_chase        "latemom   "
run_bot weak_holder_flush          weak_holder_flush          "weakhold  "
run_bot probability_drift          probability_drift          "probdrift "
run_bot resolution_compression     resolution_compression     "rescomp   "
run_bot liquidity_absorption       liquidity_absorption       "liqabsorb "
run_bot momentum_exhaustion_reversal momentum_exhaustion_reversal "momexh    "
run_bot micro_scalping             micro_scalping             "micro     "
run_bot multi_signal_consensus     multi_signal_consensus     "multisig  "

# kzer paired arb (dry-run scanner — live path not wired)
run_bot kzerlepgm_ultimatestrategy kzerlepgm_ultimatestrategy "kzer      "

POLYMARKET_DRY_RUN=1 uv run pmbot leaderboard \
    --runs news,edge,baseline,random,contrarian,favorite,championdumonde_breakout,late_favorite,panic_fade,underdog,pmlepgm_counter_panic_fade,hybrid_smart_money,smart_wallet_consensus,whale_entry_detection,wallet_cluster_correlation,early_momentum_detection,liquidity_vacuum_breakout,mean_reversion_fade,range_channel_trading,aggressive_buyer_detection,orderbook_imbalance,late_momentum_chase,weak_holder_flush,probability_drift,resolution_compression,liquidity_absorption,momentum_exhaustion_reversal,micro_scalping,multi_signal_consensus,kzerlepgm_ultimatestrategy \
    --interval 15 --telegram \
    2>&1 | sed -u 's/^/[board]     /' &

wait
