#!/usr/bin/env python3
"""Remove one category from realized history without touching open positions."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
sys.path.insert(0, str(REPO_ROOT))

from polymarket_bot._atomic_io import atomic_write_text
from polymarket_bot.categories import CATEGORIES, classify_category


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--category", required=True, choices=sorted(CATEGORIES))
    parser.add_argument("--baseline", type=float, default=None)
    args = parser.parse_args()

    backup = DATA_DIR / f"backups_category_clean_{int(time.time())}"
    backup.mkdir(parents=True, exist_ok=False)
    removed_total = 0
    for name in ("trade_journal.jsonl", "realized_trade_cache.jsonl"):
        path = DATA_DIR / name
        if not path.exists():
            continue
        shutil.copy2(path, backup / name)
        kept: list[str] = []
        removed = 0
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            category = classify_category(
                str(row.get("question") or ""), str(row.get("slug") or "")
            )
            if category == args.category:
                removed += 1
            else:
                kept.append(json.dumps(row))
        atomic_write_text(path, "\n".join(kept) + ("\n" if kept else ""))
        removed_total += removed
        print(f"{name}: removed {removed}, kept {len(kept)}")

    if args.baseline is not None:
        baseline_path = DATA_DIR / "starting_cash.txt"
        if baseline_path.exists():
            shutil.copy2(baseline_path, backup / "starting_cash.txt")
        atomic_write_text(baseline_path, f"{args.baseline:.2f}")
        print(f"starting baseline: ${args.baseline:.2f}")
    print(f"removed {removed_total} {args.category} records; backup: {backup.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
