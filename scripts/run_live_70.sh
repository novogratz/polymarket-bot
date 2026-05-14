#!/usr/bin/env bash
# Lance le bot en LIVE avec le profil wallet_cluster_correlation.
# Filtre : ≤4h, plusieurs wallets corrélés sur la même position.
# Sizing : $5/trade, 3 trades max/tick. TP +95% / SL off / cash floor 10%.
# Toute la config vit dans configs/profiles/wallet_cluster_correlation.toml.
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

exec uv run pmbot auto-loop --live --profile wallet_cluster_correlation --yes
