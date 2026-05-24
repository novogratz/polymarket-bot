#!/usr/bin/env bash
# Dry-run race across the curated active profile set.
#
# This is dry-only. It starts one named dry-run per curated profile under
# data/dry_runs/<profile>/, then starts the dry leaderboard sidecar. Ctrl+C
# stops the whole process group.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CLEANED_UP=0
cleanup() {
    [ "$CLEANED_UP" = "1" ] && return 0
    CLEANED_UP=1
    echo ""
    echo "[race] stopping all background jobs..."
    kill 0 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

run_bot() {
    local profile="$1"
    local prefix="$2"
    POLYMARKET_QUIET=1 \
        POLYMARKET_SUPPRESS_BUY_LOGS=1 \
        POLYMARKET_PAPER_BALANCE_USD=20.0 \
        POLYMARKET_ASSUME_LIVE_BALANCE_USD=20.0 \
        POLYMARKET_AUTO_INTERVAL_SECONDS="${POLYMARKET_DRY_RACE_INTERVAL_SECONDS:-300}" \
        TELEGRAM_ALERT_TRADES=0 \
        TELEGRAM_ALERT_TRADES_BUY=0 \
        TELEGRAM_ALERT_TRADES_SELL=0 \
        TELEGRAM_ALERT_ERRORS=0 \
        TELEGRAM_ALERT_THRESHOLDS=0 \
        TELEGRAM_ALERT_HEARTBEAT=0 \
        TELEGRAM_ALERT_PORTFOLIO_UPDATES=0 \
        TELEGRAM_ALERT_DAILY_SUMMARY=0 \
        uv run pmbot auto-loop --dry-run --profile "$profile" --run "$profile" \
        2>&1 | sed -u "s/^/[${prefix}] /" &
    sleep "${POLYMARKET_DRY_RACE_LAUNCH_STAGGER_SECONDS:-2}"
}

# Auto-discover all profiles in configs/profiles/ (except special ones).
SKIP_PROFILES="copy-wallet live-90"
mapfile -t profiles < <(
    for f in configs/profiles/*.toml; do
        name=$(basename "$f" .toml)
        skip=0
        for sp in $SKIP_PROFILES; do
            if [ "$name" = "$sp" ]; then skip=1; break; fi
        done
        if [ "$skip" = "0" ]; then echo "$name"; fi
    done
)

echo "[race] launching ${#profiles[@]} dry-run bots (auto-discovered)"
echo "[race] dry only: no --live process will be started"
echo "[race] bankroll: \$20 per run | interval: ${POLYMARKET_DRY_RACE_INTERVAL_SECONDS:-300}s"
echo "[race] Ctrl+C stops the race"

launched=0
for profile in "${profiles[@]}"; do
    prefix="$(printf '%-12s' "${profile:0:12}")"
    run_bot "$profile" "$prefix"
    launched=$((launched + 1))
done

echo "[race] launched $launched dry-run bots"
echo "[race] starting dry leaderboard sidecar"

POLYMARKET_DRY_RUN=1 uv run pmbot leaderboard \
    --auto-discover \
    --interval "${POLYMARKET_DRY_RACE_LEADERBOARD_INTERVAL_MINUTES:-3}" \
    --telegram \
    2>&1 | sed -u 's/^/[board]       /' &

wait
