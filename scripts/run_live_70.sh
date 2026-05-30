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

# Live bankroll baseline = $123 (2026-05-29 deposit).
# These exports are the RPC-failure fallback cap — the bot reads the real
# USDC balance from CLOB each tick; these kick in only if that read fails.
export POLYMARKET_PAPER_BALANCE_USD=${POLYMARKET_PAPER_BALANCE_USD:-75.02}
export POLYMARKET_ASSUME_LIVE_BALANCE_USD=${POLYMARKET_ASSUME_LIVE_BALANCE_USD:-75.02}

# 10s tick — 3× faster than 30s, catches more fleeting band entries.
export POLYMARKET_AUTO_INTERVAL_SECONDS=${POLYMARKET_AUTO_INTERVAL_SECONDS:-10}

# Drawdown halt at 40% — generous enough that one SL loss (-35%) doesn't freeze the bot.
export POLYMARKET_RACE_DAILY_DRAWDOWN_PCT=${POLYMARKET_RACE_DAILY_DRAWDOWN_PCT:-0.40}

# Telegram: tout pousser en live (override .env qui a TELEGRAM_ALERT_TRADES=0
# pour rester silencieux en dry-run).
export TELEGRAM_ALERT_TRADES=1
export TELEGRAM_ALERT_TRADES_BUY=1
export TELEGRAM_ALERT_ERRORS=1
export TELEGRAM_ALERT_THRESHOLDS=1
export TELEGRAM_ALERT_HEARTBEAT=1
export TELEGRAM_ALERT_PORTFOLIO_UPDATES=1
export TELEGRAM_ALERT_DAILY_SUMMARY=1

# Profile label exported BEFORE the live_analyst spawns, so the
# sidecar inherits it (else it logs "(unknown)" in reports).
export POLYMARKET_PROFILE_LABEL=grinder

# ─── Live analyst sidecar (read-only, posts to TELEGRAM_CHAT_ID_LIVE) ──
# Every 30 min: reads paper_state + realized_trade_cache and posts a
# LIVE-ONLY deterministic report (equity/ROI, open positions, top closed).
# No AI, no dry-race comparison. NEVER touches the live bot. Ctrl+C kills
# the whole process group.
cleanup() {
    kill 0 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT
python3 scripts/live_analyst.py 2>&1 | sed -u 's/^/[live-analyst] /' | tee -a "$RUN_LOG" &

# ─── Live-only leaderboard sidecar (Telegram every 5 min) ──────────────
# --live-only: renders the LIVE bot as the whole board (no dry runs), with
# winner detail (open / best closed / worst closed). W/L comes from the
# durable data/realized_trade_cache.jsonl, so it survives journal rotation.
uv run pmbot leaderboard --live-only --interval 5 --telegram \
    2>&1 | sed -u 's/^/[board] /' | tee -a "$RUN_LOG" &

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
    uv run pmbot auto-loop --dry-run --profile grinder --run grinder \
    2>&1 | sed -u 's/^/[dry-grinder] /' | tee -a "$RUN_LOG" &

# ─── Autonomous report sidecar (deterministic — NO codex/claude/ollama) ─
# Reports on the dry grinder (and any other dry runs) every 15 min to
# TELEGRAM_CHAT_ID_DRY_RUN. No AI: narrative built straight from metrics.
# Dry-run Telegram silenced — live-only mode. Remove the override to re-enable.
TELEGRAM_CHAT_ID_DRY_RUN="" \
    uv run python scripts/dry_analyst.py 2>&1 | sed -u 's/^/[analyst] /' | tee -a "$RUN_LOG" &

uv run pmbot auto-loop --live --profile grinder --yes \
    2>&1 | sed -u 's/^/[LIVE] /' | tee -a "$LIVE_LOG" "$RUN_LOG"
