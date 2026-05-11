#!/usr/bin/env bash
set -euo pipefail
# One-off 30-minute Telegram portfolio report.
# Resets the rate-limit timestamp and sends the Director review immediately.

cd "$(dirname "$0")/.."

STATE_FILE="data/notifications_state.json"
if [ -f "$STATE_FILE" ]; then
  python3 -c "
import json
d = json.load(open('$STATE_FILE'))
d['last_portfolio_update_ts'] = None
json.dump(d, open('$STATE_FILE', 'w'), indent=2)
print('[ok] reset rate-limit timestamp')
" 2>&1 || true
fi

.venv/bin/python3 -B -c "
import sys
sys.path.insert(0, '.')
from polymarket_bot.config import Settings
from polymarket_bot.main import _portfolio_update_snapshot
from polymarket_bot.notifications import notify_portfolio_update

settings = Settings()
snapshot = _portfolio_update_snapshot(settings)
eq = snapshot.get('equity_usd')
cash = snapshot.get('cash_usd')
n = snapshot.get('open_position_count')
print(f'[ok] equity=\${eq} cash=\${cash} open={n}')
notify_portfolio_update(snapshot)
print('[done] check Telegram for Director review')
" 2>&1
