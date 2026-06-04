#!/usr/bin/env python3
"""Fresh-start a bot: wipe CLOSED-trade history, KEEP open trades, reset baseline.

Run this on a bot's own machine (it reads that machine's .env / account) while
the bot is STOPPED. It:
  1. Reads the real CLOB cash + open positions (Data API) → current equity.
  2. Backs up, then rotates the trade journal + realized cache + live baseline
     to `.bak` (so W/L / realized P&L reset to 0 — old closed trades gone).
  3. Writes a flat data/paper_state.json (cash only). Open positions are
     re-imported automatically on the next start by POLYMARKET_SYNC_LIVE_POSITIONS
     → open trades are KEPT, only the closed history is wiped.
  4. Stamps data/live_tracking_start = now (report ignores pre-reset activity).
  5. Writes data/starting_cash.txt = equity  → Total P&L starts at $0. This is a
     PER-MACHINE baseline (gitignored), so bot 2 and bot 3 each keep their own
     without touching the shared profile.

Usage (on the bot's machine, bot stopped):
  uv run python scripts/fresh_start.py            # baseline = computed equity
  uv run python scripts/fresh_start.py --equity 12.91   # force the baseline
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
sys.path.insert(0, str(REPO_ROOT))


def _live_cash() -> float:
    from polymarket_bot.config import Settings
    from polymarket_bot.trading import build_client
    try:
        return float(build_client(Settings()).live_available_balance())
    except Exception as exc:
        print(f"  ! live cash read failed ({exc}); using 0.0")
        return 0.0


def _open_positions_value() -> float:
    from polymarket_bot.config import Settings
    s = Settings()
    if not s.funder_address:
        return 0.0
    url = "https://data-api.polymarket.com/positions?" + urllib.parse.urlencode(
        {"user": s.funder_address, "sizeThreshold": "0.1", "limit": "100"}
    )
    req = urllib.request.Request(url, headers={"User-Agent": "x", "Accept": "application/json"})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
    except Exception as exc:
        print(f"  ! positions read failed ({exc}); using 0.0")
        return 0.0
    return sum(float(p.get("currentValue") or 0) for p in data)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--equity", type=float, default=None, help="force baseline (else computed cash+positions)")
    args = ap.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    ts = int(time.time())
    bk = DATA_DIR / f"backups_scratch_{ts}"
    bk.mkdir(exist_ok=True)

    cash = _live_cash()
    pos_val = _open_positions_value()
    equity = args.equity if args.equity is not None else round(cash + pos_val, 2)
    print(f"cash ${cash:.2f} + open positions ${pos_val:.2f} = equity ${cash + pos_val:.2f}")
    print(f"baseline (Total P&L starts at $0) = ${equity:.2f}")

    # Backup + rotate closed-trade history / baseline
    for name in ("paper_state.json", "trade_journal.jsonl", "realized_trade_cache.jsonl",
                 "live_baseline.json", "live_tracking_start", "starting_cash.txt"):
        src = DATA_DIR / name
        if src.is_file():
            (bk / name).write_bytes(src.read_bytes())
    for name in ("trade_journal.jsonl", "realized_trade_cache.jsonl", "live_baseline.json"):
        p = DATA_DIR / name
        if p.is_file():
            p.rename(DATA_DIR / f"{p.stem}_{ts}{p.suffix}.bak")

    # Flat ledger (open positions re-imported on next start via live sync)
    (DATA_DIR / "paper_state.json").write_text(
        json.dumps({"cash": round(cash, 2), "pending_orders": [], "positions": []}, indent=2) + "\n"
    )
    (DATA_DIR / "live_tracking_start").write_text(str(ts))
    (DATA_DIR / "starting_cash.txt").write_text(f"{equity:.2f}")

    print(f"✓ fresh start done. cash ${cash:.2f}, baseline ${equity:.2f}, history wiped, "
          f"open trades kept (re-synced on start). backup: {bk.name}")
    print("Now start the bot — it'll re-import your open positions and track from $0 P&L.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
