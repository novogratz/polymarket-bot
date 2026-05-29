#!/usr/bin/env python3
"""Daily Quant Summary — Polymarket Bot kzer_ai.

Standalone entry point for the daily report. Delegates to
live_analyst.daily_report_once() so the format stays in sync.

Usage:
  uv run python scripts/daily_report.py           # post to Telegram
  uv run python scripts/daily_report.py --dry-run # print only
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.live_analyst as _analyst


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily quant summary")
    parser.add_argument("--dry-run", action="store_true", help="Print only, skip Telegram")
    args = parser.parse_args()

    _analyst._load_dotenv()

    if args.dry_run:
        # Silence Telegram; daily_report_once still prints to stdout
        _analyst.telegram_post = lambda text, **kw: None

    _analyst.daily_report_once()


if __name__ == "__main__":
    main()
