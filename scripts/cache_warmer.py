#!/usr/bin/env python3
"""Pre-warm the smart_money HTTP cache before launching 50+ bots.

Without this, all 50 dry bots launch simultaneously and try to fetch
the same wallet trades — saturating the data-api in the first 30s
and seeing 70%+ failure rate. Running this script first writes the
common payloads to data/cache/http/ so the bot swarm finds them
already populated.

Fetches:
  1. Leaderboards for all (WEEK, MONTH, ALL) × main categories
  2. Top 100 wallets' recent trade history (parallel)

Run once at startup. Re-run if cache TTL expires (default 600s).
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    # Load .env so the bot config + tokens are available
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        try:
            for raw in env_file.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
        except Exception:
            pass

    # Lazy import after env loaded
    from polymarket_bot.config import Settings  # noqa: E402
    from polymarket_bot.smart_money import (  # noqa: E402
        DataApiClient,
        _CACHE_DIR,
        _CACHE_TTL,
    )

    settings = Settings()
    base = getattr(settings, "smart_money_api_base_url", None) or "https://data-api.polymarket.com"
    client = DataApiClient(base_url=base, timeout=15)

    print(f"[cache_warmer] base_url={base} ttl={_CACHE_TTL}s dir={_CACHE_DIR}")
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Step 1: leaderboards. Cover the windows + categories the bots will request.
    windows = ["WEEK", "MONTH", "ALL"]
    categories = ["OVERALL", "SPORTS", "POLITICS", "FINANCE", "CULTURE", "ECONOMICS", "WEATHER", "TECH"]
    limits = [50, 100, 150, 30]  # common top_n values across the curated profiles

    wallets: set[str] = set()
    leaderboard_calls = 0
    leaderboard_ok = 0
    t0 = time.time()
    for window in windows:
        for cat in categories:
            for limit in limits:
                leaderboard_calls += 1
                try:
                    traders = client.leaderboard(
                        category=cat, time_period=window, limit=limit
                    )
                    leaderboard_ok += 1
                    for t in traders:
                        if t.wallet:
                            wallets.add(t.wallet)
                except Exception as exc:
                    print(f"  leaderboard {window}/{cat}/{limit}: {type(exc).__name__}")
                time.sleep(0.1)  # gentle throttle to avoid burst
    print(f"[cache_warmer] leaderboards: {leaderboard_ok}/{leaderboard_calls} ok in {time.time()-t0:.1f}s — collected {len(wallets)} unique wallets")

    # Step 2: wallet trade histories. Run in parallel batches.
    print(f"[cache_warmer] fetching trade history for {len(wallets)} wallets...")
    start_iso = None  # default — let the API use its own window
    t1 = time.time()
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(client.trades, user=w, start=start_iso) for w in wallets]
        for f in as_completed(futures):
            try:
                f.result()
                ok += 1
            except Exception:
                fail += 1
    print(f"[cache_warmer] wallet trades: {ok}/{ok+fail} ok in {time.time()-t1:.1f}s")

    elapsed = time.time() - t0
    print(f"[cache_warmer] done — total {elapsed:.1f}s. Cache populated at {_CACHE_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
