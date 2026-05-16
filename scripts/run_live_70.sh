#!/usr/bin/env bash
# Lance le bot en LIVE avec le profil aggressive_buyer_detection.
# Plus gros échantillon validé en dry : 41 trades, 56% win rate,
# +\$12.31 réalisé. Détecte les patterns d'achat agressif (volume +
# momentum positif) sur marchés ≤4h.
# Sizing (\$20 bankroll) : cap \$15/position, 15% stake/trade (=\$3), 5 orders/tick.
# Exits : TP +25% / SL -25% / resolved ≥0.97. Min-hold 3min.
# Toute la config vit dans configs/profiles/aggressive_buyer_detection.toml.
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

exec uv run pmbot auto-loop --live --profile aggressive_buyer_detection --yes
