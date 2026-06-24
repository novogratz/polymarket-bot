#!/usr/bin/env bash
# watchdog_b.sh — keeps run_live_b.sh alive 24/7.
#
# On crash or clean exit the bot restarts after a 30-second cooldown.
# Install as a macOS LaunchAgent (see scripts/com.polymarket.grinder-b.plist)
# so it survives reboots:
#
#   cp scripts/com.polymarket.grinder-b.plist ~/Library/LaunchAgents/
#   launchctl load ~/Library/LaunchAgents/com.polymarket.grinder-b.plist
#
# Uninstall:
#   launchctl unload ~/Library/LaunchAgents/com.polymarket.grinder-b.plist
#   rm ~/Library/LaunchAgents/com.polymarket.grinder-b.plist

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

mkdir -p data/logs
LOG="data/logs/watchdog_b.log"

log() { echo "[watchdog $(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

log "=== watchdog_b started (PID $$) — will restart bot on any exit ==="

RESTART_COUNT=0
while true; do
    log "Starting live bot (attempt $((RESTART_COUNT + 1)))..."
    bash scripts/run_live_b.sh || true
    RESTART_COUNT=$((RESTART_COUNT + 1))
    log "Live bot exited (total restarts so far: $RESTART_COUNT). Waiting 30s before restart..."
    sleep 30
done
