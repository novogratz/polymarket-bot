#!/usr/bin/env bash
# Lance le bot en LIVE avec le profil SMART-MONEY (copy-trading).
# Toute la config vit dans configs/profiles/smart.toml.
#
# Alternative à run_live_70.sh (grinder). NE PAS lancer les deux en même temps :
# ils partagent le même compte et le même ledger (data/paper_state.json).
# Choisis l'UN ou l'AUTRE.
#
# Comme run_live_70.sh, ce script passe --yes (pas de prompt TTY). C'est le
# seul usage sanctionné du flag --yes (cf. CLAUDE.md).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

LOG_DIR="$REPO_ROOT/data/logs"
mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/run_live_smart_$(date +%Y-%m-%d).log"
LIVE_LOG="$LOG_DIR/live_smart_$(date +%Y-%m-%d).log"
echo "[run_live_smart] logging to $RUN_LOG (live also -> $LIVE_LOG)"

# Sync live positions into the local ledger each tick.
export POLYMARKET_SYNC_LIVE_POSITIONS=1

# Live bankroll baseline = $65.78 (2026-05-30). RPC-failure fallback only —
# the bot reads the real USDC balance from CLOB each tick; these cap if it fails.
export POLYMARKET_PAPER_BALANCE_USD=${POLYMARKET_PAPER_BALANCE_USD:-65.78}
export POLYMARKET_ASSUME_LIVE_BALANCE_USD=${POLYMARKET_ASSUME_LIVE_BALANCE_USD:-65.78}

# 30s tick — smart-money fetches the leaderboard each tick, so don't hammer it.
export POLYMARKET_AUTO_INTERVAL_SECONDS=${POLYMARKET_AUTO_INTERVAL_SECONDS:-30}

# Daily drawdown halt at 15% of starting equity (~$9.87). The smart-money tick
# now honours this knob (added 2026-05-30) — exits still run, only NEW entries
# pause for the rest of the day once breached.
export POLYMARKET_RACE_DAILY_DRAWDOWN_PCT=${POLYMARKET_RACE_DAILY_DRAWDOWN_PCT:-0.15}

# Telegram: push everything live.
export TELEGRAM_ALERT_TRADES=1
export TELEGRAM_ALERT_TRADES_BUY=1
export TELEGRAM_ALERT_ERRORS=1
export TELEGRAM_ALERT_THRESHOLDS=1
export TELEGRAM_ALERT_HEARTBEAT=1
export TELEGRAM_ALERT_PORTFOLIO_UPDATES=1
export TELEGRAM_ALERT_DAILY_SUMMARY=1

# Profile label exported BEFORE the live_analyst spawns (else "(unknown)").
export POLYMARKET_PROFILE_LABEL=smart

# Clean up the whole process group on Ctrl+C / TERM (idempotent).
cleanup() {
    kill 0 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

# ─── Live analyst sidecar (read-only, posts to TELEGRAM_CHAT_ID_LIVE) ──
python3 scripts/live_analyst.py 2>&1 | sed -u 's/^/[live-analyst] /' | tee -a "$RUN_LOG" &

# ─── Live-only leaderboard sidecar (Telegram every 5 min) ──────────────
uv run pmbot leaderboard --live-only --interval 5 --telegram \
    2>&1 | sed -u 's/^/[board] /' | tee -a "$RUN_LOG" &

# ─── Dry smart twin (paper) — same smart.toml, simulated, for safe compare ─
# Writes to data/dry_runs/smart/. Telegram silenced so only the live bot speaks.
# Slower tick (10min) to keep leaderboard API load down. Watch this to see the
# strategy's real signal rate before trusting the live lane.
POLYMARKET_QUIET=1 \
    POLYMARKET_SUPPRESS_BUY_LOGS=1 \
    POLYMARKET_AUTO_INTERVAL_SECONDS=600 \
    TELEGRAM_ALERT_TRADES=0 TELEGRAM_ALERT_TRADES_BUY=0 TELEGRAM_ALERT_TRADES_SELL=0 \
    TELEGRAM_ALERT_ERRORS=0 TELEGRAM_ALERT_THRESHOLDS=0 TELEGRAM_ALERT_HEARTBEAT=0 \
    TELEGRAM_ALERT_PORTFOLIO_UPDATES=0 TELEGRAM_ALERT_DAILY_SUMMARY=0 \
    uv run pmbot auto-loop --dry-run --profile smart --run smart \
    2>&1 | sed -u 's/^/[dry-smart] /' | tee -a "$RUN_LOG" &

# ─── Autonomous report sidecar (deterministic — NO AI) ─────────────────
TELEGRAM_CHAT_ID_DRY_RUN="" \
    uv run python scripts/dry_analyst.py 2>&1 | sed -u 's/^/[analyst] /' | tee -a "$RUN_LOG" &

# ─── LIVE smart-money bot ──────────────────────────────────────────────
uv run pmbot auto-loop --live --profile smart --yes \
    2>&1 | sed -u 's/^/[LIVE] /' | tee -a "$LIVE_LOG" "$RUN_LOG"
