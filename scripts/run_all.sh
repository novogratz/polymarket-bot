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

# Drive all dry profiles that exist in configs/profiles/ except:
#  - copy-* (mirror mode, needs target wallet)
#  - the live profile (would compete with live for the same state path)
#  - test.toml
#  - live-90.toml (smart_money live config)
for f in configs/profiles/*.toml; do
    name=$(basename "$f" .toml)
    case "$name" in
        copy-*|test|live-90|aggressive-live) continue ;;
        auto_baseline_tight_microladder) continue ;;  # live's twin already running
    esac
    # Use the strategy name as both --profile and --run, padded to 10 chars
    prefix=$(printf "%-10s" "${name:0:10}")
    run_dry_bot "$name" "$name" "$prefix"
done

echo "[run_all] dry race launched ($(ls configs/profiles/*.toml | wc -l) profiles), slowed to 10min/tick"
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
