#!/usr/bin/env bash
# Lance live + dry race dans un seul script, avec cache HTTP partagé.
#
# Order of operations (cache-first):
#   1. Pre-warm HTTP cache (~60s) — populates data/cache/http/ with
#      leaderboards + wallet trade histories so BOTH live and dry start
#      with a warm cache, no first-tick 429 storm
#   2. Launch live bot (10s tick, hits warm cache immediately)
#   3. Launch dry race (auto-discovered profiles, 10min tick)
#   4. Launch sidecars: analyst + live-analyst + leaderboard
#   5. Background cache refresher loops every 3min to keep cache warm
#      (TTL is 10min, so 3min window catches wallets that missed previous passes)
#   6. Ctrl+C kill tout proprement (process group)

# NOTE: no `set -u` — bash strict mode crashed on harmless unset vars
# (e.g. LAUNCHED in the summary echo) and triggered the EXIT trap which
# kills all bots. set -e stays to catch real failures.
set -eo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Fresh-start bankroll baseline.
# By default, bots use the starting_cash defined in their TOML profiles.
# Set these env vars to override the bankroll for BOTH live and dry
# if you want to test a specific global baseline.
# export POLYMARKET_PAPER_BALANCE_USD=${POLYMARKET_PAPER_BALANCE_USD:-43.0}
# export POLYMARKET_ASSUME_LIVE_BALANCE_USD=${POLYMARKET_ASSUME_LIVE_BALANCE_USD:-43.0}

# Dry → live grinder mirror: when the dry grinder twin opens a fresh BUY, the
# live grinder takes it on its next tick (re-validated against the live quote).
# Exported here so BOTH the live bot and the dry subshells inherit it.
export POLYMARKET_LIVE_MIRROR_DRY=${POLYMARKET_LIVE_MIRROR_DRY:-1}

# Daily logs: everything that scrolls on screen is also appended to a dated
# file under data/logs/ so we can grep [LIVE]/[analyst]/etc. when debugging.
LOG_DIR="$REPO_ROOT/data/logs"
mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/run_all_$(date +%Y-%m-%d).log"
LIVE_LOG="$LOG_DIR/live_$(date +%Y-%m-%d).log"
echo "[run_all] logging to $RUN_LOG (live also -> $LIVE_LOG)"

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
uv run python scripts/cache_warmer.py 2>&1 | sed -u 's/^/[cache] /' | tee -a "$RUN_LOG" || true
echo

# ─── Step 1.5: Rebuild live ledger from Polymarket reality ──────────
# Wipes data/paper_state.json + data/live_baseline.json and rotates
# data/trade_journal.jsonl, then rebuilds cash + open positions from
# the CLOB. Catches drift from previous runs (ghost position records,
# missed sync imports, stale baseline causing phantom ROI) so the bot
# starts every session with a ledger that matches reality.
#
# Set POLYMARKET_SKIP_LEDGER_RESET=1 to skip this step and preserve
# the existing ledger / journal / baseline. Use when swapping live
# profiles mid-session and you want the leaderboard ROI to continue
# from its existing baseline instead of restarting at 0%.
if [ "${POLYMARKET_SKIP_LEDGER_RESET:-0}" = "1" ]; then
    echo "[run_all] step 1.5/4: skipped (POLYMARKET_SKIP_LEDGER_RESET=1 — preserving existing ledger/journal/baseline)"
else
    echo "[run_all] step 1.5/4: rebuilding live ledger from Polymarket..."
    uv run pmbot reset-ledger 2>&1 | sed -u 's/^/[reset] /' | tee -a "$RUN_LOG" || true
fi
echo

# ─── Step 2: LIVE bot (priority, fast tick, cache pre-populated) ────
echo "[run_all] step 2/4: launching live bot (grinder)..."

export POLYMARKET_SYNC_LIVE_POSITIONS=1
export POLYMARKET_AUTO_INTERVAL_SECONDS=30   # grinder tick = 30s
export POLYMARKET_PROFILE_LABEL=grinder

# Live Telegram alerts ON
export TELEGRAM_ALERT_TRADES=1
export TELEGRAM_ALERT_TRADES_BUY=1
export TELEGRAM_ALERT_ERRORS=1
export TELEGRAM_ALERT_THRESHOLDS=1
export TELEGRAM_ALERT_HEARTBEAT=1
export TELEGRAM_ALERT_PORTFOLIO_UPDATES=1
export TELEGRAM_ALERT_DAILY_SUMMARY=1

# Live analyst sidecar (read-only Telegram every 30min)
uv run python scripts/live_analyst.py 2>&1 | sed -u 's/^/[live-analyst] /' | tee -a "$RUN_LOG" &

# Live bot itself
uv run pmbot auto-loop --live --profile grinder --yes \
    2>&1 | sed -u 's/^/[LIVE] /' | tee -a "$LIVE_LOG" "$RUN_LOG" &
LIVE_PID=$!
echo "[run_all] live bot launched (pid=$LIVE_PID)"
echo

# ─── Step 3: DRY race (slowed — every dry bot ticks at 10min) ───────
echo "[run_all] step 3/4: launching dry race (auto-discovered profiles, tick 10min)..."

export ANALYST_CYCLE_SECONDS=${ANALYST_CYCLE_SECONDS:-900}
export ANALYST_SPAWN_KILL_INTERVAL_SECONDS=${ANALYST_SPAWN_KILL_INTERVAL_SECONDS:-3600}

run_dry_bot() {
    local profile="$1"
    local run="$2"
    local prefix="$3"

    # Use a faster interval for the dry twin of the live profile (matching live speed)
    # to provide a real-time comparison. Others stay at 10min to save resources/API.
    local interval=600
    if [ "$profile" = "$POLYMARKET_PROFILE_LABEL" ]; then
        interval=30
    fi

    # Per-subshell env: dry bots silent on Telegram BUY/SELL (live keeps alerts).
    POLYMARKET_QUIET=1 \
        POLYMARKET_SUPPRESS_BUY_LOGS=1 \
        POLYMARKET_AUTO_INTERVAL_SECONDS=$interval \
        TELEGRAM_ALERT_TRADES=0 \
        TELEGRAM_ALERT_TRADES_BUY=0 \
        TELEGRAM_ALERT_TRADES_SELL=0 \
        TELEGRAM_ALERT_ERRORS=0 \
        TELEGRAM_ALERT_THRESHOLDS=0 \
        TELEGRAM_ALERT_HEARTBEAT=0 \
        TELEGRAM_ALERT_PORTFOLIO_UPDATES=0 \
        TELEGRAM_ALERT_DAILY_SUMMARY=0 \
        uv run pmbot auto-loop --dry-run --profile "$profile" --run "$run" \
        2>&1 | sed -u "s/^/[${prefix}] /" | tee -a "$RUN_LOG" &
    sleep 0.5
}

# Auto-discover all profiles in configs/profiles/ (except special ones).
# macOS bash 3.2: no mapfile, use a loop.
SKIP_PROFILES="copy-wallet live-90"
DRY_PROFILES=()
for f in configs/profiles/*.toml; do
    name=$(basename "$f" .toml)
    skip=0
    for sp in $SKIP_PROFILES; do
        if [ "$name" = "$sp" ]; then skip=1; break; fi
    done
    if [ "$skip" = "0" ]; then
        DRY_PROFILES+=("$name")
    fi
done

LAUNCHED=0
MISSING_COUNT=0
for name in "${DRY_PROFILES[@]}"; do
    if [ ! -f "configs/profiles/${name}.toml" ]; then
        # Profile was archived by the dry-analyst (or never shipped).
        # Loud-log so the user can see WHY the dry race has fewer bots
        # than expected — silent skip used to hide the fact that 45 of
        # 55 dry profiles had been auto-killed overnight.
        echo "[run_all]   skip ${name}: profile archived or missing"
        MISSING_COUNT=$((MISSING_COUNT + 1))
        continue
    fi
    # No skip for the live profile — running it in dry too gives a
    # direct apples-to-apples comparison line on the leaderboard. Live
    # and dry use separate state files (paper_state.json vs
    # data/dry_runs/<name>/state.json) so they don't conflict.
    prefix=$(printf "%-10s" "${name:0:10}")
    run_dry_bot "$name" "$name" "$prefix"
    LAUNCHED=$((LAUNCHED + 1))
done
echo "[run_all] dry race launched: $LAUNCHED bots, tick 10min (${MISSING_COUNT} profiles skipped — archived or missing)"
echo

# ─── Step 4: Sidecars (analyst + leaderboard + promoter) ────────────
echo "[run_all] step 4/4: launching sidecars..."
TELEGRAM_CHAT_ID_DRY_RUN="" \
    uv run python scripts/dry_analyst.py 2>&1 | sed -u 's/^/[analyst] /' | tee -a "$RUN_LOG" &
POLYMARKET_DRY_RUN=1 uv run pmbot leaderboard \
    --auto-discover --interval 10 \
    2>&1 | sed -u 's/^/[board] /' | tee -a "$RUN_LOG" &

# Live profile auto-promoter: watches the dry leaderboard every 5min, writes
# data/live_active_profile.json when a profile crosses promotion gates
# (≥30 closed, ROI≥+10%, WR≥55%, positive realized PnL, no one-trade
# wonder, drawdown cap, 1h cooldown, ≤4 swaps/day). The live bot
# reads that file each tick and hot-swaps profiles in-process — no restart
# needed. If no profile qualifies, the promoter does nothing (correct
# behavior — promoting losers loses real money).
uv run python scripts/live_promoter.py 2>&1 | sed -u 's/^/[promoter] /' | tee -a "$RUN_LOG" &

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
        uv run python scripts/cache_warmer.py 2>&1 | sed -u 's/^/[cache-refresh] /' | tee -a "$RUN_LOG" || true
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
echo "    1× promoter          (auto-swap live profile when dry winner found)"
echo "    1× cache-refresher   (re-warms cache every 3min, TTL 10min)"
echo
echo "  Cache shared at: data/cache/http/"
echo "  Ctrl+C to stop everything."
echo "═══════════════════════════════════════════════════════════════"

wait
