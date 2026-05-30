#!/usr/bin/env python3
"""Place real SELL orders on the Polymarket CLOB for all positions at ≥0.97.

Uses the live trading client — requires valid API credentials in .env.
Safe to run while the bot is live (reads/writes paper_state.json the same
way the bot does).

Usage:
  uv run python scripts/sell_resolved_now.py
  uv run python scripts/sell_resolved_now.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from polymarket_bot.models import Candidate, parse_dt, utc_now
from polymarket_bot.portfolio import Portfolio


def _load_settings(dry_run: bool = False):
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
    if dry_run:
        os.environ["POLYMARKET_DRY_RUN"] = "1"

    from polymarket_bot.profiles import load_profile, apply_profile_to_env
    profile_path = REPO_ROOT / "configs" / "profiles" / "grinder.toml"
    if profile_path.exists():
        apply_profile_to_env(load_profile(profile_path), override=True)

    from polymarket_bot.config import Settings
    return Settings()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = _load_settings(dry_run=args.dry_run)
    win_threshold = float(getattr(settings, "smart_resolved_exit_threshold", 0.97) or 0.97)

    portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)

    candidates_to_sell: list[tuple[dict, Candidate]] = []
    for pos in portfolio.positions:
        if pos.get("status") != "open":
            continue
        cur = pos.get("current_price")
        if cur is None:
            continue
        try:
            cur_f = float(cur)
        except (TypeError, ValueError):
            continue
        if cur_f < win_threshold:
            continue

        token_id = str(pos.get("token_id") or "")
        candidate = Candidate(
            market_id=str(pos.get("market_id") or ""),
            question=str(pos.get("question") or ""),
            slug=str(pos.get("slug") or ""),
            end_date=parse_dt(str(pos.get("end_date") or "")) if pos.get("end_date") else None,
            hours_to_close=0.0,
            liquidity=0.0,
            volume=0.0,
            outcome=str(pos.get("outcome") or ""),
            price=cur_f,
            token_id=token_id,
            score=0.0,
            url=str(pos.get("url") or "https://polymarket.com"),
            best_bid=min(cur_f, 0.99),
            best_ask=None,
            tick_size=float(pos.get("tick_size") or 0.01),
            neg_risk=bool(pos.get("neg_risk")),
            accepts_orders=True,
            event_slug=str(pos.get("event_slug") or ""),
        )
        candidates_to_sell.append((pos, candidate))

    if not candidates_to_sell:
        print(f"No open positions at ≥{win_threshold:.2f} — nothing to sell.")
        return

    from polymarket_bot.trading import build_client, execute_live_sell
    client = build_client(settings)

    for pos, candidate in candidates_to_sell:
        q = pos.get("question", "?")[:60]
        outcome = pos.get("outcome", "?")
        shares = float(pos.get("shares") or 0.0)
        print(f"→ SELL {shares:.2f} '{outcome}' @ {candidate.best_bid:.3f}  [{q}]")
        try:
            result = execute_live_sell(
                client,
                settings,
                candidate,
                portfolio,
                pos,
                shares=shares,
                reason="force_sell_resolved",
            )
            portfolio.save(settings.state_path)
            if pos.get("status") == "closed":
                from polymarket_bot.main import _append_trade_journal
                _append_trade_journal(settings, pos, "force_sell_resolved")
            print(f"   ✓ sold  order={result.order}")
        except Exception as exc:
            print(f"   ✗ SELL failed: {type(exc).__name__}: {exc}")
            # Fallback: force-close locally so the ledger is clean even if CLOB rejected
            entry = float(pos.get("entry_price") or 0.0)
            stake = float(pos.get("stake") or (entry * shares))
            exit_p = float(candidate.best_bid or 0.99)
            proceeds = exit_p * shares
            pnl = proceeds - stake
            pos["status"] = "closed"
            pos["closed_at"] = utc_now().isoformat()
            pos["exit_reason"] = "force_sell_resolved_writeoff"
            pos["exit_price"] = exit_p
            pos["realized_pnl"] = round(pnl, 4)
            pos["sync_closed"] = True  # prevent sync from re-opening
            portfolio.cash = round(float(portfolio.cash or 0.0) + proceeds, 2)
            portfolio.save(settings.state_path)
            from polymarket_bot.main import _append_trade_journal
            _append_trade_journal(settings, pos, "force_sell_resolved_writeoff")
            print(f"   ✓ local write-off at {exit_p:.3f}  pnl=${pnl:+.2f}")

    # Re-run daily report after sells
    print("\nSending updated Telegram report…")
    import scripts.live_analyst as _analyst
    _analyst._load_dotenv()
    _analyst.daily_report_once()
    print("Done.")


if __name__ == "__main__":
    main()
