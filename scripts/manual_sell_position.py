#!/usr/bin/env python3
"""Manually sell ONE open position at the live book bid, even at a loss.

User-initiated override of the never-sell-below-entry floor — sets
POLYMARKET_ALLOW_LOSS_SELL=1 for this process only. Sells the full position.

Usage:
  uv run python scripts/manual_sell_position.py --token-id <id>
  uv run python scripts/manual_sell_position.py --token-id <id> --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from polymarket_bot.models import Candidate, parse_dt
from polymarket_bot.portfolio import Portfolio


def _load_settings(dry_run: bool = False):
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
    if dry_run:
        os.environ["POLYMARKET_DRY_RUN"] = "1"
    os.environ["POLYMARKET_ALLOW_LOSS_SELL"] = "1"

    from polymarket_bot.profiles import load_profile, apply_profile_to_env
    profile_path = REPO_ROOT / "configs" / "profiles" / "grinder.toml"
    if profile_path.exists():
        apply_profile_to_env(load_profile(profile_path), override=True)

    from polymarket_bot.config import Settings
    return Settings()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = _load_settings(dry_run=args.dry_run)
    if args.dry_run and "dry_run" not in str(settings.state_path):
        # The Settings dry-run path swap compares against forward-slash string
        # defaults and never fires on Windows — refuse to touch the live ledger.
        print(f"ABORT: dry-run would write to live ledger {settings.state_path}")
        sys.exit(1)
    portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)

    pos = next(
        (
            p
            for p in portfolio.positions
            if p.get("status") == "open" and str(p.get("token_id") or "") == args.token_id
        ),
        None,
    )
    if pos is None:
        print(f"No open position with token_id {args.token_id}")
        sys.exit(1)

    from polymarket_bot.trading import build_client, execute_live_sell, live_best_bid

    client = build_client(settings)

    bid = live_best_bid(client, args.token_id)
    cached = float(pos.get("current_price") or 0.0)
    if bid is None or bid <= 0:
        print(f"Live book probe failed — falling back to cached price {cached:.3f}")
        bid = cached
    if bid <= 0:
        print("No executable bid — aborting.")
        sys.exit(1)

    shares = float(pos.get("shares") or 0.0)
    entry = float(pos.get("entry_price") or 0.0)
    print(
        f"→ SELL ALL {shares:.4f} '{pos.get('outcome')}' @ live bid {bid:.3f} "
        f"(entry {entry:.3f})  [{str(pos.get('question'))[:70]}]"
    )

    candidate = Candidate(
        market_id=str(pos.get("market_id") or ""),
        question=str(pos.get("question") or ""),
        slug=str(pos.get("slug") or ""),
        end_date=parse_dt(str(pos.get("end_date") or "")) if pos.get("end_date") else None,
        hours_to_close=0.0,
        liquidity=0.0,
        volume=0.0,
        outcome=str(pos.get("outcome") or ""),
        price=bid,
        token_id=args.token_id,
        score=0.0,
        url=str(pos.get("url") or "https://polymarket.com"),
        best_bid=bid,
        best_ask=None,
        tick_size=float(pos.get("tick_size") or 0.01),
        neg_risk=bool(pos.get("neg_risk")),
        accepts_orders=True,
        event_slug=str(pos.get("event_slug") or ""),
    )

    result = execute_live_sell(
        client,
        settings,
        candidate,
        portfolio,
        pos,
        shares=shares,
        reason="manual_user_sell",
    )
    pos["sync_closed"] = True  # don't let live sync re-open it
    portfolio.save(settings.state_path)
    if pos.get("status") == "closed":
        from polymarket_bot.main import _append_trade_journal
        _append_trade_journal(settings, pos, "manual_user_sell")
    print(f"   ✓ sold  order={result.order}")


if __name__ == "__main__":
    main()
