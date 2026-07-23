#!/usr/bin/env bash
# Lance le bot en LIVE avec le profil grinder courant.
# Toute la config vit dans configs/profiles/grinder.toml.
#
# Ce script passe --yes : la confirmation interactive est skipée, donc aucun
# besoin de TTY. Pour une exécution sans --yes (auto-loop --live tout court),
# l'opérateur DOIT être attaché à un TTY ; sans cela, prompt_live_confirmation
# refuse et abort proprement (cf. live_confirm.py:48).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Daily logs: tee everything to a dated file under data/logs/ for debugging.
LOG_DIR="$REPO_ROOT/data/logs"
mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/run_live_$(date +%Y-%m-%d).log"
LIVE_LOG="$LOG_DIR/live_$(date +%Y-%m-%d).log"
echo "[run_live] logging to $RUN_LOG (live also -> $LIVE_LOG)"

# Sync live positions (toggle hors schéma).
export POLYMARKET_SYNC_LIVE_POSITIONS=1

# SSL: point stdlib urllib (used by the live position/data-api reads) at
# certifi's CA bundle. macOS + uv-managed Python ships no system CA store, so
# without this the data-api reads fail with CERTIFICATE_VERIFY_FAILED. Computed
# at runtime so the path stays correct on any machine.
export SSL_CERT_FILE="${SSL_CERT_FILE:-$(uv run python -c 'import certifi; print(certifi.where())' 2>/dev/null)}"
export REQUESTS_CA_BUNDLE="${REQUESTS_CA_BUNDLE:-$SSL_CERT_FILE}"

# Bot 3 bankroll fallback = $15 (baseline reset 2026-07-23).
# The bot reads the real USDC balance from CLOB each tick; these only kick in
# if that read fails. MUST match grinder_c.toml starting_cash and
# data/starting_cash.txt — a mismatched fallback skews "depuis le début" %.
export POLYMARKET_PAPER_BALANCE_USD=${POLYMARKET_PAPER_BALANCE_USD:-15.0}
export POLYMARKET_ASSUME_LIVE_BALANCE_USD=${POLYMARKET_ASSUME_LIVE_BALANCE_USD:-15.0}

# 10s tick — 3× faster than 30s, catches more fleeting band entries.
export POLYMARKET_AUTO_INTERVAL_SECONDS=${POLYMARKET_AUTO_INTERVAL_SECONDS:-10}

# Daily drawdown halt DISABLED (2026-06-07 per user) — 0 = no entry pause.
export POLYMARKET_RACE_DAILY_DRAWDOWN_PCT=${POLYMARKET_RACE_DAILY_DRAWDOWN_PCT:-0}

# Disable floor alert — local ledger cash is lower than real CLOB balance
# (force-close scripts corrupted it). Real equity is read from CLOB each tick.
export TELEGRAM_EQUITY_FLOOR_USD=0

# LIVE REPORT cadence: every 30 minutes (the ONLY Telegram message this stack
# sends). Pinned explicitly so it never drifts from the code default.
export LIVE_ANALYST_CYCLE_SECONDS=${LIVE_ANALYST_CYCLE_SECONDS:-1800}

# Telegram: SILENCE the live bot entirely. The ONLY message we want is the
# 30-minute LIVE REPORT from the live_analyst sidecar (TELEGRAM_CHAT_ID_LIVE).
# No BUY/SELL, no heartbeat, no thresholds, no daily summary — nothing.
# These flags default to ON when unset, so each one must be set to 0 explicitly.
export TELEGRAM_ALERT_TRADES=0
export TELEGRAM_ALERT_TRADES_BUY=0
export TELEGRAM_ALERT_TRADES_SELL=0
export TELEGRAM_ALERT_ERRORS=0
export TELEGRAM_ALERT_THRESHOLDS=0
export TELEGRAM_ALERT_HEARTBEAT=0
export TELEGRAM_ALERT_PORTFOLIO_UPDATES=0
export TELEGRAM_ALERT_DAILY_SUMMARY=0

# Profile label exported BEFORE the live_analyst spawns, so the
# sidecar inherits it (else it logs "(unknown)" in reports).
export POLYMARKET_PROFILE_LABEL=grinder_c

# Name shown in the LIVE REPORT header/footer.
export POLYMARKET_BOT_NAME="Grinder Bot 3"

# ─── Live analyst sidecar (read-only, posts to TELEGRAM_CHAT_ID_LIVE) ──
# Every 8 hours: reads paper_state + realized_trade_cache and posts the
# LIVE REPORT — the ONLY Telegram message this stack sends (equity since
# start, top trades today, all open positions). No AI, no dry-race compare.
# NEVER touches the live bot. Ctrl+C kills the whole process group.
cleanup() {
    kill 0 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

# Kill only THIS bot's stale live_analyst (matched by the profile-label tag
# passed on the command line). Scoped so the 3 grinder analysts can coexist
# instead of pkill'ing each other on startup.
pkill -f "live_analyst.py ${POLYMARKET_PROFILE_LABEL}\$" 2>/dev/null || true
sleep 1

# Bot B posts its OWN live report to its TELEGRAM_CHAT_ID_LIVE (.env).
# live_analyst fires cycle_once() immediately on startup, then every 8 hours —
# so you always get a report at launch, not after an 8-hour wait.
uv run python scripts/live_analyst.py "${POLYMARKET_PROFILE_LABEL}" 2>&1 | sed -u 's/^/[live-analyst] /' | tee -a "$RUN_LOG" &

# ─── Live-only leaderboard sidecar REMOVED (2026-05-30) ────────────────
# The 5-min "🏁 Leaderboard · LIVE only" Telegram summary was noisy and
# duplicated the daily quant report. Disabled per request. Re-add the
# `pmbot leaderboard --live-only --interval 5 --telegram` line to restore.

# ─── Dry grinder twin (paper, mirrors the live config for safe compare) ─
# Same grinder.toml ($43, all-in) but simulated — never spends real money,
# writes to data/dry_runs/grinder/. Telegram BUY/SELL silenced so only the
# live bot speaks. Ticks slower (10min) to keep API load down.
POLYMARKET_QUIET=1 \
    POLYMARKET_SUPPRESS_BUY_LOGS=1 \
    POLYMARKET_AUTO_INTERVAL_SECONDS=600 \
    TELEGRAM_ALERT_TRADES=0 TELEGRAM_ALERT_TRADES_BUY=0 TELEGRAM_ALERT_TRADES_SELL=0 \
    TELEGRAM_ALERT_ERRORS=0 TELEGRAM_ALERT_THRESHOLDS=0 TELEGRAM_ALERT_HEARTBEAT=0 \
    TELEGRAM_ALERT_PORTFOLIO_UPDATES=0 TELEGRAM_ALERT_DAILY_SUMMARY=0 \
    uv run pmbot auto-loop --dry-run --profile grinder_c --run grinder_c \
    2>&1 | sed -u 's/^/[dry-grinder] /' | tee -a "$RUN_LOG" &

# ─── Autonomous report sidecar (deterministic — NO codex/claude/ollama) ─
# Reports on the dry grinder (and any other dry runs) every 15 min to
# TELEGRAM_CHAT_ID_DRY_RUN. No AI: narrative built straight from metrics.
# Dry-run Telegram silenced — live-only mode. Remove the override to re-enable.
TELEGRAM_CHAT_ID_DRY_RUN="" \
    uv run python scripts/dry_analyst.py 2>&1 | sed -u 's/^/[analyst] /' | tee -a "$RUN_LOG" &

# ─── Daily self-learning sidecar (offline LLM exception) ───────────────
# Once/day after 23:00 local: writes an end-of-day ANALYSIS of the results +
# runs the FENCED Claude self-tuner (scripts/auto_improve.py) — EXIT/SIZING
# only, entry filters FROZEN, a stop-loss can NEVER be introduced, full test
# suite + CI gated, only grinder.toml writable, git branch always restored.
# Fully wrapped (set +e + try/catch) so it can NEVER crash the live loop.
# Toggle with DAILY_SELF_IMPROVE=0. Part of the process group → Ctrl+C kills it.
DAILY_SELF_IMPROVE="${DAILY_SELF_IMPROVE:-1}" \
    bash scripts/daily_self_improve.sh 2>&1 | sed -u 's/^/[self-improve] /' | tee -a "$RUN_LOG" &

uv run pmbot auto-loop --live --profile grinder_c --yes \
    2>&1 | sed -u 's/^/[LIVE] /' | tee -a "$LIVE_LOG" "$RUN_LOG"
