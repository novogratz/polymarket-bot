#!/usr/bin/env bash
# Lance la stratégie "edge" en LIVE (ordres réels).
#
# Quatre lanes (arb, crypto, near-cert, scalp-off). Cap 15%/trade,
# halt à -20% daily DD, cash floor 10%. Seuls les marchés ≤ 4h.
#
# Notifications:
#   - Heartbeat stdout toutes les 15 min (multi-ligne avec PnL since start)
#   - Heartbeat Telegram toutes les 15 min (si TELEGRAM_BOT_TOKEN+CHAT_ID setés)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export POLYMARKET_SYNC_LIVE_POSITIONS=1
export POLYMARKET_QUIET=${POLYMARKET_QUIET:-1}

# Telegram alerts: heartbeat every 15 min on top of stdout.
export TELEGRAM_ALERT_HEARTBEAT=${TELEGRAM_ALERT_HEARTBEAT:-1}
export TELEGRAM_HEARTBEAT_MINUTES=${TELEGRAM_HEARTBEAT_MINUTES:-15}

exec uv run pmbot auto-loop --live --profile edge --yes
