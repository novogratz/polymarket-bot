#!/usr/bin/env bash
# Lance le bot en LIVE avec le profil pmlepgm_counter_panic_fade.
# #2 du leaderboard dry au moment du switch (+\$15.73, 66% wr, 29 trades).
# Trigger : panic move ≥15¢ confirmé par vol ≥\$3k. Parie AVEC le mouvement
# (côté gagnant), thèse : une partie des panic moves continue jusqu'à
# la résolution.
# Sizing (\$46 bankroll) : cap \$15/position, 15% stake/trade, 5 orders/tick.
# Exits : TP +25% / SL -25% / resolved ≥0.97. Min-hold 3min.
# Toute la config vit dans configs/profiles/pmlepgm_counter_panic_fade.toml.
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

exec uv run pmbot auto-loop --live --profile pmlepgm_counter_panic_fade --yes
