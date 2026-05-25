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

# Sync live positions (toggle hors schéma).
export POLYMARKET_SYNC_LIVE_POSITIONS=1

# Live bankroll baseline = $6.
# grinder.toml has starting_cash=6 / assumed_live_balance_usd=6.
# These env exports keep live and dry-run sizing anchored to the same
# $6 bankroll unless the operator explicitly overrides them.
export POLYMARKET_PAPER_BALANCE_USD=${POLYMARKET_PAPER_BALANCE_USD:-6.0}
export POLYMARKET_ASSUME_LIVE_BALANCE_USD=${POLYMARKET_ASSUME_LIVE_BALANCE_USD:-6.0}

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
# Every 30 min: reads paper_state + trade_journal, compares vs dry race
# leaders, calls Codex CLI with Ollama fallback. NEVER touches the live bot;
# pure observability. Kill via Ctrl+C (same process group).
cleanup() {
    kill 0 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT
python3 scripts/live_analyst.py 2>&1 | sed -u 's/^/[live-analyst] /' &

uv run pmbot auto-loop --live --profile grinder --yes
