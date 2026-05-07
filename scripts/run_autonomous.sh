#!/usr/bin/env bash
# Persistent autonomous runner: restarts the live bot on crash,
# rotates logs daily, can be left running in the background.
#
# Foreground:  bash scripts/run_autonomous.sh
# Background:  nohup bash scripts/run_autonomous.sh > /dev/null 2>&1 &
# Watch logs:  tail -F data/logs/auto-$(date +%Y%m%d).log
# Stop:        pkill -f run_autonomous.sh ; pkill -f polymarket_bot.main

set -u

cd "$(dirname "$0")/.."

mkdir -p data/logs

RESTART_BACKOFF_SECONDS=${POLYMARKET_AUTONOMOUS_BACKOFF_SECONDS:-30}
MAX_BACKOFF_SECONDS=${POLYMARKET_AUTONOMOUS_MAX_BACKOFF_SECONDS:-300}

current_log() {
    echo "data/logs/auto-$(date +%Y%m%d).log"
}

log() {
    local message="$1"
    local log_path
    log_path="$(current_log)"
    printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$message" | tee -a "$log_path"
}

trap 'log "received SIGTERM/SIGINT, exiting autonomous wrapper"; exit 0' TERM INT

log "starting autonomous loop (PID $$). Bot script: scripts/run_live_70.sh"

backoff=$RESTART_BACKOFF_SECONDS
while true; do
    log_path="$(current_log)"
    log "starting bot tick loop"
    bash scripts/run_live_70.sh >> "$log_path" 2>&1
    exit_code=$?
    if [ "$exit_code" -eq 0 ]; then
        log "bot exited cleanly (code 0), restarting in ${RESTART_BACKOFF_SECONDS}s"
        backoff=$RESTART_BACKOFF_SECONDS
    else
        log "bot crashed (code $exit_code), restarting in ${backoff}s"
    fi
    sleep "$backoff"
    if [ "$exit_code" -ne 0 ]; then
        backoff=$(( backoff * 2 ))
        if [ "$backoff" -gt "$MAX_BACKOFF_SECONDS" ]; then
            backoff=$MAX_BACKOFF_SECONDS
        fi
    fi
done
