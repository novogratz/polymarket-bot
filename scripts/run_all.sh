#!/usr/bin/env bash
# Lance live + dry race dans un seul script, avec coordination API:
#
#   1. Live bot tourne en priorité (tick 10s, accès API quasi-prioritaire)
#   2. La dry race tourne en arrière-plan, RALENTIE (tick 600s = 10min)
#      pour ne pas saturer le quota Polymarket data-api
#   3. Analyst sidecars (live + dry) tournent comme d'habitude
#   4. Ctrl+C kill tout proprement
#
# Pourquoi le ralentissement dry: 130+ bots dry × 60s tick × 50 wallets
# = ~6,500 calls/min qui faisaient 429er les fetches du live. Avec un
# tick de 10min (1/10e), la dry race fait ~650 calls/min — la live a
# la marge nécessaire pour fetcher ses propres signaux proprement.
#
# Trade-off: la dry race converge 10x plus lentement, mais on continue
# à collecter de la data sur 130 strategies sans nuire au live.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

cleanup() {
    echo ""
    echo "[run_all] stopping all bots..."
    kill 0 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "═══════════════════════════════════════════════════════════════"
echo "  🟢 LIVE + DRY race lancés ensemble — Ctrl+C pour tout stopper"
echo "═══════════════════════════════════════════════════════════════"
echo

# ─── Step 1: LIVE bot (priority — fast tick, full API access) ────────
echo "[run_all] launching live bot (auto_baseline_tight_microladder)..."

export POLYMARKET_SYNC_LIVE_POSITIONS=1
export POLYMARKET_AUTO_INTERVAL_SECONDS=10   # live tick = 10s
export POLYMARKET_PROFILE_LABEL=auto_baseline_tight_microladder

# Live Telegram alerts ON
export TELEGRAM_ALERT_TRADES=1
export TELEGRAM_ALERT_TRADES_BUY=1
export TELEGRAM_ALERT_ERRORS=1
export TELEGRAM_ALERT_THRESHOLDS=1
export TELEGRAM_ALERT_HEARTBEAT=1
export TELEGRAM_ALERT_PORTFOLIO_UPDATES=1
export TELEGRAM_ALERT_DAILY_SUMMARY=1

# Live analyst sidecar (read-only Telegram every 30min)
python3 scripts/live_analyst.py 2>&1 | sed -u 's/^/[live-analyst] /' &

# Live bot itself
uv run pmbot auto-loop --live --profile auto_baseline_tight_microladder --yes \
    2>&1 | sed -u 's/^/[LIVE] /' &
LIVE_PID=$!

# Let live warm up + grab its share of the API quota before we add dry bots
echo "[run_all] live bot launching (pid=$LIVE_PID) — waiting 30s for warmup..."
sleep 30

# ─── Step 2: DRY race (slowed down — every dry bot ticks at 10min) ──
echo "[run_all] launching dry race (slowed to 10min/tick for API quota)..."

# Override every dry bot's interval at the env-var level. The profile's
# auto_interval_seconds becomes a default; this env var wins.
# Most smart_money dry bots: 60-130s → forced to 600s here.
# Race-style dry bots: 30-45s → forced to 600s here.
export ANALYST_CYCLE_SECONDS=${ANALYST_CYCLE_SECONDS:-900}   # 15min reports (default)
export ANALYST_SPAWN_KILL_INTERVAL_SECONDS=${ANALYST_SPAWN_KILL_INTERVAL_SECONDS:-3600}

run_dry_bot() {
    local profile="$1"
    local run="$2"
    local prefix="$3"
    POLYMARKET_QUIET=1 \
        POLYMARKET_SUPPRESS_BUY_LOGS=1 \
        POLYMARKET_AUTO_INTERVAL_SECONDS=600 \
        uv run pmbot auto-loop --dry-run --profile "$profile" --run "$run" \
        2>&1 | sed -u "s/^/[${prefix}] /" &
    sleep 0.5
}

# Curated dry roster (~50 instead of 195) — focus on representative
# strategies. Auto-spawned variants (~110 auto_*) are skipped here;
# the dry_analyst will spawn its own auto_* on top if it sees signal.
#
# Pattern: keep one representative per "family" so the dry race
# tests breadth without 110 near-duplicates competing for API quota.

DRY_PROFILES=(
    # Baseline family (the proven winners)
    baseline kzerlepgm_baseline
    claude_baseline_tight claude_baseline_fresh claude_baseline_persist
    claude_baseline_quick_exit claude_baseline_let_run
    # Smart-money + insider
    smart_money_dry smart_money_loose
    insider_whales insider_millionaires
    # Race strategies (one per thesis)
    aggressive_buyer_detection hybrid_smart_money smart_wallet_consensus
    whale_entry_detection wallet_cluster_correlation
    early_momentum_detection mean_reversion_fade
    pmlepgm_counter_panic_fade pm_le_pgm_weak_holder_flush_inverse
    weak_holder_flush_inverse championdumonde_breakout
    favorite contrarian
    # Claude race batch (representative — most are similar)
    claude_anti_favorite claude_mid_dump_fade claude_resolution_sniper
    claude_strong_breakout claude_frozen_favorite claude_mid_rebound
    claude_oversold_bounce claude_late_pump claude_extreme_consensus
    claude_balanced_mid claude_endgame_sweep claude_fade_extreme
    claude_blue_chip claude_high_vol_quiet claude_high_vol_pop
    # Momentum family (user's favorite thesis)
    momentum_breakout_aggressive momentum_breakout_defensive
    momentum_strong_continuation momentum_early_letrun
    momentum_volume_spike_safe momentum_exhaustion_fade
    momentum_panic_continuation momentum_high_vol_pop_micro
    # Other
    probability_drift liquidity_absorption momentum_exhaustion_reversal
    micro_scalping multi_signal_consensus
    kzerlepgm_ultimatestrategy
    # Control
    random
)

# Filter to only those that actually exist (some may be archived)
LAUNCHED=0
for name in "${DRY_PROFILES[@]}"; do
    [ -f "configs/profiles/${name}.toml" ] || continue
    [ "$name" = "auto_baseline_tight_microladder" ] && continue
    prefix=$(printf "%-10s" "${name:0:10}")
    run_dry_bot "$name" "$name" "$prefix"
    LAUNCHED=$((LAUNCHED + 1))
done

echo "[run_all] dry race launched ($LAUNCHED curated profiles, tick 10min) — analyst will spawn auto_* on top"
echo

# ─── Step 3: Autonomous analyst sidecar ──────────────────────────────
python3 scripts/dry_analyst.py 2>&1 | sed -u 's/^/[analyst] /' &

# ─── Step 4: Leaderboard sidecar ─────────────────────────────────────
POLYMARKET_DRY_RUN=1 uv run pmbot leaderboard \
    --auto-discover --interval 5 --telegram \
    2>&1 | sed -u 's/^/[board] /' &

echo "═══════════════════════════════════════════════════════════════"
echo "  Bot setup complete. All processes:"
echo "    1× LIVE bot  (10s tick, real money)"
echo "    1× live-analyst (30min Telegram reports)"
echo "    N× DRY bots  (10min tick, simulated)"
echo "    1× analyst   (15min reports, 1h spawn/kill)"
echo "    1× leaderboard (5min Telegram leaderboard)"
echo
echo "  Ctrl+C to stop everything."
echo "═══════════════════════════════════════════════════════════════"

# Wait for all background processes
wait
