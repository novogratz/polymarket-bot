#!/usr/bin/env bash
# Lance le bot en LIVE avec le profil edge.
# 4 lanes : arbitrage (YES+NO<$0.98), crypto BS pricing, near-cert (bid≥0.70), scalp(off).
# Sizing actuel ($9 bankroll) : cap 3$ par position, 1$ starter, 2 orders/tick.
# Exits : TP +25% / SL adaptatif (-25%/-15%/-10% selon temps) / DD halt -15%.
# Min-hold universel : 3 minutes (toutes les sorties).
# Toute la config vit dans configs/profiles/edge.toml.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Sync live positions (toggle hors schéma).
export POLYMARKET_SYNC_LIVE_POSITIONS=1

# Telegram: tout pousser en live (override .env qui a TELEGRAM_ALERT_TRADES=0
# pour rester silencieux en dry-run).
export TELEGRAM_ALERT_TRADES=1
export TELEGRAM_ALERT_TRADES_BUY=1
export TELEGRAM_ALERT_ERRORS=1
export TELEGRAM_ALERT_THRESHOLDS=1
export TELEGRAM_ALERT_HEARTBEAT=1
export TELEGRAM_ALERT_PORTFOLIO_UPDATES=1
export TELEGRAM_ALERT_DAILY_SUMMARY=1

exec uv run pmbot auto-loop --live --profile edge --yes
