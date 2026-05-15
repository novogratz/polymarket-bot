#!/usr/bin/env bash
# Lance le bot en LIVE avec le profil claude_balanced_mid.
# Trigger : marchés coin-flip (ask 0.35-0.65) avec momentum positif et
# volume confirmant. Thèse : 50/50 markets qui basculent vers une
# direction continuent souvent dans cette direction.
# Sizing (\$46 bankroll) : cap \$15/position, 15% stake/trade, 5 orders/tick.
# Exits : TP +25% / SL -25% / resolved ≥0.97. Min-hold 3min.
# Toute la config vit dans configs/profiles/claude_balanced_mid.toml.
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

exec uv run pmbot auto-loop --live --profile claude_balanced_mid --yes
