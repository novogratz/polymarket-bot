"""Atomic write helper.

`Path.write_text` is not atomic: a crash, kill -9, or power loss
mid-write leaves a truncated file. For state that the bot reads back
at startup (ledger, tick state, mirror state, auto-tuner overrides),
truncation is data loss.

`atomic_write_text` writes to a sibling tempfile in the same
directory then `os.replace` swaps the target. The replace is
atomic on POSIX and Windows-compatible (same filesystem). If the
write fails partway, the tempfile is cleaned up and the target is
left untouched.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(content)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # On Windows, os.replace can fail with [WinError 5] Access is denied when
    # another process (e.g. antivirus) briefly holds the file. Retry with backoff.
    last_exc: Exception | None = None
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:
            last_exc = exc
            time.sleep(0.05 * (attempt + 1))
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise last_exc  # type: ignore[misc]
