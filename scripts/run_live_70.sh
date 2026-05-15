#!/usr/bin/env bash
# Lance le bot en LIVE avec le profil championdumonde_breakout.
# Entrée : marchés ≤4h avec breakout intraday ≥5¢ ET volume total ≥$5k
# (momentum continuation, filtre les fake spikes whale-only).
# Sizing ($100 bankroll) : cap $15/position, $5 stake, 3 orders/tick.
# Exits : TP +25% / SL -25% (min-age 5min) / resolved ≥0.97.
# Toute la config vit dans configs/profiles/championdumonde_breakout.toml.
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

exec uv run pmbot auto-loop --live --profile championdumonde_breakout --yes
