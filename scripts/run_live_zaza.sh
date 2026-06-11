#!/usr/bin/env bash
# Windows machine — Zaza's live bot (4th account, wife's wallet).
# Exact copy of run_live_win.sh (bot 3) pointed at profile grinder_zaza.
#
# MUST run from its OWN repo clone (e.g. C:\Users\benoi\polymarket-bot-zaza)
# whose .env holds ZAZA's credentials (private key / funder / API creds).
# Running it from the bot-3 clone would trade the wrong wallet.
# Independent from the other bots — separate account, separate ledger.
set -euo pipefail

# Force UTF-8 output — Windows defaults to cp1252 which chokes on emojis.
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

# Add uv to PATH (Windows: ~/.local/bin not in default PATH for some shells)
export PATH="$HOME/.local/bin:$PATH"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ── WRONG-CLONE GUARD ───────────────────────────────────────────────────
# This script exists in every clone of the repo, but it must only ever run
# from Zaza's clone: the .env decides which WALLET trades and which LEDGER
# is written. Launching from the bot-3 clone would trade bot 3's account
# under Zaza's profile (this happened 2026-06-11). Abort unless the .env
# funder is Zaza's wallet.
ZAZA_FUNDER="0x7cf56dd179bdcbf619b80932210bf890d0e4ad84"
ENV_FUNDER="$(grep -i '^POLYMARKET_FUNDER_ADDRESS=' .env 2>/dev/null | cut -d= -f2 | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
if [ "$ENV_FUNDER" != "$ZAZA_FUNDER" ]; then
    echo "ERROR: this is NOT Zaza's clone — .env funder is '${ENV_FUNDER:-missing}'."
    echo "Run from the zaza clone instead:"
    echo "    cd ~/polymarket-bot-zaza && bash scripts/run_live_zaza.sh"
    exit 1
fi

LOG_DIR="$REPO_ROOT/data/logs"
mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/run_live_zaza_$(date +%Y-%m-%d).log"
LIVE_LOG="$LOG_DIR/live_zaza_$(date +%Y-%m-%d).log"
echo "[run_live_zaza] logging to $RUN_LOG (live also -> $LIVE_LOG)"

export POLYMARKET_SYNC_LIVE_POSITIONS=1

# Zaza's bankroll baseline ($100 per user 2026-06-11; RPC-failure
# fallback — the bot reads the real USDC balance from CLOB each tick; these
# kick in only if that read fails). MUST match grinder_zaza.toml
# starting_cash, else the "since start" % in the LIVE REPORT is skewed.
export POLYMARKET_PAPER_BALANCE_USD=${POLYMARKET_PAPER_BALANCE_USD:-100.0}
export POLYMARKET_ASSUME_LIVE_BALANCE_USD=${POLYMARKET_ASSUME_LIVE_BALANCE_USD:-100.0}

export POLYMARKET_AUTO_INTERVAL_SECONDS=${POLYMARKET_AUTO_INTERVAL_SECONDS:-10}
export POLYMARKET_RACE_DAILY_DRAWDOWN_PCT=0
export TELEGRAM_EQUITY_FLOOR_USD=0

# LIVE REPORT cadence: every 30 minutes (the ONLY Telegram message this stack
# sends). Pinned explicitly so it never drifts from the code default.
export LIVE_ANALYST_CYCLE_SECONDS=${LIVE_ANALYST_CYCLE_SECONDS:-1800}

# SILENCE the live bot entirely. The ONLY message we want is the periodic
# LIVE REPORT from the live_analyst sidecar. These flags default to ON when
# unset, so each one must be set to 0 explicitly.
export TELEGRAM_ALERT_TRADES=0
export TELEGRAM_ALERT_TRADES_BUY=0
export TELEGRAM_ALERT_TRADES_SELL=0
export TELEGRAM_ALERT_ERRORS=0
export TELEGRAM_ALERT_THRESHOLDS=0
export TELEGRAM_ALERT_HEARTBEAT=0
export TELEGRAM_ALERT_PORTFOLIO_UPDATES=0
export TELEGRAM_ALERT_DAILY_SUMMARY=0

# Label = profile TOML name (live_analyst loads configs/profiles/<label>.toml).
# MUST differ from "grinder" so the pkill below never kills bot 3's analyst
# when both bots run on this machine.
export POLYMARKET_PROFILE_LABEL=grinder_zaza

# Name shown in the LIVE REPORT header/footer.
export POLYMARKET_BOT_NAME="Zaza Bot"

cleanup() {
    kill 0 2>&1 || true
    wait 2>&1 || true
}
trap cleanup INT TERM EXIT

# Kill only THIS bot's stale live_analyst (matched by the profile-label tag
# passed on the command line). Scoped so the grinder analysts can coexist
# instead of pkill'ing each other on startup.
pkill -f "live_analyst.py ${POLYMARKET_PROFILE_LABEL}\$" 2>/dev/null || true
sleep 1

uv run python scripts/live_analyst.py "${POLYMARKET_PROFILE_LABEL}" 2>&1 | sed -u 's/^/[live-analyst] /' | tee -a "$RUN_LOG" &

# ─── Dry grinder twin (paper, mirrors the live config for safe compare) ─
# Same grinder_zaza.toml but simulated — never spends real money, writes to
# data/dry_runs/grinder_zaza/. Telegram BUY/SELL silenced so only the live
# bot speaks. Ticks slower (10min) to keep API load down.
POLYMARKET_QUIET=1 \
    POLYMARKET_SUPPRESS_BUY_LOGS=1 \
    POLYMARKET_AUTO_INTERVAL_SECONDS=600 \
    TELEGRAM_ALERT_TRADES=0 TELEGRAM_ALERT_TRADES_BUY=0 TELEGRAM_ALERT_TRADES_SELL=0 \
    TELEGRAM_ALERT_ERRORS=0 TELEGRAM_ALERT_THRESHOLDS=0 TELEGRAM_ALERT_HEARTBEAT=0 \
    TELEGRAM_ALERT_PORTFOLIO_UPDATES=0 TELEGRAM_ALERT_DAILY_SUMMARY=0 \
    uv run pmbot auto-loop --dry-run --profile grinder_zaza --run grinder_zaza \
    2>&1 | sed -u 's/^/[dry-grinder] /' | tee -a "$RUN_LOG" &

# ─── Autonomous report sidecar (deterministic — NO codex/claude/ollama) ─
# Dry-run Telegram silenced — live-only mode. Remove the override to re-enable.
TELEGRAM_CHAT_ID_DRY_RUN="" \
    uv run python scripts/dry_analyst.py 2>&1 | sed -u 's/^/[analyst] /' | tee -a "$RUN_LOG" &

uv run pmbot auto-loop --live --profile grinder_zaza --yes \
    2>&1 | sed -u 's/^/[LIVE] /' | tee -a "$LIVE_LOG" "$RUN_LOG"
