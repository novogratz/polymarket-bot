"""Best-effort persistence of per-tick execution state for the dashboard.

Each successful tick of ``strategy_loop`` records a compact summary
(``tick_id``, scan counts, actions taken, tuner overrides, timestamps) so
the dashboard's Live tab can show what the bot is doing right now and
what it did over the last few ticks. Two files are maintained:

- ``data/last_tick.json`` — most recent tick only, atomically rewritten.
- ``data/tick_history.jsonl`` — append-only log capped at 200 lines so the
  file stays small without rotating.

In dry-run mode the equivalent ``data/dry_run_*`` paths are used (handled
by ``Settings.__post_init__``). All write operations are best-effort: any
exception is swallowed so a failure here cannot break the trading loop.
"""

from __future__ import annotations

import json
from typing import Any

from ._atomic_io import atomic_write_text
from .config import Settings

_HISTORY_CAP = 200


def write_tick(settings: Settings, record: dict[str, Any]) -> None:
    """Persist ``record`` as the latest tick and append to the history log.

    Best-effort: silently swallows any I/O or encoding error.
    """
    try:
        last_path = settings.tick_state_path
        history_path = settings.tick_history_path
        encoded = json.dumps(record)
        atomic_write_text(last_path, encoded)

        existing: list[str] = []
        if history_path.exists():
            existing = [line for line in history_path.read_text().splitlines() if line.strip()]
        existing.append(encoded)
        if len(existing) > _HISTORY_CAP:
            existing = existing[-_HISTORY_CAP:]
        atomic_write_text(history_path, "\n".join(existing) + "\n")
    except Exception:
        return


def read_last_tick(settings: Settings) -> dict[str, Any] | None:
    """Return the latest tick record, or None if the file is missing/corrupt."""
    path = settings.tick_state_path
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def read_tick_history(settings: Settings, limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent ``limit`` ticks, newest first.

    Skips any line that fails to parse rather than aborting the read.
    """
    path = settings.tick_history_path
    if not path.exists():
        return []
    try:
        lines = path.read_text().splitlines()
    except Exception:
        return []
    parsed: list[dict[str, Any]] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            parsed.append(json.loads(line))
        except Exception:
            continue
        if len(parsed) >= limit:
            break
    return parsed
