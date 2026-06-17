#!/usr/bin/env python3
"""One-off: break down the grinder eligibility funnel on the LIVE board.

Counts how many ≤max_hours markets each filter stage drops, so we can see
the binding constraint before loosening anything. Read-only (Gamma only).
"""
from __future__ import annotations
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv
load_dotenv(REPO / ".env")
from polymarket_bot.profiles import load_profile, apply_profile_to_env
apply_profile_to_env(load_profile(REPO / "configs/profiles/grinder_b.toml"), override=True)

from datetime import timedelta
from polymarket_bot.config import Settings
from polymarket_bot.models import is_excluded_market, parse_dt, as_float, parse_json_list
from polymarket_bot.race_strategies import _load_short_expiry_markets, _build_eligible_candidates
from polymarket_bot.models import utc_now

s = Settings()
markets = _load_short_expiry_markets(s)
now = utc_now()
earliest = now + timedelta(minutes=5)
horizon = now + timedelta(hours=s.race_max_hours)

drop = Counter()
near_band = Counter()  # how many would pass if we widened a single knob
for m in markets:
    if is_excluded_market(m):
        drop["excluded"] += 1; continue
    end_date = parse_dt(m.get("endDate"))
    if end_date is None:
        drop["no_end_date"] += 1; continue
    gs = parse_dt(m.get("gameStartTime"))
    closes_soon = earliest <= end_date <= horizon
    starts_soon = gs is not None and now <= gs <= horizon
    if not (closes_soon or starts_soon):
        drop["outside_window"] += 1; continue
    if not bool(m.get("acceptingOrders")):
        drop["not_accepting"] += 1; continue
    liq = as_float(m.get("liquidity") or m.get("liquidityNum"))
    vol = as_float(m.get("volume24hr") or m.get("volume24hrClob"))
    if liq < s.race_min_liquidity_usd:
        drop["low_liquidity"] += 1; continue
    if vol < s.race_min_volume_24h_usd:
        drop["low_volume"] += 1; continue
    if m.get("bestBid") is None or m.get("bestAsk") is None:
        drop["no_quotes"] += 1; continue
    bb = as_float(m.get("bestBid"), default=None)
    ba = as_float(m.get("bestAsk"), default=None)
    prices = [as_float(x, -1.0) for x in parse_json_list(m.get("outcomePrices"))]
    if bb is None or ba is None or len(prices) != 2:
        drop["bad_quotes"] += 1; continue
    # check each outcome ask against band + spread
    asks = [ba, round(1 - bb, 4)]
    bids = [bb, round(1 - ba, 4)]
    passed = False
    reason = None
    for ask, bid in zip(asks, bids):
        if ask < s.race_min_price or ask > s.race_max_price:
            # how close?
            if s.race_min_price - 0.05 <= ask < s.race_min_price:
                near_band["ask_just_below_min(0.80-0.85)"] += 1
            if s.race_max_price < ask <= s.race_max_price + 0.02:
                near_band["ask_just_above_max"] += 1
            reason = "out_of_band"; continue
        sp = ask - bid
        if sp < 0 or sp > s.race_max_spread:
            reason = "spread"
            if s.race_max_spread < sp <= s.race_max_spread + 0.04:
                near_band["spread_4to8c"] += 1
            continue
        passed = True
    if not passed:
        drop[reason or "out_of_band"] += 1; continue
    drop["ELIGIBLE"] += 1

print(f"raw markets (≤{s.race_max_hours}h): {len(markets)}")
print(f"band=[{s.race_min_price},{s.race_max_price}] spread≤{s.race_max_spread} "
      f"liq≥{s.race_min_liquidity_usd} vol≥{s.race_min_volume_24h_usd}")
print("--- drop reasons ---")
for k, v in drop.most_common():
    print(f"  {v:5d}  {k}")
print("--- near-miss (would pass if widened) ---")
for k, v in near_band.most_common():
    print(f"  {v:5d}  {k}")

# How many DISTINCT games would a lower liq/vol floor add?
from collections import defaultdict
def event_key(m):
    es = str(m.get("events", [{}])[0].get("slug", "") if m.get("events") else "") or str(m.get("slug") or "")
    return es.rsplit("-", 1)[0] if es else str(m.get("id"))

for liq_floor, vol_floor in [(500,300),(300,150),(250,100),(150,50)]:
    events = set()
    n = 0
    for m in markets:
        if is_excluded_market(m): continue
        end_date = parse_dt(m.get("endDate"))
        if end_date is None: continue
        gs = parse_dt(m.get("gameStartTime"))
        if not ((earliest <= end_date <= horizon) or (gs is not None and now <= gs <= horizon)): continue
        if not bool(m.get("acceptingOrders")): continue
        liq = as_float(m.get("liquidity") or m.get("liquidityNum"))
        vol = as_float(m.get("volume24hr") or m.get("volume24hrClob"))
        if liq < liq_floor or vol < vol_floor: continue
        bb = as_float(m.get("bestBid"), default=None); ba = as_float(m.get("bestAsk"), default=None)
        if bb is None or ba is None: continue
        for ask, bid in [(ba, bb), (round(1-bb,4), round(1-ba,4))]:
            if s.race_min_price <= ask <= s.race_max_price and 0 <= ask-bid <= s.race_max_spread:
                n += 1; events.add(event_key(m)); break
    print(f"  liq≥{liq_floor} vol≥{vol_floor}: {n} eligible candidates across {len(events)} distinct events")

elig = _build_eligible_candidates(markets, s)
print(f"\n_build_eligible_candidates -> {len(elig)} candidates")
from collections import defaultdict
by_event = defaultdict(int)
for c, _ in elig:
    by_event[c.event_slug or c.slug] += 1
print(f"distinct events among eligible: {len(by_event)}")
for c, mom in elig:
    print(f"  ask={c.best_ask} bid={c.best_bid} h2c={c.hours_to_close:.1f} liq={c.liquidity:.0f} | {c.question[:55]} [{c.outcome}]")
