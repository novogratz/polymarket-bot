#!/usr/bin/env bash
# Lance le bot en LIVE avec le profil kzerlepgm_baseline.
# Restauration de la baseline main qui marchait, + règle 4h.
# Mode smart_money (default) : leaderboard WEEK top 50, multi-wallet
# consensus ≥2 sur même token < 240min, min_copied_usdc=\$50,
# max_chase_premium=12%. Exit stack complet (TP ladder + trailing +
# peak-protect + SL -40% + cohort-sell). Auto-tune ON à 30 trades.
# Sizing 10%/trade, cap \$25/position. assumed_live_balance_usd=\$42.43.
# Toute la config vit dans configs/profiles/kzerlepgm_baseline.toml.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Sync live positions (toggle hors schéma).
export POLYMARKET_SYNC_LIVE_POSITIONS=1

# Live tick interval — fast (10s) even though kzerlepgm_baseline TOML
# now uses 60s for the dry-race rate-limit fix. Env var override wins.
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

exec uv run pmbot auto-loop --live --profile kzerlepgm_baseline --yes
