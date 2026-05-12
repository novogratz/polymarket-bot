"""Append-only equity curve writer.

Each tick of the dry-run loop appends one JSON line: timestamp, tick
index, cash, invested (cost-basis of open positions), unrealized PnL,
and the derived equity (cash + invested + unrealized).

The file lives at ``data/dry_runs/<run>/equity_curve.jsonl``. The
caller is responsible for picking the path; this module only knows
how to append and read.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_equity_point(
    path: Path,
    *,
    tick: int,
    cash: float,
    invested: float,
    unrealized: float,
) -> None:
    """Append a single equity point to ``path`` (one JSON object per line)."""
    point = {
        "ts": _now_iso(),
        "tick": int(tick),
        "cash": float(cash),
        "invested": float(invested),
        "unrealized": float(unrealized),
        "equity": float(cash) + float(invested) + float(unrealized),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(point) + "\n")


def read_equity_curve(path: Path) -> list[dict]:
    """Read all points back. Returns ``[]`` if the file does not exist."""
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
