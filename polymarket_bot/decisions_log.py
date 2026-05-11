"""Append-only log of entry-stage candidates and their disposition per tick.

A "decision" is a candidate market that survived the upstream filters
(consensus, ROI, spread, etc.) and reached the entry-check stage. For
each candidate we log: market id, outcome (Yes/No), score, decision
(``BUY`` or ``REJECT``), and optional fields (stake if BUY, reason if
REJECT, consensus, copied USDC).

The log is per-tick: one JSON object per line, each line containing a
``tick`` index and a list of candidates. Ticks with no candidates are
not written (no empty entries).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


@dataclass
class Decision:
    market_id: str
    outcome: str
    score: float
    decision: str  # "BUY" | "REJECT"
    stake: float | None = None
    reason: str | None = None
    consensus: int | None = None
    copied_usdc: float | None = None
    avg_copy_price: float | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _serialize_decision(decision: Decision) -> dict:
    return {k: v for k, v in asdict(decision).items() if v is not None}


def append_decisions(path: Path, *, tick: int, candidates: list[Decision]) -> None:
    """Append one JSON line covering one tick's worth of decisions.

    If ``candidates`` is empty, the function is a no-op (we do not log
    "empty" ticks).
    """
    if not candidates:
        return
    entry = {
        "ts": _now_iso(),
        "tick": int(tick),
        "candidates": [_serialize_decision(c) for c in candidates],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def read_decisions(path: Path) -> Iterator[dict]:
    """Yield each tick's record as a dict."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)
