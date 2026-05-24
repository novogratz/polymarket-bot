#!/usr/bin/env bash
# Lance le bot en LIVE avec le profil smart_copy_live courant.
# Toute la config vit dans configs/profiles/smart_copy_live.toml.
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

# Live bankroll baseline = $20.
# smart_copy_live.toml has starting_cash=20 / assumed_live_balance_usd=20.
# These env exports keep live and dry-run sizing anchored to the same
# $20 bankroll unless the operator explicitly overrides them.
export POLYMARKET_PAPER_BALANCE_USD=${POLYMARKET_PAPER_BALANCE_USD:-20.0}
export POLYMARKET_ASSUME_LIVE_BALANCE_USD=${POLYMARKET_ASSUME_LIVE_BALANCE_USD:-20.0}

# Live tick interval — fast (10s).
export POLYMARKET_AUTO_INTERVAL_SECONDS=${POLYMARKET_AUTO_INTERVAL_SECONDS:-10}

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
export POLYMARKET_PROFILE_LABEL=smart_copy_live

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

uv run pmbot auto-loop --live --profile smart_copy_live --yes
