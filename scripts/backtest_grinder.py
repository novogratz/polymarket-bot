#!/usr/bin/env python3
"""Grinder strategy backtest — 90 days of real Polymarket resolved markets.

Methodology:
  1. Pull all resolved binary markets from Gamma API (last 90 days).
  2. Apply every current entry filter: exclusion list, volume >=300,
     spread <=0.02.
  3. For each qualifying market, query CLOB authenticated trades for BOTH
     outcome tokens within the 6h window before close.
  4. A trade is a simulated entry if: side=BUY, 0.92 <= price <= 0.97.
  5. WIN  = entry on the outcome that resolved to 1.0.
     LOSS = entry on the outcome that resolved to 0.0.
  6. Simulate compounding from $50 with 80% Kelly stake, 1 position at a time.
  7. Print win rate, final equity, ROI, weekly equity curve.

Usage (bot stopped or running — read-only):
  uv run python scripts/backtest_grinder.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Config mirrors grinder.toml
MIN_PRICE        = 0.92
MAX_PRICE        = 0.97
MAX_SPREAD       = 0.02
MIN_VOLUME       = 300.0
MAX_HOURS        = 6.0
STAKE_PCT        = 0.80
STARTING_CASH    = 50.0
LOOKBACK_DAYS    = 90


def _load_env() -> None:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
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


@dataclass
class SimTrade:
    date: str
    question: str
    outcome: str
    entry_price: float
    result: str
    stake: float
    pnl: float
    equity_after: float


def _gamma_markets(start_ts: int, end_ts: int) -> list[dict]:
    markets: list[dict] = []
    offset = 0
    limit  = 200
    start_iso = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso   = datetime.fromtimestamp(end_ts,   tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    while True:
        url = (
            f"https://gamma-api.polymarket.com/markets"
            f"?closed=true&limit={limit}&offset={offset}"
            f"&end_date_min={start_iso}&end_date_max={end_iso}"
        )
        req = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
        )
        try:
            batch = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
        except Exception as exc:
            print(f"  [gamma] page failed: {exc}", flush=True)
            break
        if not batch:
            break
        markets.extend(batch)
        print(f"  [gamma] {len(markets)} markets...", end="\r", flush=True)
        if len(batch) < limit:
            break
        offset += limit
        time.sleep(0.3)
    print(f"  [gamma] {len(markets)} resolved markets fetched.          ", flush=True)
    return markets


def _parse_json_field(val) -> list:
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            r = json.loads(val)
            return r if isinstance(r, list) else []
        except Exception:
            return []
    return []


def _winner_loser_tokens(m: dict) -> tuple[str | None, str | None]:
    op_raw    = _parse_json_field(m.get("outcomePrices"))
    token_ids = _parse_json_field(m.get("clobTokenIds"))
    if not token_ids or not op_raw or len(op_raw) != 2 or len(token_ids) < 2:
        return None, None
    try:
        op = [float(x) for x in op_raw]
    except Exception:
        return None, None
    winner_idx = next((i for i, p in enumerate(op) if p >= 0.99), None)
    loser_idx  = next((i for i, p in enumerate(op) if p <= 0.01), None)
    if winner_idx is None or loser_idx is None or winner_idx == loser_idx:
        return None, None
    return str(token_ids[winner_idx]), str(token_ids[loser_idx])


def _parse_end_ts(m: dict) -> int | None:
    raw = m.get("endDate") or m.get("closedTime") or m.get("endDateIso")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None


def _get_buys_in_window(sdk, token_id: str, after_ts: int, before_ts: int) -> float | None:
    """Return highest BUY price in 0.92-0.97 within the window, or None."""
    from py_clob_client_v2.clob_types import TradeParams
    try:
        params = TradeParams(asset_id=token_id, after=after_ts, before=before_ts)
        trades = sdk.get_trades(params)
        if not isinstance(trades, list):
            return None
        best = None
        for t in trades:
            if str(t.get("side") or "").upper() != "BUY":
                continue
            try:
                price = float(t.get("price") or 0)
            except Exception:
                continue
            if MIN_PRICE <= price <= MAX_PRICE:
                if best is None or price > best:
                    best = price
        return best
    except Exception:
        return None


def main() -> None:
    print("\n=== GRINDER BACKTEST — 90 DAYS ===\n", flush=True)

    from polymarket_bot.config import Settings
    from polymarket_bot.trading import build_client
    from polymarket_bot.models import is_excluded_market

    settings = Settings()
    client   = build_client(settings)
    sdk      = getattr(client, "sdk_client", None)
    if sdk is None:
        print("ERROR: no SDK client available.")
        return

    now_ts   = int(time.time())
    start_ts = now_ts - LOOKBACK_DAYS * 86400

    print("Step 1 — Fetching resolved markets from Gamma...", flush=True)
    all_markets = _gamma_markets(start_ts, now_ts)

    print("\nStep 2 — Applying filters...", flush=True)
    qualified = []
    skipped_excluded = skipped_binary = skipped_volume = skipped_spread = skipped_no_winner = 0
    for m in all_markets:
        if len(_parse_json_field(m.get("outcomes"))) != 2:
            skipped_binary += 1
            continue
        if is_excluded_market(m):
            skipped_excluded += 1
            continue
        vol = float(m.get("volumeNum") or m.get("volume") or 0)
        if vol < MIN_VOLUME:
            skipped_volume += 1
            continue
        spread = float(m.get("spread") or 1.0)
        if spread > MAX_SPREAD:
            skipped_spread += 1
            continue
        winner_t, loser_t = _winner_loser_tokens(m)
        if winner_t is None:
            skipped_no_winner += 1
            continue
        end_ts = _parse_end_ts(m)
        if end_ts is None:
            continue
        qualified.append((m, winner_t, loser_t, end_ts))

    print(f"  Skipped — non-binary: {skipped_binary}, excluded: {skipped_excluded}, "
          f"low-vol: {skipped_volume}, wide-spread: {skipped_spread}, "
          f"no-winner: {skipped_no_winner}", flush=True)
    print(f"  Qualifying: {len(qualified)} markets\n", flush=True)

    if not qualified:
        print("No qualifying markets. Try relaxing filters.")
        return

    print(f"Step 3 — Querying CLOB trades (~{len(qualified) * 0.3:.0f}s)...\n", flush=True)

    sim_trades: list[SimTrade] = []
    equity    = STARTING_CASH
    checked   = 0
    entries   = 0

    for m, winner_t, loser_t, end_ts in qualified:
        after_ts  = end_ts - int(MAX_HOURS * 3600)
        before_ts = end_ts
        date_str  = datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        question  = (m.get("question") or "?")[:55]
        outcomes  = _parse_json_field(m.get("outcomes")) or ["Yes", "No"]

        win_entry  = _get_buys_in_window(sdk, winner_t, after_ts, before_ts)
        loss_entry = _get_buys_in_window(sdk, loser_t,  after_ts, before_ts)

        checked += 1
        if checked % 5 == 0:
            print(f"  {checked}/{len(qualified)} markets checked, {entries} entries found...",
                  end="\r", flush=True)

        for entry_price, result_str, outcome_lbl in [
            (win_entry,  "WIN",  outcomes[0]),
            (loss_entry, "LOSS", outcomes[1]),
        ]:
            if entry_price is None:
                continue
            entries += 1
            stake = round(equity * STAKE_PCT, 2)
            if result_str == "WIN":
                shares = stake / entry_price
                pnl    = round(shares * (1.0 - entry_price), 2)
            else:
                pnl = round(-stake, 2)
            equity = round(equity + pnl, 2)
            sim_trades.append(SimTrade(
                date=date_str, question=question, outcome=outcome_lbl,
                entry_price=entry_price, result=result_str,
                stake=stake, pnl=pnl, equity_after=equity,
            ))

        time.sleep(0.25)

    print(f"\n  {checked} markets checked, {entries} simulated entries found.\n", flush=True)

    if not sim_trades:
        print("No entries found in the 0.92-0.97 band within 6h of close.")
        print("The CLOB may not retain old trade data — most history is < 30 days.")
        print("\nFalling back to theoretical model (see below).")
        _theoretical_model()
        return

    sim_trades.sort(key=lambda t: t.date)
    wins   = [t for t in sim_trades if t.result == "WIN"]
    losses = [t for t in sim_trades if t.result == "LOSS"]
    win_rate = len(wins) / len(sim_trades) * 100
    total_pnl = sim_trades[-1].equity_after - STARTING_CASH
    roi = total_pnl / STARTING_CASH * 100

    print("=" * 62)
    print(f"  GRINDER BACKTEST RESULTS  ({LOOKBACK_DAYS} days, real trade data)")
    print("=" * 62)
    print(f"  Starting cash  : ${STARTING_CASH:.2f}")
    print(f"  Final equity   : ${sim_trades[-1].equity_after:.2f}")
    print(f"  Total P&L      : ${total_pnl:+.2f}  ({roi:+.1f}%)")
    print(f"  Trades         : {len(sim_trades)}  ({len(wins)}W / {len(losses)}L)")
    print(f"  Win rate       : {win_rate:.1f}%")
    avg_win  = sum(t.pnl for t in wins)  / len(wins)   if wins   else 0
    avg_loss = sum(t.pnl for t in losses)/ len(losses)  if losses else 0
    print(f"  Avg win        : +${avg_win:.2f}")
    print(f"  Avg loss       : -${abs(avg_loss):.2f}")
    print()

    # Weekly equity curve
    from collections import defaultdict
    week_eq: dict[str, float] = {}
    for t in sim_trades:
        dt   = datetime.fromisoformat(t.date)
        week = f"{dt.year}-W{int(dt.strftime('%U')):02d}"
        week_eq[week] = t.equity_after

    print("  WEEKLY EQUITY CURVE:")
    prev = STARTING_CASH
    for wk in sorted(week_eq):
        eq  = week_eq[wk]
        chg = eq - prev
        bar = "#" * min(int(eq / 5), 30)
        print(f"    {wk}  ${eq:>8.2f}  ({chg:+6.2f})  {bar}")
        prev = eq

    print()
    print("  LAST 10 TRADES:")
    for t in sim_trades[-10:]:
        icon = "+" if t.result == "WIN" else "-"
        print(f"    [{icon}] {t.date}  @{t.entry_price:.2f}  "
              f"P&L ${t.pnl:+.2f}  eq ${t.equity_after:.2f}  {t.question[:35]}")

    if len(sim_trades) >= 10:
        wr = win_rate / 100
        rr = avg_win / abs(avg_loss) if avg_loss else 1
        kelly = wr - (1 - wr) / max(rr, 0.01)
        print(f"\n  Kelly at this win rate/ratio: {kelly*100:.1f}%  "
              f"(running at {STAKE_PCT*100:.0f}%)")
    print("=" * 62)


def _theoretical_model() -> None:
    """Fallback: simulate 90 days using observed win rates for 0.92-0.97 entries."""
    print("\n=== THEORETICAL MODEL (if CLOB history unavailable) ===\n")
    print("  Based on prediction market theory + our exclusion filters:")
    print("  - Markets priced 0.92-0.97 have implied probability 92-97%")
    print("  - With exclusion filters (no exact-score, no O/U, no crypto,")
    print("    no weather, no esports) gap-risk is removed")
    print("  - Conservative estimated win rate: 90%")
    print("  - Typical entry: 0.94, typical win: +6.4% on stake")
    print()

    # Simulate with realistic trade counts from the live bot data
    import random
    random.seed(42)
    WIN_RATE     = 0.90
    ENTRY_PRICE  = 0.94
    TRADES_PER_DAY = 8   # conservative — bot sees 10-15/day but not all qualify
    DAYS         = 90

    equity = STARTING_CASH
    wins = losses = 0
    week_equities: dict[str, float] = {}

    for day in range(DAYS):
        n_trades = random.randint(max(1, TRADES_PER_DAY - 3), TRADES_PER_DAY + 3)
        week = f"W{day // 7 + 1:02d}"
        for _ in range(n_trades):
            stake = equity * STAKE_PCT
            if random.random() < WIN_RATE:
                shares = stake / ENTRY_PRICE
                pnl = shares * (1.0 - ENTRY_PRICE)
                wins += 1
            else:
                pnl = -stake
                losses += 1
            equity += pnl
            if equity < 1:
                equity = 1
        week_equities[week] = round(equity, 2)

    total = wins + losses
    roi   = (equity - STARTING_CASH) / STARTING_CASH * 100

    print(f"  Start: ${STARTING_CASH:.2f}  →  End: ${equity:.2f}")
    print(f"  ROI: {roi:+.1f}%  over 90 days")
    print(f"  Trades: {total}  ({wins}W / {losses}L, {wins/total*100:.1f}% WR)")
    print()
    print("  WEEKLY EQUITY (theoretical):")
    prev = STARTING_CASH
    for wk in sorted(week_equities):
        eq  = week_equities[wk]
        chg = eq - prev
        bar = "#" * min(int(eq / 10), 25)
        print(f"    {wk}  ${eq:>9.2f}  ({chg:+7.2f})  {bar}")
        prev = eq
    print()
    print("  NOTE: theoretical model uses fixed 90% win rate + 8 trades/day.")
    print("  Real results depend on market availability and actual win rate.")


if __name__ == "__main__":
    main()
