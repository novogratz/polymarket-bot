#!/usr/bin/env bash
# Lance le bot en LIVE avec le profil claude_endgame_sweep.
# Entrée : favoris forts (bid 0.92-0.985) avec ≤2h restantes, spread
# ≤2¢, volume ≥$500. Bande de calibration documentée (88-93% win rate
# sur marchés liquides, Datawallet/TradeTheOutcome).
# Sizing ($100 bankroll) : cap $15/position, $5 stake, 3 orders/tick.
# Exits : TP +25% / SL -10% (serré, peu de marge à 0.92+) / resolved ≥0.97.
# Toute la config vit dans configs/profiles/claude_endgame_sweep.toml.
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

exec uv run pmbot auto-loop --live --profile claude_endgame_sweep --yes
