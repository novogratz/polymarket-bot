#!/usr/bin/env python3
"""Live profile auto-promoter.

Watches the dry-run leaderboard. When a profile crosses promotion thresholds,
writes ``data/live_active_profile.json``. The live bot reads that file at the
start of each tick and hot-swaps the active profile in-process (no restart).

Hard gates (configurable via env):
- closed_trades >= MIN_CLOSED (default 10)
- roi_pct >= MIN_ROI_PCT (default +5.0)
- win_rate_pct >= MIN_WR_PCT (default 50.0)
- no swap in last COOLDOWN_MINUTES (default 60)
- no more than MAX_SWAPS_PER_DAY (default 4)
- skip if the proposed profile == current active profile

If no profile passes the gates, nothing happens. That is the correct
behavior — promoting a losing profile loses real money. The previous
analyst recommended losing profiles 3 times in a row; this sidecar refuses
to do that.

Run: ``python3 scripts/live_promoter.py`` (loops every CYCLE_SECONDS).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from polymarket_bot.leaderboard import gather_run_stats  # noqa: E402


DATA_DIR = REPO_ROOT / "data"
ACTIVE_PATH = DATA_DIR / "live_active_profile.json"
HISTORY_PATH = DATA_DIR / "live_active_profile_history.jsonl"

CYCLE_SECONDS = int(os.environ.get("PROMOTER_CYCLE_SECONDS", "300"))
MIN_CLOSED = int(os.environ.get("PROMOTER_MIN_CLOSED", "10"))
MIN_ROI_PCT = float(os.environ.get("PROMOTER_MIN_ROI_PCT", "5.0"))
MIN_WR_PCT = float(os.environ.get("PROMOTER_MIN_WR_PCT", "50.0"))
COOLDOWN_MINUTES = int(os.environ.get("PROMOTER_COOLDOWN_MINUTES", "60"))
MAX_SWAPS_PER_DAY = int(os.environ.get("PROMOTER_MAX_SWAPS_PER_DAY", "4"))
DEFAULT_PROFILE = os.environ.get("PROMOTER_DEFAULT_PROFILE", "baseline")


def _list_dry_runs() -> list[str]:
    runs_dir = DATA_DIR / "dry_runs"
    if not runs_dir.is_dir():
        return []
    return sorted(p.name for p in runs_dir.iterdir() if p.is_dir())


def _read_current_active() -> dict | None:
    if not ACTIVE_PATH.is_file():
        return None
    try:
        return json.loads(ACTIVE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _swaps_in_last_24h() -> int:
    if not HISTORY_PATH.is_file():
        return 0
    cutoff = time.time() - 24 * 3600
    count = 0
    for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            ts = float(rec.get("ts", 0))
            if ts >= cutoff:
                count += 1
        except Exception:
            pass
    return count


def _seconds_since_last_swap() -> float:
    if not ACTIVE_PATH.is_file():
        return float("inf")
    return time.time() - ACTIVE_PATH.stat().st_mtime


def _evaluate() -> dict | None:
    """Find the best dry profile that passes promotion gates. None if none qualify."""
    runs = _list_dry_runs()
    qualified: list[tuple[float, str, dict]] = []
    for run_name in runs:
        stats = gather_run_stats(DATA_DIR, run_name)
        if stats is None:
            continue  # stale
        if stats.closed_trades < MIN_CLOSED:
            continue
        if stats.roi_pct < MIN_ROI_PCT:
            continue
        if stats.win_rate_pct < MIN_WR_PCT:
            continue
        # Also require profile TOML exists (live bot loads it by name)
        if not (REPO_ROOT / "configs" / "profiles" / f"{run_name}.toml").is_file():
            continue
        qualified.append(
            (stats.roi_pct, run_name, {
                "profile": run_name,
                "roi_pct": round(stats.roi_pct, 2),
                "closed_trades": stats.closed_trades,
                "win_rate_pct": round(stats.win_rate_pct, 1),
                "equity": round(stats.equity, 2),
            })
        )
    if not qualified:
        return None
    qualified.sort(key=lambda x: x[0], reverse=True)
    return qualified[0][2]


def _write_active(profile: str, reason: str, stats_blob: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    payload = {
        "profile": profile,
        "reason": reason,
        "switched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stats": stats_blob,
    }
    ACTIVE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), **payload}) + "\n")
    print(f"[promoter] WROTE active profile → {profile} (reason: {reason})", flush=True)


def cycle() -> None:
    current = _read_current_active() or {"profile": DEFAULT_PROFILE}
    current_profile = str(current.get("profile") or DEFAULT_PROFILE)

    # Cooldown gate
    elapsed = _seconds_since_last_swap()
    if elapsed < COOLDOWN_MINUTES * 60:
        print(
            f"[promoter] cooldown active ({int(elapsed)}s < {COOLDOWN_MINUTES * 60}s) "
            f"— current: {current_profile}",
            flush=True,
        )
        return

    # Daily swap cap
    swaps_today = _swaps_in_last_24h()
    if swaps_today >= MAX_SWAPS_PER_DAY:
        print(
            f"[promoter] max swaps/day reached ({swaps_today}/{MAX_SWAPS_PER_DAY}) "
            f"— current: {current_profile}",
            flush=True,
        )
        return

    winner = _evaluate()
    if winner is None:
        print(
            f"[promoter] no profile passes gates (≥{MIN_CLOSED} closed, "
            f"ROI≥{MIN_ROI_PCT}%, WR≥{MIN_WR_PCT}%) — current: {current_profile}",
            flush=True,
        )
        return
    if winner["profile"] == current_profile:
        print(
            f"[promoter] best is already active: {current_profile} "
            f"({winner['roi_pct']}% / {winner['closed_trades']}c)",
            flush=True,
        )
        return
    reason = (
        f"ROI {winner['roi_pct']}% on {winner['closed_trades']} closed "
        f"(WR {winner['win_rate_pct']}%), promoted over {current_profile}"
    )
    _write_active(winner["profile"], reason, winner)


def main() -> None:
    print(
        f"[promoter] starting — cycle={CYCLE_SECONDS}s, "
        f"min_closed={MIN_CLOSED}, min_roi={MIN_ROI_PCT}%, "
        f"min_wr={MIN_WR_PCT}%, cooldown={COOLDOWN_MINUTES}min, "
        f"max_swaps/day={MAX_SWAPS_PER_DAY}",
        flush=True,
    )
    while True:
        try:
            cycle()
        except Exception as exc:
            print(f"[promoter] cycle failed: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    main()
