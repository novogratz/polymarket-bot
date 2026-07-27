#!/usr/bin/env python3
"""Cross-window consistency logger — read-only sidecar, no trading.

The 2-day, weekly, and monthly counting windows of one account all derive
from the SAME posting process, so their prices must be mutually consistent.
This sidecar snapshots, every cycle, EVERY bracket of every active window of
every tracked account: market mid vs model probability, hours left, current
count. Appends JSONL to data/tweet_consistency.jsonl.

Purpose (user 2026-07-27): measure discrepancy frequency/size BEFORE building
a relative-value trading path. Analyze offline:
  - persistent |model - mid| gaps per window age (is the mid-window edge real
    live, not just in snapshots?);
  - cross-window disagreement: two windows implying incompatible posting
    rates at the same instant.

Deterministic, stdlib-only, NO LLM, never trades. Toggle with
TWEET_CONSISTENCY_LOG=0; cycle via TWEET_CONSISTENCY_INTERVAL_SECONDS (1800).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from polymarket_bot.tweet_model import (  # noqa: E402
    _users_cached,
    _bucket,
    parse_tweet_question,
    tweet_outcome_probability,
)

GAMMA = "https://gamma-api.polymarket.com"
OUT_PATH = os.environ.get("TWEET_CONSISTENCY_PATH", "data/tweet_consistency.jsonl")
INTERVAL_S = int(os.environ.get("TWEET_CONSISTENCY_INTERVAL_SECONDS", "1800"))


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-bot/consistency"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8"))


def snapshot_once() -> int:
    now = datetime.now(timezone.utc)
    rows = []
    for user in _users_cached(_bucket()):
        handle = str(user.get("handle") or "")
        for tracking in user.get("trackings") or ():
            if not tracking.get("isActive"):
                continue
            link = str(tracking.get("marketLink") or "").rstrip("/")
            slug = link.rsplit("/", 1)[-1] if "/" in link else ""
            if not slug:
                continue
            try:
                ev = _get(f"{GAMMA}/events?{urllib.parse.urlencode({'slug': slug})}")
            except Exception:
                continue
            if not ev:
                continue
            for mkt in ev[0].get("markets") or ():
                if mkt.get("closed"):
                    continue
                question = str(mkt.get("question") or "")
                parsed = parse_tweet_question(question)
                if parsed is None:
                    continue
                try:
                    prices = json.loads(mkt.get("outcomePrices") or "[]")
                    mid_yes = float(prices[0])
                except (ValueError, IndexError, TypeError):
                    continue
                model_yes = tweet_outcome_probability(parsed, "Yes")
                if model_yes is None:
                    continue
                rows.append({
                    "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "handle": handle,
                    "window": slug,
                    "window_end": tracking.get("endDate"),
                    "question": question,
                    "lo": parsed["lo"], "hi": parsed["hi"],
                    "mid_yes": mid_yes,
                    "model_yes": round(model_yes, 4),
                    "gap": round(model_yes - mid_yes, 4),
                })
    if rows:
        os.makedirs(os.path.dirname(OUT_PATH) or ".", exist_ok=True)
        with open(OUT_PATH, "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    return len(rows)


def main() -> None:
    print(f"[consistency] sidecar up: every {INTERVAL_S}s -> {OUT_PATH}", flush=True)
    while True:
        try:
            n = snapshot_once()
            print(f"[consistency] snapshot: {n} brackets logged", flush=True)
        except Exception as exc:  # never dies
            print(f"[consistency] cycle error (ignored): {exc}", flush=True)
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
