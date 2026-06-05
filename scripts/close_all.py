#!/usr/bin/env python3
"""Close all open positions at market price, then reset the P&L baseline.

Run while the bot is STOPPED:
  uv run python scripts/close_all.py

What it does:
  1. Reads open positions from data/paper_state.json
  2. Places a real CLOB SELL order for each at current bid price
  3. Skips positions already at/above 0.995 (auto-redeems on-chain, no sell needed)
  4. After all sells, runs a fresh-start reset:
       - Wipes trade journal / realized cache (closed history gone)
       - Writes new data/starting_cash.txt = current CLOB cash balance
       - Clears paper_state positions (re-synced on next start)
"""
from __future__ import annotations

import json
import sys
import time
import types
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
sys.path.insert(0, str(REPO_ROOT))


def _load_env() -> None:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    import os
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_env()


def _get_current_bid(token_id: str) -> float | None:
    """Fetch current best bid for a token from the CLOB order book."""
    try:
        url = f"https://clob.polymarket.com/book?token_id={token_id}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
        bids = data.get("bids") or []
        if bids:
            return float(bids[0].get("price", 0))
    except Exception as exc:
        print(f"  ! bid fetch failed for {token_id[:16]}: {exc}")
    return None


def _sell_position(client, position: dict) -> float | None:
    """Place a SELL order for a position. Returns proceeds or None on failure."""
    token_id = str(position.get("token_id") or "")
    if not token_id:
        print(f"  ! no token_id for: {position.get('question', '?')[:50]}")
        return None

    # Get on-chain share balance
    shares = None
    try:
        shares = client.live_share_balance(token_id)
    except Exception:
        pass
    if not shares:
        shares = float(position.get("shares") or position.get("size_usd") or 0)
    if shares is None or shares <= 0.001:
        print(f"  ! zero shares for: {position.get('question', '?')[:50]}")
        return None

    # Get current bid price
    current_price = _get_current_bid(token_id)
    if current_price is None:
        current_price = float(position.get("current_price") or position.get("price") or 0.5)

    # Markets at 0.995+ auto-redeem — no sell possible or needed
    if current_price >= 0.995:
        proceeds = round(shares * 1.0, 2)
        print(f"  ✓ {position.get('question', '?')[:50]}")
        print(f"    auto-redeems at 1.0 — proceeds ~${proceeds:.2f}")
        return proceeds

    sell_price = round(min(current_price, 0.99), 3)
    tick_size = float(position.get("tick_size") or 0.01)
    neg_risk = bool(position.get("neg_risk") or False)

    stub = types.SimpleNamespace(token_id=token_id, tick_size=tick_size, neg_risk=neg_risk)
    try:
        _, response = client.place_live_order(
            candidate=stub, price=sell_price, size=round(shares, 6), side="SELL"
        )
        resp = response if isinstance(response, dict) else (
            response.__dict__ if hasattr(response, "__dict__") else {}
        )
        success = (
            resp.get("success") is True
            or resp.get("status") in ("matched", "delayed", "live")
            or bool(resp.get("orderID") or resp.get("orderId"))
        )
        if success:
            taking = resp.get("takingAmount")
            proceeds = float(taking) if taking is not None else round(shares * sell_price, 2)
            print(f"  ✓ SOLD {shares:.4f} @ {sell_price} → ${proceeds:.2f}  "
                  f"{position.get('question', '?')[:50]}")
            return proceeds
        else:
            print(f"  ! order not matched: {resp}")
    except Exception as exc:
        print(f"  ! sell failed: {type(exc).__name__}: {exc}")
    return None


def main() -> int:
    from polymarket_bot.config import Settings
    from polymarket_bot.trading import build_client

    settings = Settings()
    if not settings.funder_address:
        print("ERROR: POLYMARKET_FUNDER_ADDRESS not set in .env")
        return 1

    # Load paper_state
    state_path = DATA_DIR / "paper_state.json"
    if not state_path.exists():
        print("No paper_state.json found — nothing to close.")
        return 0

    state = json.loads(state_path.read_text())
    positions = state.get("positions") or []
    if not positions:
        print("No open positions in paper_state.json.")
        return 0

    print(f"\nClosing {len(positions)} open position(s)...\n")
    client = build_client(settings)

    total_proceeds = 0.0
    for pos in positions:
        proceeds = _sell_position(client, pos)
        if proceeds is not None:
            total_proceeds += proceeds
        time.sleep(0.5)  # brief pause between orders

    print(f"\nTotal proceeds from sells: ${total_proceeds:.2f}")

    # Fresh-start reset
    print("\nResetting P&L baseline...")
    ts = int(time.time())
    bk = DATA_DIR / f"backups_closeall_{ts}"
    bk.mkdir(exist_ok=True)

    # Read current CLOB cash balance
    try:
        cash = float(client.live_available_balance())
    except Exception:
        cash = float(state.get("cash") or 0) + total_proceeds
    print(f"Current CLOB cash: ${cash:.2f}")

    # Backup everything
    for name in ("paper_state.json", "trade_journal.jsonl", "realized_trade_cache.jsonl",
                 "live_baseline.json", "live_tracking_start", "starting_cash.txt"):
        src = DATA_DIR / name
        if src.is_file():
            (bk / name).write_bytes(src.read_bytes())

    # Rotate closed-trade history
    for name in ("trade_journal.jsonl", "realized_trade_cache.jsonl", "live_baseline.json"):
        p = DATA_DIR / name
        if p.is_file():
            p.rename(DATA_DIR / f"{p.stem}_{ts}{p.suffix}.bak")

    # Flat ledger — positions cleared, re-synced on next start
    state_path.write_text(
        json.dumps({"cash": round(cash, 2), "pending_orders": [], "positions": []}, indent=2) + "\n"
    )
    (DATA_DIR / "live_tracking_start").write_text(str(ts))
    (DATA_DIR / "starting_cash.txt").write_text(f"{cash:.2f}")

    print(f"\n✓ Done. Baseline reset to ${cash:.2f}. Backup: {bk.name}")
    print("Start the bot — open positions will re-sync and P&L tracks from $0.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
