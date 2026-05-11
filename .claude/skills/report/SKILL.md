---
name: report
description: Send a one-off 30-minute Telegram portfolio report (Director review).
---

# 30-Minute Report Skill

Trigger the Telegram Director review report immediately, bypassing the 30-minute rate limit.

## One-off invocation

```bash
bash scripts/report.sh
```

This resets the notification rate-limit timestamp, builds a portfolio snapshot from the local ledger, and sends the report via Telegram.

## Manual Python equivalent

```bash
.venv/bin/python3 -B -c "
import sys; sys.path.insert(0, '.')
from polymarket_bot.main import _portfolio_update_snapshot
from polymarket_bot.notifications import notify_portfolio_update
from polymarket_bot.config import Settings
notify_portfolio_update(_portfolio_update_snapshot(Settings()))
"
```

But first reset `data/notifications_state.json` → `last_portfolio_update_ts` to `null`.

## What the report contains

- Equity, cash, invested, unrealized PnL
- 30m / today / all-time PnL deltas
- Today's trade count with wins/losses
- All-time win rate and PnL
- Open positions (big trades >$50 first, then smaller)
- Recent closed trades

## Notes

- Works with both live and paper (`POLYMARKET_DRY_RUN=1`) ledgers.
- The report reads from local state (`data/paper_state.json`) — run `auto-loop` first if you want live-synced position data.
- Requires `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID_LIVE` (or `TELEGRAM_CHAT_ID_DRY_RUN`), and `TELEGRAM_ALERT_PORTFOLIO_UPDATES=1`.
