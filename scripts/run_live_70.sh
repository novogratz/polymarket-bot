#!/usr/bin/env bash
# Lance le bot en LIVE avec le profil smart_wallet_consensus.
# Stratégie race : volume 24h élevé comme proxy de l'activité multi-wallets,
# entre WITH les marchés où le smart money est déjà présent.
# Sizing (\$20 bankroll) : cap \$15/position, 15% par trade, 5 orders/tick.
# Exits : TP +25% / SL -25% / resolved ≥0.97 / 4h hard rule.
# Toute la config vit dans configs/profiles/smart_wallet_consensus.toml.
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

exec uv run pmbot auto-loop --live --profile smart_wallet_consensus --yes
