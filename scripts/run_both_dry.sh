#!/usr/bin/env bash
# La grande race dry-run — 62 strategies, full restore + 9 new entries.
#
# Composition:
#   - 52 legacy bots (race-style strategies — Gamma scan only, no leaderboard)
#   - 9 smart-money-pipeline bots (kzerlepgm_baseline, smart_money_*,
#     claude_baseline_*) — these have staggered intervals 60-130s in their
#     profiles to avoid 429 rate limits on the Polymarket leaderboard API.
#   - 1 random control
#
# Each $100 starting cash, own ledger + journal. Telegram alerts stream to
# TELEGRAM_CHAT_ID_DRY_RUN. Ctrl+C stops everything. Launch is staggered
# (sleep 1.5s between bots) to spread initial API requests.
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
    sleep 1.5
}

# ─── Original 10 + cpanic ─────────────────────────────────────────────
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

# ─── Framework rules 18 (race-mode approximations) ───────────────────
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
run_bot weak_holder_flush_inverse  weak_holder_flush_inverse  "weakhold_i"
run_bot pm_le_pgm_weak_holder_flush_inverse pm_le_pgm_weak_holder_flush_inverse "pm_whfi   "

# ─── Claude race batch (20) ──────────────────────────────────────────
run_bot claude_anti_favorite       claude_anti_favorite       "cl_antifav"
run_bot claude_mid_dump_fade       claude_mid_dump_fade       "cl_dumpfd "
run_bot claude_resolution_sniper   claude_resolution_sniper   "cl_snipe  "
run_bot claude_high_vol_quiet      claude_high_vol_quiet      "cl_hv_qt  "
run_bot claude_lottery_balanced    claude_lottery_balanced    "cl_lottery"
run_bot claude_strong_breakout     claude_strong_breakout     "cl_strong "
run_bot claude_frozen_favorite     claude_frozen_favorite     "cl_frozen "
run_bot claude_mid_rebound         claude_mid_rebound         "cl_rebd   "
run_bot claude_high_vol_panic      claude_high_vol_panic      "cl_hv_pnc "
run_bot claude_high_vol_pop        claude_high_vol_pop        "cl_hv_pop "
run_bot claude_oversold_bounce     claude_oversold_bounce     "cl_bounce "
run_bot claude_late_pump           claude_late_pump           "cl_pump   "
run_bot claude_extreme_consensus   claude_extreme_consensus   "cl_xtreme "
run_bot claude_balanced_mid        claude_balanced_mid        "cl_mid    "
run_bot claude_resolution_clock    claude_resolution_clock    "cl_clock  "
run_bot claude_endgame_sweep       claude_endgame_sweep       "cl_endgame"
run_bot claude_fade_extreme        claude_fade_extreme        "cl_fade   "
run_bot claude_mid_volume_band     claude_mid_volume_band     "cl_midvol "
run_bot claude_blue_chip           claude_blue_chip           "cl_blue   "
run_bot claude_volume_spike        claude_volume_spike        "cl_spike  "
run_bot claude_mid_endgame         claude_mid_endgame         "cl_midend "

# ─── Additional research-backed race strategies ──────────────────────
run_bot probability_drift          probability_drift          "probdrift "
run_bot resolution_compression     resolution_compression     "rescomp   "
run_bot liquidity_absorption       liquidity_absorption       "liqabsorb "
run_bot momentum_exhaustion_reversal momentum_exhaustion_reversal "momexh    "
run_bot micro_scalping             micro_scalping             "micro     "
run_bot multi_signal_consensus     multi_signal_consensus     "multisig  "

# ─── kzer paired arb (dry-run scanner) ───────────────────────────────
run_bot kzerlepgm_ultimatestrategy kzerlepgm_ultimatestrategy "kzer      "

# ─── Smart-money copy-trade family (real leaderboard pipeline) ───────
# Profiles have staggered auto_interval_seconds (60-130s) to keep
# Polymarket leaderboard API requests below the 429 threshold.
run_bot kzerlepgm_baseline         kzerlepgm_baseline         "kzer_base "
run_bot smart_money_dry            smart_money_dry            "sm_real   "
run_bot smart_money_loose          smart_money_loose          "sm_loose  "

# ─── Claude baseline A/B variants (each flips ONE dimension) ─────────
run_bot claude_baseline_tight       claude_baseline_tight       "cb_tight  "
run_bot claude_baseline_wide        claude_baseline_wide        "cb_wide   "
run_bot claude_baseline_persist     claude_baseline_persist     "cb_persist"
run_bot claude_baseline_fresh       claude_baseline_fresh       "cb_fresh  "
run_bot claude_baseline_quick_exit  claude_baseline_quick_exit  "cb_quick  "
run_bot claude_baseline_let_run     claude_baseline_let_run     "cb_letrun "

# ─── Autonomous analyst sidecar ──────────────────────────────────────
# Every 15 min, reads per-strategy state, calls claude CLI for insights,
# may SPAWN new auto_* strategies (TOML-only, additive) and KILL its
# own underperformers (≥10 closed trades, ROI ≤ -10%, win_rate ≤ 35%).
# Will NEVER touch human-curated bots. Posts to Telegram dry-run channel.
# Kill-switch: write {"enabled": false} to data/autonomous_state.json
python3 scripts/dry_analyst.py 2>&1 | sed -u 's/^/[analyst]   /' &

POLYMARKET_DRY_RUN=1 uv run pmbot leaderboard \
    --runs news,edge,baseline,random,contrarian,favorite,championdumonde_breakout,late_favorite,panic_fade,underdog,pmlepgm_counter_panic_fade,hybrid_smart_money,smart_wallet_consensus,whale_entry_detection,wallet_cluster_correlation,early_momentum_detection,liquidity_vacuum_breakout,mean_reversion_fade,range_channel_trading,aggressive_buyer_detection,orderbook_imbalance,late_momentum_chase,weak_holder_flush,weak_holder_flush_inverse,pm_le_pgm_weak_holder_flush_inverse,claude_anti_favorite,claude_mid_dump_fade,claude_resolution_sniper,claude_high_vol_quiet,claude_lottery_balanced,claude_strong_breakout,claude_frozen_favorite,claude_mid_rebound,claude_high_vol_panic,claude_high_vol_pop,claude_oversold_bounce,claude_late_pump,claude_extreme_consensus,claude_balanced_mid,claude_resolution_clock,claude_endgame_sweep,claude_fade_extreme,claude_mid_volume_band,claude_blue_chip,claude_volume_spike,claude_mid_endgame,probability_drift,resolution_compression,liquidity_absorption,momentum_exhaustion_reversal,micro_scalping,multi_signal_consensus,kzerlepgm_ultimatestrategy,kzerlepgm_baseline,smart_money_dry,smart_money_loose,claude_baseline_tight,claude_baseline_wide,claude_baseline_persist,claude_baseline_fresh,claude_baseline_quick_exit,claude_baseline_let_run \
    --interval 3 --telegram \
    2>&1 | sed -u 's/^/[board]     /' &

wait
