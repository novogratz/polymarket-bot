#!/usr/bin/env bash
# Lance le bot en LIVE avec le profil pm_le_pgm_weak_holder_flush_inverse.
# Stratégie promue après analyse dry: #1 du leaderboard (+19.3% sur 34
# trades, 65% win rate). Achète le côté opposé d'un dump de panique:
# mom ≥ +10% et vol ≥ $1k.
# Sizing ($46 bankroll) : cap $15/position, 15% stake/trade, 5 orders/tick.
# Exits : TP +25% / SL -25% / resolved ≥0.97. Min-hold 3min.
# Toute la config vit dans configs/profiles/pm_le_pgm_weak_holder_flush_inverse.toml.
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

exec uv run pmbot auto-loop --live --profile pm_le_pgm_weak_holder_flush_inverse --yes
