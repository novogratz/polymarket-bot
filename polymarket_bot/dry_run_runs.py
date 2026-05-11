"""Lifecycle of a named dry-run simulation.

A run is a directory under ``data/dry_runs/<name>/`` that holds the
ledger, journal, tick history, equity curve, decisions log, frozen
config snapshot and metadata. This module owns the directory layout
and the ``metadata.json`` schema. It does NOT know about the trading
logic — other modules (``equity_tracker``, ``decisions_log``,
``portfolio``) write into the directory it provisions.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path


_RUN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class DryRunPaths:
    """Filesystem layout of a single named dry-run."""

    root: Path
    metadata: Path
    state: Path
    journal: Path
    tick_state: Path
    tick_history: Path
    overrides: Path
    config_snapshot: Path
    equity_curve: Path
    decisions: Path

    @classmethod
    def for_run(cls, base_dir: Path, run_name: str) -> "DryRunPaths":
        root = base_dir / "dry_runs" / run_name
        return cls(
            root=root,
            metadata=root / "metadata.json",
            state=root / "state.json",
            journal=root / "journal.jsonl",
            tick_state=root / "last_tick.json",
            tick_history=root / "tick_history.jsonl",
            overrides=root / "overrides.json",
            config_snapshot=root / "config_snapshot.toml",
            equity_curve=root / "equity_curve.jsonl",
            decisions=root / "decisions.jsonl",
        )


@dataclass
class RunMetadata:
    """Persisted metadata about a dry-run.

    Saved as JSON. Mutable so we can update last_tick_at / total_ticks
    on every tick without recreating the dataclass.
    """

    run_name: str
    mode: str  # "dry-run"
    starting_cash: float
    profile_source: str
    started_at: str
    last_tick_at: str | None = None
    total_ticks: int = 0
    git_sha: str | None = None
    code_version: str | None = None


def _validate_run_name(name: str) -> None:
    if not name or not _RUN_NAME_RE.match(name):
        raise ValueError(
            f"invalid run name {name!r}: must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}"
        )


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=False, timeout=2.0,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_run_directory(
    base_dir: Path,
    run_name: str,
    *,
    starting_cash: float,
    profile_source: str,
) -> DryRunPaths:
    """Create ``data/dry_runs/<run_name>/`` (idempotent), write metadata
    if absent, return the :class:`DryRunPaths`.

    If the run directory already exists with a metadata file, the
    existing metadata is preserved as-is (does NOT overwrite). The
    caller can compare ``starting_cash``/``profile_source`` against the
    stored values if they want to warn about drift.
    """
    _validate_run_name(run_name)
    paths = DryRunPaths.for_run(base_dir, run_name)
    paths.root.mkdir(parents=True, exist_ok=True)
    if not paths.metadata.is_file():
        metadata = RunMetadata(
            run_name=run_name,
            mode="dry-run",
            starting_cash=float(starting_cash),
            profile_source=profile_source,
            started_at=_now_iso(),
            git_sha=_git_sha(),
        )
        save_metadata(paths, metadata)
    return paths


def load_metadata(paths: DryRunPaths) -> RunMetadata:
    raw = json.loads(paths.metadata.read_text(encoding="utf-8"))
    return RunMetadata(**raw)


def save_metadata(paths: DryRunPaths, metadata: RunMetadata) -> None:
    paths.metadata.write_text(
        json.dumps(asdict(metadata), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def update_tick_metadata(paths: DryRunPaths) -> None:
    """Increment ``total_ticks`` and refresh ``last_tick_at``."""
    metadata = load_metadata(paths)
    metadata.total_ticks += 1
    metadata.last_tick_at = _now_iso()
    save_metadata(paths, metadata)
