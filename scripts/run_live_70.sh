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

# Live bankroll baseline = $43 (2026-05-26 deposit).
# grinder.toml has starting_cash=43 / assumed_live_balance_usd=43.
# These exports are the RPC-failure fallback cap — the bot reads the real
# USDC balance from CLOB each tick, but if that read fails it must not
# clamp down to a stale low number. Override only if you change bankroll.
export POLYMARKET_PAPER_BALANCE_USD=${POLYMARKET_PAPER_BALANCE_USD:-43.0}
export POLYMARKET_ASSUME_LIVE_BALANCE_USD=${POLYMARKET_ASSUME_LIVE_BALANCE_USD:-43.0}

# Live tick interval — grinder default is 30s (the profile sets it via
# [telemetry].auto_interval_seconds). Keep the env override as 30 here too
# so the parent shell doesn't smuggle in a stale 10s value.
export POLYMARKET_AUTO_INTERVAL_SECONDS=${POLYMARKET_AUTO_INTERVAL_SECONDS:-30}

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

uv run pmbot auto-loop --live --profile grinder --yes \
    2>&1 | sed -u 's/^/[LIVE] /' | tee -a "$LIVE_LOG" "$RUN_LOG"
