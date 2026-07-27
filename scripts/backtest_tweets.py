#!/usr/bin/env python3
"""Backtest the tweet-count lane against HISTORICAL CLOB prices.

Answers the question the live calibration can't: did the MARKET price these
brackets well enough that our model had no realized edge, or would the lane's
entry rule (band 0.85-0.94 + model edge gate) have made money on past windows?

Method:
  1. Discover closed elonmusk counting windows of the last --days days by
     probing Gamma event slugs (elon-musk-of-tweets-<m1>-<d1>-<m2>-<d2>,
     weekly and 2-day cadences).
  2. For each bracket market, pull the hourly CLOB price history of the YES
     token (clob.polymarket.com/prices-history).
  3. Walk the window: at each price point, reconstruct what the live bot
     would have seen — executable ask ~ mid + 1 tick, model probability
     fitted ONLY on posts known at that time (56d hour-of-week profile,
     72h activity multiplier, negative-binomial tails; NO regime file).
  4. First time a side (Yes or No) is in the 0.85-0.94 band AND clears the
     edge gate, enter $--stake and hold to resolution (final count decides).
  5. Report per-gate: n, win rate, ROI — plus the band-only baseline
     (gate 0.0 = every band entry, no model) to isolate the model's value.

Honest caveats printed with the results: mid+1-tick is an optimistic ask
proxy, no slippage/queue, ignores concurrent-capital limits, elonmusk only.

Usage:
  uv run python scripts/backtest_tweets.py --days 60
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from polymarket_bot.tweet_model import (  # noqa: E402
    _bracket_prob,
    _count_between,
    parse_tweet_question,
)

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
XTRACKER = "https://xtracker.polymarket.com/api"
_MONTHS = ("january", "february", "march", "april", "may", "june",
           "july", "august", "september", "october", "november", "december")


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-bot/backtest"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_posts(handle: str) -> tuple:
    data = _get(f"{XTRACKER}/users/{handle}/posts")["data"]
    stamps = []
    for p in data:
        try:
            stamps.append(datetime.fromisoformat(str(p["createdAt"]).replace("Z", "+00:00")).timestamp())
        except (KeyError, ValueError):
            continue
    stamps.sort()
    return tuple(stamps)


def _slug(d1: datetime, d2: datetime) -> str:
    return (f"elon-musk-of-tweets-{_MONTHS[d1.month - 1]}-{d1.day}"
            f"-{_MONTHS[d2.month - 1]}-{d2.day}")


def discover_windows(days: int) -> list[dict]:
    """Probe Gamma for closed elonmusk windows: weekly (D, D+7) and 2-day
    (D, D+2) cadences, window boundaries at 16:00 UTC."""
    now = datetime.now(timezone.utc)
    found, seen = [], set()
    for back in range(days, 1, -1):
        d1 = (now - timedelta(days=back)).replace(hour=16, minute=0, second=0, microsecond=0)
        for span in (7, 2):
            d2 = d1 + timedelta(days=span)
            if d2 > now:  # only CLOSED windows
                continue
            slug = _slug(d1, d2)
            if slug in seen:
                continue
            seen.add(slug)
            try:
                ev = _get(f"{GAMMA}/events?{urllib.parse.urlencode({'slug': slug})}")
            except Exception:
                continue
            if not ev or not ev[0].get("closed"):
                continue
            found.append({"slug": slug, "start": d1, "end": d2 - timedelta(seconds=1),
                          "markets": ev[0].get("markets") or []})
            print(f"  window {slug}  ({len(found[-1]['markets'])} brackets)")
    return found


# ── point-in-time model fit (mirror of tweet_model, explicit as-of time) ────
_fit_cache: dict[int, tuple] = {}


def _fit_asof(stamps: tuple, asof: datetime) -> tuple | None:
    key = int(asof.timestamp() // 86400)
    if key in _fit_cache:
        return _fit_cache[key]
    train_start = asof - timedelta(days=56)
    if not stamps or stamps[0] > train_start.timestamp():
        return None
    counts, exposure = [0.0] * 168, [0.0] * 168
    cur = train_start.replace(minute=0, second=0, microsecond=0)
    while cur < asof:
        cell = cur.weekday() * 24 + cur.hour
        counts[cell] += _count_between(stamps, cur, cur + timedelta(hours=1))
        exposure[cell] += 1.0
        cur += timedelta(hours=1)
    mean_rate = sum(counts) / max(sum(exposure), 1.0)
    profile = tuple((c + 4.0 * mean_rate) / (e + 4.0) if e else mean_rate
                    for c, e in zip(counts, exposure))
    daily = []
    cur = asof - timedelta(days=90)
    while cur + timedelta(days=1) <= asof:
        daily.append(_count_between(stamps, cur, cur + timedelta(days=1)))
        cur += timedelta(days=1)
    m = sum(daily) / len(daily)
    var = sum((x - m) ** 2 for x in daily) / max(len(daily) - 1, 1)
    r_daily = (m * m / (var - m)) if var > m else 1e9
    out = (profile, max(r_daily, 0.5))
    _fit_cache[key] = out
    return out


def _expected(profile: tuple, a: datetime, b: datetime) -> float:
    lam, cur = 0.0, a
    while cur < b:
        step = min(b, (cur + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0))
        if step <= cur:
            step = min(b, cur + timedelta(hours=1))
        lam += profile[cur.weekday() * 24 + cur.hour] * ((step - cur).total_seconds() / 3600.0)
        cur = step
    return lam


def _model_p_in(stamps, profile, r_daily, start, end, asof, lo, hi) -> float:
    current = _count_between(stamps, start, min(asof, end))
    expected = _expected(profile, asof - timedelta(hours=72), asof)
    actual = float(_count_between(stamps, asof - timedelta(hours=72), asof))
    mult = max(0.4, min(2.5, (actual + 0.5 * expected) / (1.5 * expected))) if expected > 0 else 1.0
    mean_rem = _expected(profile, max(start, asof), end) * mult
    days_left = max((end - asof).total_seconds() / 86400.0, 0.05)
    r_eff = max(1.0, r_daily * days_left)
    return _bracket_prob(current, mean_rem, r_eff, lo, hi if hi is not None else None)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--stake", type=float, default=5.0)
    ap.add_argument("--gates", default="0.0,0.05,0.08,0.12")
    ap.add_argument("--min-hours-left", type=float, default=4.0)
    args = ap.parse_args()
    gates = [float(g) for g in args.gates.split(",")]

    print("fetching elonmusk post history…")
    stamps = _fetch_posts("elonmusk")
    print(f"  {len(stamps)} posts "
          f"({datetime.fromtimestamp(stamps[0], tz=timezone.utc):%Y-%m-%d} → now)")
    print(f"discovering closed windows ({args.days} days back)…")
    windows = discover_windows(args.days)
    print(f"{len(windows)} closed windows found\n")

    results: dict[float, list] = {g: [] for g in gates}
    for w in windows:
        final = _count_between(stamps, w["start"], w["end"] + timedelta(seconds=1))
        fit = _fit_asof(stamps, w["start"])
        for mkt in w["markets"]:
            parsed = parse_tweet_question(str(mkt.get("question") or ""))
            if parsed is None:
                continue
            lo, hi = int(parsed["lo"] or 0), parsed["hi"]
            try:
                tokens = json.loads(mkt.get("clobTokenIds") or "[]")
            except ValueError:
                tokens = []
            if not tokens:
                continue
            q = urllib.parse.urlencode({
                "market": tokens[0], "fidelity": 120,
                "startTs": int(w["start"].timestamp()), "endTs": int(w["end"].timestamp()),
            })
            try:
                hist = _get(f"{CLOB}/prices-history?{q}").get("history") or []
            except Exception:
                continue
            won_in = lo <= final and (hi is None or final <= hi)
            entered: dict[tuple, bool] = {}
            for pt in hist:
                asof = datetime.fromtimestamp(int(pt["t"]), tz=timezone.utc)
                if (w["end"] - asof).total_seconds() < args.min_hours_left * 3600:
                    break
                if asof < w["start"]:
                    continue
                p_yes_mid = float(pt["p"])
                fit = _fit_asof(stamps, asof) or fit
                if fit is None:
                    continue
                profile, r_daily = fit
                p_in = None
                for side, ask, won in (
                    ("Yes", p_yes_mid + 0.01, won_in),
                    ("No", (1.0 - p_yes_mid) + 0.01, not won_in),
                ):
                    if not (0.85 <= ask <= 0.94):
                        continue
                    if p_in is None:
                        p_in = _model_p_in(stamps, profile, r_daily,
                                           w["start"], w["end"], asof, lo, hi)
                    p_model = p_in if side == "Yes" else 1.0 - p_in
                    for g in gates:
                        if (side, g) in entered:
                            continue
                        if p_model >= ask + g:
                            entered[(side, g)] = True
                            pnl = args.stake * (1.0 / ask - 1.0) if won else -args.stake
                            results[g].append({"won": won, "ask": ask, "pnl": pnl,
                                               "q": mkt.get("question"), "side": side})

    print(f"\n=== BACKTEST — elonmusk, last {args.days} days, ${args.stake:.0f}/entry, "
          f"band 0.85-0.94, ask=mid+1 tick ===")
    print(f"{'gate':>6} {'n':>5} {'win%':>6} {'avg ask':>8} {'P&L $':>9} {'ROI':>7}")
    for g in gates:
        rows = results[g]
        if not rows:
            print(f"{g:>6} {0:>5}      -        -         -       -")
            continue
        n = len(rows)
        wr = sum(1 for r in rows if r["won"]) / n
        avg_ask = sum(r["ask"] for r in rows) / n
        pnl = sum(r["pnl"] for r in rows)
        roi = pnl / (n * args.stake)
        tag = "  <- band-only baseline (no model)" if g == 0.0 else ""
        print(f"{g:>6} {n:>5} {wr:>6.1%} {avg_ask:>8.3f} {pnl:>+9.2f} {roi:>+7.1%}{tag}")
    print("\nworst 5 losers at gate 0.08:")
    for r in sorted(results.get(0.08, []), key=lambda r: r["pnl"])[:5]:
        print(f"  {r['pnl']:+.2f}  {r['side']:3s} @ {r['ask']:.3f}  {str(r['q'])[:70]}")
    print("\ncaveats: mid+1-tick ask proxy (optimistic), no slippage/queue, one entry "
          "per side/bracket, ignores capital limits, elonmusk only.")


if __name__ == "__main__":
    main()
