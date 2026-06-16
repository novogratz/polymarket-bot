#!/usr/bin/env python3
"""Manually SELL specific open positions by token_id at the live book bid.

Bypasses the bot's never-sell-below-entry / winner-floor guards
(POLYMARKET_ALLOW_LOSS_SELL=1) because these are deliberate user-ordered
exits, possibly at a loss. Probes the live CLOB bid per position so the
order matches at a real executable price.

Usage:
  uv run python scripts/manual_sell.py --dry-run
  uv run python scripts/manual_sell.py            # live
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Tokens the user asked to sell manually.
TARGET_TOKENS = {
    # Antonio Gracias / No
    "49230672971701746465122426449835191870819159491584700297160742718022482783703",
    # Miami Marlins vs. Philadelphia Phillies: O/U 4.5 / Over
    "43795065402261717214379510192942048116088888161067134164915431433730409921434",
}


def _load_settings(dry_run: bool):
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
    if dry_run:
        os.environ["POLYMARKET_DRY_RUN"] = "1"
    # Deliberate user-ordered exit — allow selling below entry.
    os.environ["POLYMARKET_ALLOW_LOSS_SELL"] = "1"

    from polymarket_bot.profiles import load_profile, apply_profile_to_env
    profile_path = REPO_ROOT / "configs" / "profiles" / "grinder_b.toml"
    if profile_path.exists():
        apply_profile_to_env(load_profile(profile_path), override=True)

    from polymarket_bot.config import Settings
    return Settings()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Flatten ALL open positions at the live book bid (loss floor "
        "bypassed). Previews by default; pass --yes to actually place orders.",
    )
    parser.add_argument("--dry-run", action="store_true", help="simulate against the dry ledger")
    parser.add_argument("--yes", action="store_true", help="actually place live SELL orders")
    args = parser.parse_args()
    execute = args.yes or args.dry_run

    settings = _load_settings(dry_run=args.dry_run)

    from polymarket_bot.models import Candidate, parse_dt
    from polymarket_bot.portfolio import Portfolio
    from polymarket_bot.trading import build_client, execute_live_sell, live_best_bid

    portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)
    client = build_client(settings)

    targets = [
        p for p in portfolio.positions
        if p.get("status") == "open" and str(p.get("token_id") or "")
    ]
    if not targets:
        print("No matching open positions found.")
        return

    for pos in targets:
        token_id = str(pos.get("token_id") or "")
        q = (pos.get("question") or "?")[:55]
        outcome = pos.get("outcome", "?")
        shares = float(pos.get("shares") or 0.0)
        entry = float(pos.get("entry_price") or 0.0)
        tick = float(pos.get("tick_size") or 0.01)

        # Probe the live book; fall back to cached price.
        live_bid = None
        try:
            live_bid = live_best_bid(client, token_id)
        except Exception as exc:
            print(f"   live-bid probe failed: {type(exc).__name__}: {exc}")
        bid = float(live_bid) if live_bid else float(pos.get("current_price") or 0.0)
        bid = max(bid, tick)

        proceeds = bid * shares
        pnl = proceeds - float(pos.get("stake") or entry * shares)
        verb = "SELL" if execute else "WOULD SELL"
        print(f"\n→ {verb} {shares:.2f} '{outcome}' @ {bid:.3f}  [{q}]")
        print(f"   entry={entry:.3f}  live_bid={bid:.3f}  proceeds=${proceeds:.2f}  pnl≈${pnl:+.2f}")

        if not execute:
            continue

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
            token_id=token_id,
            score=0.0,
            url=str(pos.get("url") or "https://polymarket.com"),
            best_bid=bid,
            best_ask=None,
            tick_size=tick,
            neg_risk=bool(pos.get("neg_risk")),
            accepts_orders=True,
            event_slug=str(pos.get("event_slug") or ""),
        )

        try:
            result = execute_live_sell(
                client, settings, candidate, portfolio, pos,
                shares=shares, reason="manual_sell",
            )
            portfolio.save(settings.state_path)
            if pos.get("status") == "closed":
                from polymarket_bot.main import _append_trade_journal
                _append_trade_journal(settings, pos, "manual_sell")
            print(f"   ✓ sold  order={result.order}")
        except Exception as exc:
            print(f"   ✗ SELL failed: {type(exc).__name__}: {exc}")

    if not execute:
        print("\n(preview only — no orders placed. Re-run with --yes to sell.)")


if __name__ == "__main__":
    main()
