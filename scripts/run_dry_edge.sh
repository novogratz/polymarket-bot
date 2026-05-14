#!/usr/bin/env bash
# Miroir de run_live_70.sh en --dry-run : mêmes profil, mêmes filtres, mêmes
# exits. Permet de valider que ce que dry voit, live le fera aussi.
#
# Bankroll : starting_cash du profil edge.toml (=$9 actuellement).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Pas de sync des positions live en dry — on isole le ledger papier.
export POLYMARKET_SYNC_LIVE_POSITIONS=0

# Telegram silencieux en dry — pas de firehose.
export TELEGRAM_ALERT_TRADES=0
export TELEGRAM_ALERT_TRADES_BUY=0
export TELEGRAM_ALERT_ERRORS=0
export TELEGRAM_ALERT_THRESHOLDS=0
export TELEGRAM_ALERT_HEARTBEAT=0
export TELEGRAM_ALERT_PORTFOLIO_UPDATES=0
export TELEGRAM_ALERT_DAILY_SUMMARY=0

exec uv run pmbot auto-loop --dry-run --profile edge --run edge_dry
