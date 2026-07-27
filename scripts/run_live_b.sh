#!/usr/bin/env bash
# Lance le bot 2 en LIVE avec le profil TWEET-COUNT (remplacement de la
# stratégie weather, user 2026-07-27). Toute la config vit dans
# configs/profiles/tweet_b.toml.
#
# Ce script passe --yes : la confirmation interactive est skipée, donc aucun
# besoin de TTY. Pour une exécution sans --yes (auto-loop --live tout court),
# l'opérateur DOIT être attaché à un TTY ; sans cela, prompt_live_confirmation
# refuse et abort proprement (cf. live_confirm.py:48).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# DUPLICATE GUARD (2026-07-27): a second live loop on the same wallet double-
# trades every candidate (it happened when a manual launch raced the launchd
# watchdog's copy). Refuse to start when one is already running — stop the
# other stack first (launchctl unload ~/Library/LaunchAgents/
# com.polymarket.grinder-b.plist for the watchdog copy).
if pgrep -f "pmbot auto-loop --live" >/dev/null 2>&1; then
    echo "[run_live] ABORT: another live pmbot auto-loop is already running:" >&2
    pgrep -fl "pmbot auto-loop --live" >&2
    exit 1
fi

# Daily logs: tee everything to a dated file under data/logs/ for debugging.
LOG_DIR="$REPO_ROOT/data/logs"
mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/run_live_$(date +%Y-%m-%d).log"
LIVE_LOG="$LOG_DIR/live_$(date +%Y-%m-%d).log"
echo "[run_live] logging to $RUN_LOG (live also -> $LIVE_LOG)"

# Sync live positions (toggle hors schéma).
export POLYMARKET_SYNC_LIVE_POSITIONS=1

# Bot B bankroll fallback = $15 (user 2026-07-27). The bot reads the real
# USDC balance from CLOB each tick; these only kick in if that read fails.
# MUST match tweet_b.toml starting_cash and data/starting_cash.txt — a
# mismatched fallback skews "depuis le début" %.
export POLYMARKET_PAPER_BALANCE_USD=${POLYMARKET_PAPER_BALANCE_USD:-15.0}
export POLYMARKET_ASSUME_LIVE_BALANCE_USD=${POLYMARKET_ASSUME_LIVE_BALANCE_USD:-15.0}

# 10s tick — 3× faster than 30s, catches more fleeting band entries.
export POLYMARKET_AUTO_INTERVAL_SECONDS=${POLYMARKET_AUTO_INTERVAL_SECONDS:-10}

# Maker entries (2026-07-27): let a resting GTC work the spread for 4 minutes
# (8 ticks at 30s) before the stale-pending sweep cancels it on the CLOB.
# The default 45s barely outlives one tick — too short for a maker order.
export POLYMARKET_SMART_PENDING_ORDER_TTL_SECONDS=${POLYMARKET_SMART_PENDING_ORDER_TTL_SECONDS:-240}

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
export POLYMARKET_PROFILE_LABEL=tweet_b

# Name shown in the LIVE REPORT header/footer.
export POLYMARKET_BOT_NAME="Grinder Bot 2 — Tweets"

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
    uv run pmbot auto-loop --dry-run --profile tweet_b --run tweet_b \
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

# ─── Tweet-regime sidecar (OFFLINE, LOCAL OLLAMA ONLY — user 2026-07-27) ─
# Every 15 min: asks a LOCAL Ollama model (default qwen2.5:7b) to classify
# the tracked account's posting regime from its recent posts and writes
# data/tweet_regime.json. The live loop only READS that file (stale >2h
# ignored, multiplier clamped [0.5, 2.0]) — NO LLM call in the trade path.
# Fully wrapped: Ollama down => nothing written, model falls back to 1.0.
# Toggle with TWEET_REGIME_SIDECAR=0. Part of the process group.
if [ "${TWEET_REGIME_SIDECAR:-1}" = "1" ]; then
    uv run python scripts/tweet_regime_sidecar.py 2>&1 | sed -u 's/^/[tweet-regime] /' | tee -a "$RUN_LOG" &
fi

# ─── Cross-window consistency logger (read-only, deterministic, no AI) ──
# Every 30 min: snapshots model-vs-market probability for every bracket of
# every active counting window -> data/tweet_consistency.jsonl. Pure data
# collection for the future relative-value lane; never trades.
if [ "${TWEET_CONSISTENCY_LOG:-1}" = "1" ]; then
    uv run python scripts/tweet_consistency_log.py 2>&1 | sed -u 's/^/[consistency] /' | tee -a "$RUN_LOG" &
fi

uv run pmbot auto-loop --live --profile tweet_b --yes \
    2>&1 | sed -u 's/^/[LIVE] /' | tee -a "$LIVE_LOG" "$RUN_LOG"
