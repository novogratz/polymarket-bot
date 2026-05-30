#!/usr/bin/env python
"""Offline backtest for the grinder thesis — does buying a binary outcome at
price p within H hours of close actually have edge?

Read-only. Uses only public Polymarket APIs (Gamma closed markets + CLOB
price history). Never touches the SDK, the ledger, or any live state.

Method (no lookahead — resolution is known, so win/loss is exact):
  1. Pull recently *closed* binary markets from Gamma (active=false,
     closed=true), skipping the same categories the live bot excludes.
  2. For each outcome token, fetch the CLOB price history over the window
     [close - max_hours, close]. Find the first snapshot whose price lands
     in the entry band [min_price, max_price] — that is a simulated grinder
     entry. Buy 1 share at that price.
  3. The token's final resolution (outcomePrices) decides the payout:
     win → +1, lose → 0. Per-trade return = (payout - entry) / entry.
  4. Aggregate win rate and EV, and report the *edge*: realised win rate
     minus the average entry price. Positive edge = the band is mispriced
     in our favour; ~zero edge = the strategy is a fee/spread bleed.

Sweeps several (band, hours) combinations so you can read the best
parameters off the table instead of guessing.

Usage:
  uv run python scripts/backtest_grinder.py --max-markets 300 --days 14
  uv run python scripts/backtest_grinder.py --band 0.90 0.94 --hours 3
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polymarket_bot.gamma import GammaClient  # noqa: E402
from polymarket_bot.models import is_excluded_market, parse_json_list, parse_dt  # noqa: E402

CLOB_BASE = "https://clob.polymarket.com"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def fetch_price_history(token_id: str, fidelity: int = 60) -> list[tuple[int, float]]:
    """Full CLOB price history for one token as sorted [(unix_ts, price)].

    Uses interval=max so we get the whole life of the market, then anchor
    'close' to the LAST traded point (robust to endDate lagging behind the
    final trade — otherwise the window before a nominal endDate is just the
    post-resolution flatline at 1.0/0.0 and the band looks empty).
    """
    query = urllib.parse.urlencode({"market": token_id, "interval": "max", "fidelity": fidelity})
    req = urllib.request.Request(
        f"{CLOB_BASE}/prices-history?{query}",
        headers={"Accept": "application/json", "User-Agent": "polymarket-bot/backtest"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    history = payload.get("history") if isinstance(payload, dict) else None
    if not isinstance(history, list):
        return []
    out: list[tuple[int, float]] = []
    for point in history:
        try:
            out.append((int(point["t"]), float(point["p"])))
        except (TypeError, ValueError, KeyError):
            continue
    out.sort(key=lambda x: x[0])
    return out


def fetch_closed_markets(days: int, max_markets: int) -> list[dict]:
    """Recently closed markets, newest first, paginated until max_markets.

    Gamma caps a single response (~100), so walk ``end_date_max`` backward
    from the oldest market seen so far until we hit the cap or run out.
    """
    client = GammaClient()
    floor = _utc_now() - timedelta(days=days)
    cursor = _utc_now()
    out: list[dict] = []
    seen: set[str] = set()
    while len(out) < max_markets and cursor > floor:
        page = client.get_markets(
            active=False,
            closed=True,
            limit=100,
            order="end_date",
            ascending=False,
            end_date_min=floor,
            end_date_max=cursor,
        )
        if not isinstance(page, list) or not page:
            break
        oldest = cursor
        added = 0
        for market in page:
            key = str(market.get("id") or market.get("conditionId") or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            out.append(market)
            added += 1
            end_dt = parse_dt(market.get("endDate"))
            if end_dt is not None and end_dt < oldest:
                oldest = end_dt
        if added == 0 or oldest >= cursor:
            break  # no forward progress
        cursor = oldest - timedelta(seconds=1)
    return out[:max_markets]


def winning_index(market: dict) -> int | None:
    """Index (0/1) of the outcome that resolved to 1, or None if undecided."""
    prices = parse_json_list(market.get("outcomePrices"))
    if len(prices) != 2:
        return None
    try:
        p0, p1 = float(prices[0]), float(prices[1])
    except (TypeError, ValueError):
        return None
    if p0 >= 0.99 and p1 <= 0.01:
        return 0
    if p1 >= 0.99 and p0 <= 0.01:
        return 1
    return None  # not cleanly resolved (refunded / 50-50 / still open)


# One token's history reduced to (hours_to_close, price) points + its result.
TokenSeries = tuple[list[tuple[float, float]], int]  # ([(h2c, price)], won 0/1)


def build_token_series(markets: list[dict], sleep: float) -> list[TokenSeries]:
    """Fetch each resolved token's full history once; precompute h2c per point."""
    series: list[TokenSeries] = []
    total = len(markets)
    for i, market in enumerate(markets, 1):
        win_idx = winning_index(market)
        if win_idx is None or is_excluded_market(market):
            continue
        token_ids = [str(t) for t in parse_json_list(market.get("clobTokenIds"))]
        if len(token_ids) != 2:
            continue
        for idx, token in enumerate(token_ids):
            hist = fetch_price_history(token)
            if sleep:
                time.sleep(sleep)
            if len(hist) < 2:
                continue
            close_ts = hist[-1][0]  # last traded point = effective close
            points = [((close_ts - t) / 3600.0, p) for t, p in hist if close_ts - t >= 0]
            if points:
                series.append((points, 1 if idx == win_idx else 0))
        if i % 25 == 0:
            print(f"  …fetched {i}/{total} markets", flush=True)
    return series


def simulate(series: list[TokenSeries], lo: float, hi: float, hours: float) -> dict:
    """Aggregate stats for entry band [lo, hi] within `hours` of close.

    Grinder entry = the EARLIEST snapshot inside the window whose price is in
    band (largest h2c ≤ hours). One entry per token at most.
    """
    trades: list[tuple[float, int]] = []  # (entry_price, won 0/1)
    for points, won in series:
        entry = None
        for h2c, price in points:  # points are chronological (h2c descending)
            if h2c <= hours and lo <= price <= hi:
                entry = price
                break
        if entry is not None:
            trades.append((entry, won))
    n = len(trades)
    if n == 0:
        return {"n": 0}
    wins = sum(w for _, w in trades)
    avg_entry = sum(e for e, _ in trades) / n
    # Buy 1 share at entry e; payout 1 if win else 0. Return = (payout-e)/e.
    ev_per_trade = sum(((w - e) / e) for e, w in trades) / n
    return {
        "n": n,
        "win_rate": wins / n,
        "avg_entry": avg_entry,
        "edge": (wins / n) - avg_entry,  # realised WR minus priced-in WR
        "ev_per_trade_pct": ev_per_trade * 100.0,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=14, help="lookback window for closed markets")
    ap.add_argument("--max-markets", type=int, default=300, help="cap on markets fetched")
    ap.add_argument("--band", type=float, nargs=2, metavar=("LO", "HI"), help="single band, e.g. --band 0.90 0.94")
    ap.add_argument("--hours", type=float, help="single hours-to-close window")
    ap.add_argument("--sleep", type=float, default=0.03, help="delay between CLOB calls (rate-limit politeness)")
    args = ap.parse_args()

    print(f"Fetching closed markets (last {args.days}d, cap {args.max_markets})…", flush=True)
    markets = fetch_closed_markets(args.days, args.max_markets)
    resolved = [m for m in markets if winning_index(m) is not None and not is_excluded_market(m)]
    print(f"  {len(markets)} closed markets, {len(resolved)} cleanly-resolved binaries in scope", flush=True)
    if not resolved:
        print("No resolved binary markets to backtest. Try a wider --days.")
        return

    print(f"Fetching price history for {len(resolved)} markets (2 tokens each)…", flush=True)
    series = build_token_series(resolved, args.sleep)
    print(f"  {len(series)} token series with usable history\n", flush=True)
    if not series:
        print("No usable price history. The CLOB prices-history endpoint may be rate-limiting.")
        return

    if args.band and args.hours:
        combos = [(args.band[0], args.band[1], args.hours)]
    else:
        combos = [
            (lo, hi, h)
            for (lo, hi) in [(0.90, 0.94), (0.87, 0.95), (0.85, 0.97), (0.95, 0.99)]
            for h in (1.0, 3.0, 6.0)
        ]

    print(f"{'band':>12} {'hrs':>4} {'trades':>7} {'win%':>6} {'avg_entry':>10} {'edge':>7} {'EV/trade':>9}")
    print("-" * 64)
    best = None
    for lo, hi, h in combos:
        r = simulate(series, lo, hi, h)
        if r["n"] == 0:
            print(f"{lo:.2f}-{hi:.2f} {h:>4.0f} {'0':>7}   (no entries in band/window)")
            continue
        print(
            f"{lo:.2f}-{hi:.2f} {h:>4.0f} {r['n']:>7} {r['win_rate']*100:>5.1f}% "
            f"{r['avg_entry']:>10.3f} {r['edge']:>+7.3f} {r['ev_per_trade_pct']:>+8.2f}%"
        )
        # "best" = highest EV/trade with a non-trivial sample.
        if r["n"] >= 10 and (best is None or r["ev_per_trade_pct"] > best[1]):
            best = ((lo, hi, h), r["ev_per_trade_pct"], r)

    print("-" * 64)
    if best:
        (lo, hi, h), ev, r = best
        verdict = "REAL EDGE ✅" if ev > 0.5 else ("MARGINAL ⚠️" if ev > -0.5 else "NEGATIVE — bleed ❌")
        print(
            f"Best: band {lo:.2f}-{hi:.2f}, {h:.0f}h → EV {ev:+.2f}%/trade, "
            f"win {r['win_rate']*100:.1f}% vs priced {r['avg_entry']*100:.1f}% "
            f"(n={r['n']}). Verdict: {verdict}"
        )
        print(
            "\nKelly note: with ruinous downside, growth-optimal stake fraction "
            "≈ edge / (1 - entry). Even a +1% EV band wants only a few % of "
            "bankroll per trade — confirm before raising the $10 cap."
        )
    else:
        print("No band reached a 10-trade sample. Widen --days / --max-markets.")


if __name__ == "__main__":
    main()
