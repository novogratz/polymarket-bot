#!/usr/bin/env bash
# Lance live + dry race dans un seul script, avec cache HTTP partagé.
#
# Order of operations (cache-first):
#   1. Pre-warm HTTP cache (~60s) — populates data/cache/http/ with
#      leaderboards + wallet trade histories so BOTH live and dry start
#      with a warm cache, no first-tick 429 storm
#   2. Launch live bot (10s tick, hits warm cache immediately)
#   3. Launch dry race (~50 curated profiles, 10min tick)
#   4. Launch sidecars: analyst + live-analyst + leaderboard
#   5. Background cache refresher loops every 8min to keep cache warm
#      (TTL is 10min, so refresh at 8min ensures no gap)
#   6. Ctrl+C kill tout proprement (process group)

# NOTE: no `set -u` — bash strict mode crashed on harmless unset vars
# (e.g. LAUNCHED in the summary echo) and triggered the EXIT trap which
# kills all bots. set -e stays to catch real failures.
set -eo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CLEANED_UP=0
cleanup() {
    # Idempotent: trap fires on multiple signals + EXIT; only do this once
    [ "$CLEANED_UP" = "1" ] && return 0
    CLEANED_UP=1
    echo ""
    echo "[run_all] stopping all bots..."
    kill 0 2>/dev/null || true
    wait 2>/dev/null || true
}
# Only INT/TERM (user-initiated), not EXIT — avoids tearing everything
# down on a harmless script-level error. If user wants to stop, Ctrl+C.
trap cleanup INT TERM

echo "═══════════════════════════════════════════════════════════════"
echo "  🟢 LIVE + DRY race lancés ensemble — Ctrl+C pour tout stopper"
echo "═══════════════════════════════════════════════════════════════"
echo

# ─── Step 1: Pre-warm HTTP cache (BEFORE anything else) ─────────────
# Populates data/cache/http/ so live + dry both find the data cached
# on their very first tick. No 429 storm.
echo "[run_all] step 1/4: pre-warming HTTP cache (~60s)..."
uv run python scripts/cache_warmer.py 2>&1 | sed -u 's/^/[cache] /' || true
echo

# ─── Step 1.5: Rebuild live ledger from Polymarket reality ──────────
# Wipes data/paper_state.json (LIVE only — dry ledgers untouched) and
# rebuilds cash + open positions from the CLOB. Catches any drift from
# previous runs (e.g. ghost position records from the pre-fix stacking
# bug, missed sync imports, etc.) so the live bot starts every session
# with a ledger that matches reality. Journal is a separate file and
# stays intact.
echo "[run_all] step 1.5/4: rebuilding live ledger from Polymarket..."
uv run pmbot reset-ledger 2>&1 | sed -u 's/^/[reset] /' || true
echo

# ─── Step 2: LIVE bot (priority, fast tick, cache pre-populated) ────
echo "[run_all] step 2/4: launching live bot (claude_baseline_persist)..."

export POLYMARKET_SYNC_LIVE_POSITIONS=1
export POLYMARKET_AUTO_INTERVAL_SECONDS=10   # live tick = 10s
export POLYMARKET_PROFILE_LABEL=claude_baseline_persist

# Live Telegram alerts ON
export TELEGRAM_ALERT_TRADES=1
export TELEGRAM_ALERT_TRADES_BUY=1
export TELEGRAM_ALERT_ERRORS=1
export TELEGRAM_ALERT_THRESHOLDS=1
export TELEGRAM_ALERT_HEARTBEAT=1
export TELEGRAM_ALERT_PORTFOLIO_UPDATES=1
export TELEGRAM_ALERT_DAILY_SUMMARY=1

# Live analyst sidecar (read-only Telegram every 30min)
uv run python scripts/live_analyst.py 2>&1 | sed -u 's/^/[live-analyst] /' &

# Live bot itself
uv run pmbot auto-loop --live --profile claude_baseline_persist --yes \
    2>&1 | sed -u 's/^/[LIVE] /' &
LIVE_PID=$!
echo "[run_all] live bot launched (pid=$LIVE_PID)"
echo

# ─── Step 3: DRY race (slowed — every dry bot ticks at 10min) ───────
echo "[run_all] step 3/4: launching dry race (50 curated, tick 10min)..."

export ANALYST_CYCLE_SECONDS=${ANALYST_CYCLE_SECONDS:-900}
export ANALYST_SPAWN_KILL_INTERVAL_SECONDS=${ANALYST_SPAWN_KILL_INTERVAL_SECONDS:-3600}

run_dry_bot() {
    local profile="$1"
    local run="$2"
    local prefix="$3"
    # Per-subshell env: dry bots silent on Telegram BUY/SELL (live keeps alerts)
    POLYMARKET_QUIET=1 \
        POLYMARKET_SUPPRESS_BUY_LOGS=1 \
        POLYMARKET_AUTO_INTERVAL_SECONDS=600 \
        TELEGRAM_ALERT_TRADES=0 \
        TELEGRAM_ALERT_TRADES_BUY=0 \
        TELEGRAM_ALERT_TRADES_SELL=0 \
        TELEGRAM_ALERT_ERRORS=0 \
        TELEGRAM_ALERT_THRESHOLDS=0 \
        TELEGRAM_ALERT_HEARTBEAT=0 \
        TELEGRAM_ALERT_PORTFOLIO_UPDATES=0 \
        TELEGRAM_ALERT_DAILY_SUMMARY=0 \
        uv run pmbot auto-loop --dry-run --profile "$profile" --run "$run" \
        2>&1 | sed -u "s/^/[${prefix}] /" &
    sleep 0.5
}

DRY_PROFILES=(
    # Baseline family
    baseline kzerlepgm_baseline
    claude_baseline_tight claude_baseline_fresh claude_baseline_persist
    claude_baseline_quick_exit claude_baseline_let_run
    # Smart-money + insider
    smart_money_dry smart_money_loose
    insider_whales insider_millionaires
    # Race strategies (one per thesis)
    aggressive_buyer_detection hybrid_smart_money smart_wallet_consensus
    whale_entry_detection wallet_cluster_correlation auto_mombreak_locktight
    early_momentum_detection mean_reversion_fade
    pmlepgm_counter_panic_fade pm_le_pgm_weak_holder_flush_inverse
    weak_holder_flush_inverse championdumonde_breakout
    favorite contrarian
    # Claude race batch
    claude_anti_favorite claude_mid_dump_fade claude_resolution_sniper
    claude_strong_breakout claude_frozen_favorite claude_mid_rebound
    claude_oversold_bounce claude_late_pump claude_extreme_consensus
    claude_balanced_mid claude_endgame_sweep claude_fade_extreme
    claude_blue_chip claude_high_vol_quiet claude_high_vol_pop
    # Momentum family
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

LAUNCHED=0
for name in "${DRY_PROFILES[@]}"; do
    [ -f "configs/profiles/${name}.toml" ] || continue
    [ "$name" = "claude_baseline_persist" ] && continue
    prefix=$(printf "%-10s" "${name:0:10}")
    run_dry_bot "$name" "$name" "$prefix"
    LAUNCHED=$((LAUNCHED + 1))
done
echo "[run_all] dry race launched: $LAUNCHED bots, tick 10min"
echo

# ─── Step 4: Sidecars (analyst + leaderboard) ───────────────────────
echo "[run_all] step 4/4: launching sidecars..."
uv run python scripts/dry_analyst.py 2>&1 | sed -u 's/^/[analyst] /' &
POLYMARKET_DRY_RUN=1 uv run pmbot leaderboard \
    --auto-discover --interval 5 --telegram \
    2>&1 | sed -u 's/^/[board] /' &

# ─── Background: refresh the cache every 3 min (TTL is 10min) ───────
# Aggressive refresh interval: even though the cache TTL is 10min, ~25%
# of wallets fail to populate on each warmer pass (data-api 429s during
# the parallel burst). Re-running every 3 min means a wallet that
# missed two consecutive passes still gets retried within 6 min, well
# under the 10min TTL on the entries that did populate. Net effect:
# steadier coverage of the 786-wallet set the dry race needs.
(
    while true; do
        sleep 180  # 3 min
        uv run python scripts/cache_warmer.py 2>&1 | sed -u 's/^/[cache-refresh] /' || true
    done
) &
CACHE_REFRESHER_PID=$!

echo "═══════════════════════════════════════════════════════════════"
echo "  Bot setup complete:"
echo "    1× LIVE bot          (10s tick, real money, cache pre-warm)"
echo "    1× live-analyst      (30min Telegram reports)"
echo "    $LAUNCHED× DRY bots         (10min tick, simulated)"
echo "    1× analyst           (15min reports, 1h spawn/kill)"
echo "    1× leaderboard       (5min Telegram leaderboard)"
echo "    1× cache-refresher   (re-warms cache every 3min, TTL 10min)"
echo
echo "  Cache shared at: data/cache/http/"
echo "  Ctrl+C to stop everything."
echo "═══════════════════════════════════════════════════════════════"

wait
