#!/usr/bin/env python3
"""Strategy consistency analyzer — slices journal data into time windows
and ranks strategies per window. A strategy that ranks top-N in most
windows is statistically consistent; one that bounces in/out is variance.

Reads data/dry_runs/*/journal.jsonl, no API calls, no new bots. Runs
on whatever sample the race has accumulated.

CLI:
    python3 scripts/winner_consistency.py            # default 30-min windows
    python3 scripts/winner_consistency.py --window 60 --top 10
    python3 scripts/winner_consistency.py --telegram # post to dry-run channel
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DRY_RUNS = REPO_ROOT / "data" / "dry_runs"


def _load_dotenv() -> None:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
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


_load_dotenv()


def load_closes() -> dict[str, list[tuple[datetime, float]]]:
    """Return {strategy_name: [(closed_at, realized_pnl), ...]}."""
    out: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    if not DRY_RUNS.exists():
        return out
    for d in DRY_RUNS.iterdir():
        if not d.is_dir():
            continue
        j = d / "journal.jsonl"
        if not j.exists():
            continue
        try:
            for line in j.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not (e.get("event") == "position_closed" or e.get("closed_at")):
                    continue
                ts_raw = e.get("closed_at")
                if not ts_raw:
                    continue
                try:
                    ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                except Exception:
                    continue
                pnl = float(
                    e.get("realized_pnl_usd") or e.get("realized_pnl") or 0.0
                )
                out[d.name].append((ts, pnl))
        except Exception:
            continue
    return out


def slice_into_windows(
    closes: dict[str, list[tuple[datetime, float]]],
    window_minutes: int,
    lookback_hours: int,
) -> tuple[list[tuple[datetime, datetime]], dict[str, list[float]]]:
    """Compute PnL per strategy per window over the last lookback_hours.

    Returns (windows, pnl_by_strategy) where pnl_by_strategy[s][i] is
    the realized PnL of strategy s in window i.
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=lookback_hours)
    windows: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < now:
        end = cursor + timedelta(minutes=window_minutes)
        windows.append((cursor, min(end, now)))
        cursor = end

    pnl: dict[str, list[float]] = {}
    for name, events in closes.items():
        row = [0.0] * len(windows)
        for ts, p in events:
            if ts < start or ts > now:
                continue
            for i, (a, b) in enumerate(windows):
                if a <= ts < b:
                    row[i] += p
                    break
        pnl[name] = row
    return windows, pnl


def consistency_table(
    pnl: dict[str, list[float]],
    top_n: int,
) -> list[dict]:
    """For each strategy, count how many windows it ranked top_n by PnL.

    Strategies with no closed trades in any window are skipped.
    """
    n_windows = max((len(v) for v in pnl.values()), default=0)
    if n_windows == 0:
        return []
    # Per-window rankings (descending PnL, skip 0-PnL ones to avoid
    # arbitrary tie-breaks crowding the top with idle strategies)
    in_top_count: dict[str, int] = defaultdict(int)
    appeared_count: dict[str, int] = defaultdict(int)
    for i in range(n_windows):
        ranked = sorted(
            ((name, row[i]) for name, row in pnl.items() if abs(row[i]) > 0.005),
            key=lambda x: x[1],
            reverse=True,
        )
        for name, _ in ranked:
            appeared_count[name] += 1
        for name, _ in ranked[:top_n]:
            in_top_count[name] += 1
    out = []
    for name in pnl:
        appearances = appeared_count.get(name, 0)
        tops = in_top_count.get(name, 0)
        total_pnl = sum(pnl[name])
        if appearances == 0:
            continue
        out.append({
            "name": name,
            "windows_in_top": tops,
            "windows_active": appearances,
            "total_windows": n_windows,
            "top_rate_pct": (tops / appearances * 100) if appearances else 0,
            "total_pnl": total_pnl,
        })
    out.sort(key=lambda r: (-r["windows_in_top"], -r["total_pnl"]))
    return out


def format_report(
    rows: list[dict],
    windows: list,
    window_minutes: int,
    top_n: int,
    limit: int = 20,
) -> str:
    n_w = len(windows)
    if not rows:
        return (f"📊 *Consistency Analyzer*\n"
                f"No closed trades in last {n_w * window_minutes // 60}h.")
    lines = [
        f"📊 Consistency Analyzer · {window_minutes}min windows · {n_w} windows = ~{n_w * window_minutes // 60}h lookback",
        f"Ranking: how many active windows was each strategy in the top {top_n}?",
        "",
        f"{'strategy':<40s} {'top/active':>11s} {'rate':>6s} {'total PnL':>10s}",
        "-" * 72,
    ]
    for r in rows[:limit]:
        marker = "🏆" if r["windows_in_top"] >= n_w // 2 else "  "
        lines.append(
            f"{marker} {r['name']:<37s} "
            f"{r['windows_in_top']:>4d}/{r['windows_active']:<4d}  "
            f"{r['top_rate_pct']:>5.0f}%  "
            f"${r['total_pnl']:>+9.2f}"
        )
    lines.append("")
    lines.append("🏆 = top-N in ≥half of active windows (statistically consistent)")
    return "\n".join(lines)


def telegram_post(text: str) -> bool:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID_DRY_RUN") or "").strip()
    if not token or not chat:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat,
        "text": text[:4000],
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        print(f"[consistency] telegram failed: {exc}", file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window", type=int, default=30,
                    help="Window size in minutes (default 30)")
    ap.add_argument("--lookback", type=int, default=8,
                    help="Lookback in hours (default 8)")
    ap.add_argument("--top", type=int, default=10,
                    help="Rank within top N per window (default 10)")
    ap.add_argument("--limit", type=int, default=20,
                    help="Strategies to print (default 20)")
    ap.add_argument("--telegram", action="store_true",
                    help="Post result to TELEGRAM_CHAT_ID_DRY_RUN")
    args = ap.parse_args()

    closes = load_closes()
    if not closes:
        print("No journals found in data/dry_runs/. Has the race been running?")
        return 0
    windows, pnl = slice_into_windows(
        closes, window_minutes=args.window, lookback_hours=args.lookback
    )
    rows = consistency_table(pnl, top_n=args.top)
    report = format_report(rows, windows, args.window, args.top, args.limit)
    print(report)
    if args.telegram:
        if telegram_post(report):
            print("\n[consistency] posted to Telegram dry-run channel", file=sys.stderr)
        else:
            print("\n[consistency] Telegram disabled or post failed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
