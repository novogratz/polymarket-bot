#!/usr/bin/env python3
"""Force-close positions whose current_price >= 0.97 in the local ledger.

Runs once, writes the closed positions to the journal, credits cash,
and sends a Telegram win notification for each. Safe to run while the
bot is live — reads/writes paper_state.json atomically.

Usage:
  uv run python scripts/force_close_resolved.py
  uv run python scripts/force_close_resolved.py --dry-run   # print only
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.live_analyst as _analyst

STATE_PATH = REPO_ROOT / "data" / "paper_state.json"
JOURNAL_PATH = REPO_ROOT / "data" / "trade_journal.jsonl"
WIN_THRESHOLD = 0.97


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(dry_run: bool = False) -> None:
    _analyst._load_dotenv()

    if not STATE_PATH.exists():
        print("paper_state.json not found — nothing to do")
        return

    with STATE_PATH.open() as f:
        state = json.load(f)

    positions = state.get("positions", [])
    closed: list[dict] = []

    for pos in positions:
        if pos.get("status") != "open":
            continue
        cur = pos.get("current_price")
        if cur is None:
            continue
        try:
            cur_f = float(cur)
        except (TypeError, ValueError):
            continue
        if cur_f < WIN_THRESHOLD:
            continue

        shares = float(pos.get("shares") or 0.0)
        entry = float(pos.get("entry_price") or 0.0)
        stake = float(pos.get("stake") or pos.get("cost_basis") or (entry * shares))
        exit_price = min(cur_f, 0.99)
        proceeds = exit_price * shares
        pnl = proceeds - stake
        pnl_pct = (pnl / stake) if stake > 0 else 0.0

        question = pos.get("question", "?")
        outcome = pos.get("outcome", "?")
        print(
            f"{'[DRY] ' if dry_run else ''}Closing: {question[:60]} | "
            f"{outcome} | entry={entry:.3f} exit={exit_price:.3f} "
            f"pnl=${pnl:+.2f} ({pnl_pct:+.1%})"
        )

        if not dry_run:
            pos["status"] = "closed"
            pos["closed_at"] = _now_iso()
            pos["exit_reason"] = "force_close_resolved"
            pos["exit_price"] = exit_price
            pos["realized_pnl"] = round(pnl, 4)
            pos["journaled"] = True
            state["cash"] = round(float(state.get("cash", 0.0)) + proceeds, 2)

        journal_rec = {
            "event": "position_closed",
            "closed_at": _now_iso(),
            "opened_at": pos.get("opened_at"),
            "market_id": pos.get("market_id"),
            "question": question,
            "outcome": outcome,
            "token_id": pos.get("token_id"),
            "strategy": pos.get("strategy", "grinder"),
            "exit_reason": "force_close_resolved",
            "entry_price": entry,
            "exit_price": exit_price,
            "shares": shares,
            "cost_basis": round(stake, 4),
            "realized_pnl_usd": round(pnl, 4),
            "realized_pnl_pct": round(pnl_pct, 4),
        }
        closed.append(journal_rec)

        # Send Telegram win notification
        sign = "+" if pnl >= 0 else ""
        mood = "🟢" if pnl >= 0 else "🔴"
        msg = (
            f"{mood} *WIN — {outcome}*\n"
            f"_{question[:80]}_\n"
            f"Entry: {entry:.3f} → Exit: {exit_price:.3f}\n"
            f"P&L: `{sign}${pnl:.2f}` ({sign}{pnl_pct:.1%})\n"
            f"Reason: resolved at {cur_f:.3f}"
        )
        if dry_run:
            print(f"[DRY] Telegram: {msg[:120]}")
        else:
            _analyst.telegram_post(msg)

    if not closed:
        print("No resolved positions to close.")
        return

    if not dry_run:
        # Save updated state
        with STATE_PATH.open("w") as f:
            json.dump(state, f, indent=2)
        # Append to journal
        JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with JOURNAL_PATH.open("a") as f:
            for rec in closed:
                f.write(json.dumps(rec) + "\n")
        print(f"Closed {len(closed)} position(s) and wrote journal entries.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
