#!/usr/bin/env bash
# Lance BOT 2 en LIVE avec le profil COPY-TRADING (smart_b.toml).
#
# Bot 1 (run_live_70.sh, grinder) et bot 3 (run_live_b.sh, grinder) ne sont PAS
# touchés : ce lanceur n'utilise que configs/profiles/smart_b.toml, qui est
# propre à bot 2. NE PAS lancer ce script en même temps qu'un lanceur grinder
# sur la MÊME machine/compte : ils partagent le ledger (data/paper_state.json).
#
# Comme les autres lanceurs live, il passe --yes (pas de prompt TTY) — usage
# sanctionné du flag (cf. CLAUDE.md).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

LOG_DIR="$REPO_ROOT/data/logs"
mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/run_live_copy_b_$(date +%Y-%m-%d).log"
LIVE_LOG="$LOG_DIR/live_copy_b_$(date +%Y-%m-%d).log"
echo "[run_live_copy_b] logging to $RUN_LOG (live also -> $LIVE_LOG)"

# Sync live positions into the local ledger each tick.
export POLYMARKET_SYNC_LIVE_POSITIONS=1

# RPC-failure fallback only — the bot reads the real USDC balance from CLOB
# each tick. Per-machine baseline lives in data/starting_cash.txt.
export POLYMARKET_PAPER_BALANCE_USD=${POLYMARKET_PAPER_BALANCE_USD:-65.0}
export POLYMARKET_ASSUME_LIVE_BALANCE_USD=${POLYMARKET_ASSUME_LIVE_BALANCE_USD:-65.0}

# 30s tick — copy lane fetches the leaderboard each tick, so don't hammer it.
export POLYMARKET_AUTO_INTERVAL_SECONDS=${POLYMARKET_AUTO_INTERVAL_SECONDS:-30}

# Daily drawdown halt at 15% of starting equity. Exits still run; only NEW
# entries pause for the rest of the day once breached.
export POLYMARKET_RACE_DAILY_DRAWDOWN_PCT=${POLYMARKET_RACE_DAILY_DRAWDOWN_PCT:-0.15}

# Telegram: live reporting only (the deterministic live_analyst sidecar).
# Per-trade BUY/SELL alerts off, matching the other live launchers.
export TELEGRAM_ALERT_TRADES=0
export TELEGRAM_ALERT_TRADES_BUY=0
export TELEGRAM_ALERT_TRADES_SELL=0
export TELEGRAM_ALERT_ERRORS=1
export TELEGRAM_ALERT_THRESHOLDS=0
export TELEGRAM_ALERT_HEARTBEAT=0
export TELEGRAM_ALERT_PORTFOLIO_UPDATES=0
export TELEGRAM_ALERT_DAILY_SUMMARY=0

# Profile label exported BEFORE the live_analyst spawns (else "(unknown)").
export POLYMARKET_PROFILE_LABEL=copy_b

# Clean up the whole process group on Ctrl+C / TERM (idempotent).
cleanup() {
    kill 0 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

# ─── Pre-warm HTTP cache (~60s) ────────────────────────────────────────
# Copy lane fetches the leaderboard every tick. Populate data/cache/http/
# first so the cold start doesn't 429-storm the data-api and find 0 signals
# for the first few minutes. The cache layer (TTL 600s) dedups thereafter.
echo "[run_live_copy_b] pre-warming HTTP cache (~60s)..."
uv run python scripts/cache_warmer.py 2>&1 | sed -u 's/^/[cache] /' | tee -a "$RUN_LOG" || true

# Re-warm every 8 min (cache TTL is 10 min) to keep live + dry twin warm.
(
    while true; do
        sleep 480
        uv run python scripts/cache_warmer.py 2>&1 | sed -u 's/^/[cache-refresh] /' | tee -a "$RUN_LOG" || true
    done
) &

# ─── Live analyst sidecar (read-only, posts to TELEGRAM_CHAT_ID_LIVE) ──
python3 scripts/live_analyst.py 2>&1 | sed -u 's/^/[live-analyst] /' | tee -a "$RUN_LOG" &

# ─── Dry copy twin (paper) — same smart_b.toml, simulated, for safe compare ─
# Writes to data/dry_runs/copy_b/. Telegram silenced so only the live bot
# speaks. Slower tick (10min) to keep leaderboard API load down. Watch this to
# see the strategy's real signal rate before trusting the live lane.
POLYMARKET_QUIET=1 \
    POLYMARKET_SUPPRESS_BUY_LOGS=1 \
    POLYMARKET_AUTO_INTERVAL_SECONDS=600 \
    TELEGRAM_ALERT_TRADES=0 TELEGRAM_ALERT_TRADES_BUY=0 TELEGRAM_ALERT_TRADES_SELL=0 \
    TELEGRAM_ALERT_ERRORS=0 TELEGRAM_ALERT_THRESHOLDS=0 TELEGRAM_ALERT_HEARTBEAT=0 \
    TELEGRAM_ALERT_PORTFOLIO_UPDATES=0 TELEGRAM_ALERT_DAILY_SUMMARY=0 \
    uv run pmbot auto-loop --dry-run --profile smart_b --run copy_b \
    2>&1 | sed -u 's/^/[dry-copy] /' | tee -a "$RUN_LOG" &

# ─── LIVE copy-trading bot ─────────────────────────────────────────────
uv run pmbot auto-loop --live --profile smart_b --yes \
    2>&1 | sed -u 's/^/[LIVE] /' | tee -a "$LIVE_LOG" "$RUN_LOG"
