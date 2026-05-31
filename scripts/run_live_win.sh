#!/usr/bin/env bash
# Windows machine — 3rd account live bot.
# Wallet: 0x8eA51Ad57de9010816990E580F8114E786807646
# Balance: $15.80 (2026-05-31 fresh start)
# Independent from Mac bots — separate account, separate ledger.
set -euo pipefail

# Force UTF-8 output — Windows defaults to cp1252 which chokes on emojis.
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

# Add uv to PATH (Windows: ~/.local/bin not in default PATH for some shells)
export PATH="$HOME/.local/bin:$PATH"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

LOG_DIR="$REPO_ROOT/data/logs"
mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/run_live_win_$(date +%Y-%m-%d).log"
LIVE_LOG="$LOG_DIR/live_win_$(date +%Y-%m-%d).log"
echo "[run_live_win] logging to $RUN_LOG (live also -> $LIVE_LOG)"

export POLYMARKET_SYNC_LIVE_POSITIONS=1

# Bankroll baseline — $15.80 (2026-05-31).
export POLYMARKET_PAPER_BALANCE_USD=${POLYMARKET_PAPER_BALANCE_USD:-12.67}
export POLYMARKET_ASSUME_LIVE_BALANCE_USD=${POLYMARKET_ASSUME_LIVE_BALANCE_USD:-12.67}

export POLYMARKET_AUTO_INTERVAL_SECONDS=${POLYMARKET_AUTO_INTERVAL_SECONDS:-10}
export POLYMARKET_RACE_DAILY_DRAWDOWN_PCT=0
export TELEGRAM_EQUITY_FLOOR_USD=0

export TELEGRAM_ALERT_TRADES=1
export TELEGRAM_ALERT_TRADES_BUY=1
export TELEGRAM_ALERT_ERRORS=1
export TELEGRAM_ALERT_THRESHOLDS=1
export TELEGRAM_ALERT_HEARTBEAT=1
export TELEGRAM_ALERT_PORTFOLIO_UPDATES=1
export TELEGRAM_ALERT_DAILY_SUMMARY=1

export POLYMARKET_PROFILE_LABEL=grinder

cleanup() {
    kill 0 2>&1 || true
    wait 2>&1 || true
}
trap cleanup INT TERM EXIT

pkill -f "live_analyst.py" 2>/dev/null || true
sleep 1

uv run python scripts/live_analyst.py 2>&1 | sed -u 's/^/[live-analyst] /' | tee -a "$RUN_LOG" &

POLYMARKET_QUIET=1 \
    POLYMARKET_SUPPRESS_BUY_LOGS=1 \
    POLYMARKET_AUTO_INTERVAL_SECONDS=600 \
    TELEGRAM_ALERT_TRADES=0 TELEGRAM_ALERT_TRADES_BUY=0 TELEGRAM_ALERT_TRADES_SELL=0 \
    TELEGRAM_ALERT_ERRORS=0 TELEGRAM_ALERT_THRESHOLDS=0 TELEGRAM_ALERT_HEARTBEAT=0 \
    TELEGRAM_ALERT_PORTFOLIO_UPDATES=0 TELEGRAM_ALERT_DAILY_SUMMARY=0 \
    uv run pmbot auto-loop --dry-run --profile grinder --run grinder \
    2>&1 | sed -u 's/^/[dry-grinder] /' | tee -a "$RUN_LOG" &

TELEGRAM_CHAT_ID_DRY_RUN="" \
    uv run python scripts/dry_analyst.py 2>&1 | sed -u 's/^/[analyst] /' | tee -a "$RUN_LOG" &

uv run pmbot auto-loop --live --profile grinder --yes \
    2>&1 | sed -u 's/^/[LIVE] /' | tee -a "$LIVE_LOG" "$RUN_LOG"
