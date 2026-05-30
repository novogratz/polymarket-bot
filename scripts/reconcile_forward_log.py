#!/usr/bin/env python
"""Reconcile the forward-test log against resolution → measured grinder edge.

Read-only. Reads ``data/forward_eligible_log.jsonl`` (written by the race
tick's ``_log_forward_observations``), then for each observed token fetches
its CLOB price history and reads the final traded price to decide the
outcome: last price ≥ 0.90 → win, ≤ 0.10 → loss, otherwise still
open/ambiguous (skipped). Tokens the bot actually observed had volume, so
their history is far less sparse than the random closed-market sample.

Then sweeps (price band, hours-to-close, momentum floor) over the resolved
observations and reports win rate vs the priced-in probability (the entry
ask), the edge, and EV per trade — i.e. whether the markets the strategy
*actually meets* are mispriced in our favour, and which filters help.

Usage:
  uv run python scripts/reconcile_forward_log.py
  uv run python scripts/reconcile_forward_log.py --log data/forward_eligible_log.jsonl
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CLOB_BASE = "https://clob.polymarket.com"


def load_observations(path: Path) -> list[dict]:
    """First record per token_id (the entry the bot first saw in-band)."""
    by_token: dict[str, dict] = {}
    if not path.is_file():
        return []
    with path.open() as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            tok = str(rec.get("token_id") or "")
            if tok and tok not in by_token:
                by_token[tok] = rec
    return list(by_token.values())


def final_price(token_id: str) -> float | None:
    """Last traded price for a token (resolution proxy), or None if no data."""
    query = urllib.parse.urlencode({"market": token_id, "interval": "max", "fidelity": 60})
    req = urllib.request.Request(
        f"{CLOB_BASE}/prices-history?{query}",
        headers={"Accept": "application/json", "User-Agent": "polymarket-bot/reconcile"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    history = payload.get("history") if isinstance(payload, dict) else None
    if not isinstance(history, list) or not history:
        return None
    try:
        return float(history[-1]["p"])
    except (TypeError, ValueError, KeyError):
        return None


def resolve(obs: list[dict], sleep: float) -> list[dict]:
    """Attach won=0/1 to each observation that has cleanly resolved."""
    resolved: list[dict] = []
    n = len(obs)
    for i, rec in enumerate(obs, 1):
        fp = final_price(str(rec["token_id"]))
        if sleep:
            time.sleep(sleep)
        if fp is None:
            continue
        if fp >= 0.90:
            rec = {**rec, "won": 1}
        elif fp <= 0.10:
            rec = {**rec, "won": 0}
        else:
            continue  # still open / ambiguous
        resolved.append(rec)
        if i % 25 == 0:
            print(f"  …resolved {i}/{n}", flush=True)
    return resolved


def stats(rows: list[dict], lo: float, hi: float, hours: float, min_mom: float) -> dict:
    sel = [
        r
        for r in rows
        if lo <= float(r.get("best_ask") or 0) <= hi
        and float(r.get("hours_to_close") or 1e9) <= hours
        and (float(r.get("one_day_change") or 0.0) if r.get("outcome_index") == 0
             else -float(r.get("one_day_change") or 0.0)) >= min_mom
    ]
    n = len(sel)
    if n == 0:
        return {"n": 0}
    wins = sum(int(r["won"]) for r in sel)
    avg_entry = sum(float(r["best_ask"]) for r in sel) / n
    ev = sum(((int(r["won"]) - float(r["best_ask"])) / float(r["best_ask"])) for r in sel) / n
    return {
        "n": n,
        "win_rate": wins / n,
        "avg_entry": avg_entry,
        "edge": (wins / n) - avg_entry,
        "ev_per_trade_pct": ev * 100.0,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", default=os.getenv("POLYMARKET_FORWARD_LOG_PATH", "data/forward_eligible_log.jsonl"))
    ap.add_argument("--sleep", type=float, default=0.03)
    args = ap.parse_args()

    path = Path(args.log)
    obs = load_observations(path)
    print(f"Loaded {len(obs)} unique observed tokens from {path}", flush=True)
    if not obs:
        print("No observations yet. Let the bot run — the race tick logs them each tick.")
        return

    print("Fetching resolutions…", flush=True)
    rows = resolve(obs, args.sleep)
    print(f"  {len(rows)} cleanly resolved (of {len(obs)} observed)\n", flush=True)
    if not rows:
        print("Nothing resolved yet — observed markets may still be open. Re-run later.")
        return

    combos = [
        (lo, hi, h, m)
        for (lo, hi) in [(0.80, 0.85), (0.85, 0.90), (0.90, 0.94), (0.94, 0.99), (0.80, 0.99)]
        for h in (1.0, 3.0)
        for m in (-1.0, -0.05)  # -1.0 = no momentum filter; -0.05 = live filter
    ]
    print(f"{'band':>11} {'hrs':>4} {'mom':>6} {'n':>5} {'win%':>6} {'avg_entry':>10} {'edge':>7} {'EV/trade':>9}")
    print("-" * 70)
    best = None
    for lo, hi, h, m in combos:
        r = stats(rows, lo, hi, h, m)
        if r["n"] == 0:
            continue
        flag = "no-mom" if m <= -1.0 else "mom≥-5%"
        print(
            f"{lo:.2f}-{hi:.2f} {h:>4.0f} {flag:>6} {r['n']:>5} {r['win_rate']*100:>5.1f}% "
            f"{r['avg_entry']:>10.3f} {r['edge']:>+7.3f} {r['ev_per_trade_pct']:>+8.2f}%"
        )
        if r["n"] >= 20 and (best is None or r["ev_per_trade_pct"] > best[1]):
            best = ((lo, hi, h, m), r["ev_per_trade_pct"], r)
    print("-" * 70)
    if best:
        (lo, hi, h, m), ev, r = best
        verdict = "REAL EDGE ✅" if ev > 0.5 else ("MARGINAL ⚠️" if ev > -0.5 else "NEGATIVE — bleed ❌")
        print(
            f"Best (n≥20): band {lo:.2f}-{hi:.2f}, {h:.0f}h → EV {ev:+.2f}%/trade, "
            f"win {r['win_rate']*100:.1f}% vs priced {r['avg_entry']*100:.1f}% (n={r['n']}). {verdict}"
        )
    else:
        print("No configuration has n≥20 yet. Keep the bot running to grow the sample.")


if __name__ == "__main__":
    main()
