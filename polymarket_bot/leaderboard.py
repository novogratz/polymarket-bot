"""Multi-strategy dry-run leaderboard.

Polls ``data/dry_runs/<run>/`` for each named run, reads the local
ledger + journal + metadata, ranks by ROI, and prints a formatted
scoreboard. Designed to run as a sidecar process alongside the bots
so the strategies themselves stay focused on tick logic.

Each dry-run owns its files:

- ``state.json``     — current cash + open positions + unrealized PnL
- ``journal.jsonl``  — one record per closed trade (realized PnL)
- ``metadata.json``  — starting cash, total ticks, started-at timestamp

The formatter is pure (no I/O) so it's easy to unit-test.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class RunStats:
    """Snapshot of one dry-run's standing."""

    run_name: str
    starting_cash: float
    cash: float
    invested: float
    unrealized_pnl: float
    equity: float
    open_positions: int
    closed_trades: int
    wins: int
    losses: int
    realized_pnl: float
    started_at: str | None
    total_ticks: int

    @property
    def total_pnl(self) -> float:
        return self.equity - self.starting_cash

    @property
    def roi_pct(self) -> float:
        return (self.total_pnl / self.starting_cash * 100.0) if self.starting_cash > 0 else 0.0

    @property
    def win_rate_pct(self) -> float:
        return (self.wins / self.closed_trades * 100.0) if self.closed_trades > 0 else 0.0


def gather_run_stats(base_dir: Path, run_name: str) -> RunStats | None:
    """Read one dry-run directory and compute its standings.

    Returns ``None`` only if the directory doesn't exist; otherwise we
    return defaults so a freshly-spawned run still shows up at the
    bottom of the table (starting_cash → equity, 0 trades).
    """
    root = base_dir / "dry_runs" / run_name
    if not root.is_dir():
        return None

    starting_cash = 100.0
    total_ticks = 0
    started_at: str | None = None
    meta_path = root / "metadata.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            starting_cash = float(meta.get("starting_cash", 100.0))
            total_ticks = int(meta.get("total_ticks", 0))
            started_at = meta.get("started_at")
        except Exception:
            pass

    cash = starting_cash
    positions: list[dict] = []
    state_path = root / "state.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            cash = float(state.get("cash", starting_cash))
            positions = state.get("positions", []) or []
        except Exception:
            pass

    open_positions = [p for p in positions if p.get("status") == "open"]
    invested = sum(float(p.get("stake", 0) or 0) for p in open_positions)
    unrealized = sum(float(p.get("unrealized_pnl", 0) or 0) for p in open_positions)
    equity = cash + invested + unrealized

    closed_trades = 0
    wins = 0
    losses = 0
    realized_pnl = 0.0
    journal_path = root / "journal.jsonl"
    if journal_path.is_file():
        try:
            with journal_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    closed_trades += 1
                    try:
                        pnl = float(rec.get("realized_pnl", 0) or 0)
                    except (TypeError, ValueError):
                        pnl = 0.0
                    realized_pnl += pnl
                    if pnl > 0:
                        wins += 1
                    elif pnl < 0:
                        losses += 1
        except Exception:
            pass

    return RunStats(
        run_name=run_name,
        starting_cash=starting_cash,
        cash=cash,
        invested=invested,
        unrealized_pnl=unrealized,
        equity=equity,
        open_positions=len(open_positions),
        closed_trades=closed_trades,
        wins=wins,
        losses=losses,
        realized_pnl=realized_pnl,
        started_at=started_at,
        total_ticks=total_ticks,
    )


def format_leaderboard(stats: list[RunStats], *, now: datetime | None = None) -> str:
    """Render a ranked text leaderboard. Pure function."""
    if not stats:
        return "🏁 LEADERBOARD: no runs found"

    ranked = sorted(stats, key=lambda s: s.roi_pct, reverse=True)
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%H:%M:%S")

    bar = "═" * 78
    lines: list[str] = [
        bar,
        f"🏁 STRATEGY LEADERBOARD · {stamp} UTC",
        bar,
        f" #  {'STRATEGY':<10} {'EQUITY':>10} {'PnL':>10} {'ROI':>8} "
        f"{'WIN%':>5} {'CLOSED':>7} {'POS':>4} {'TICKS':>6}",
        "─" * 78,
    ]
    for i, s in enumerate(ranked, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, "  ")
        pnl_str = f"{s.total_pnl:+9.2f}"
        roi_str = f"{s.roi_pct:+6.1f}%"
        lines.append(
            f"{medal} {s.run_name:<10} "
            f"${s.equity:>9.2f} "
            f"{pnl_str:>10} "
            f"{roi_str:>8} "
            f"{s.win_rate_pct:>4.0f}% "
            f"{s.closed_trades:>7d} "
            f"{s.open_positions:>4d} "
            f"{s.total_ticks:>6d}"
        )
    lines.append(bar)

    leader = ranked[0]
    if leader.total_pnl > 0:
        lines.append(f"🏆 {leader.run_name} winning by ${leader.total_pnl:+.2f} ({leader.roi_pct:+.1f}%)")
    elif leader.total_pnl == 0 and all(s.total_pnl == 0 for s in stats):
        lines.append("⏸  no movement yet — all flat at starting cash")
    elif leader.total_pnl < 0:
        spread = leader.total_pnl - ranked[-1].total_pnl
        lines.append(
            f"📉 all underwater; least bad: {leader.run_name} ({leader.roi_pct:+.1f}%) — "
            f"spread to last ${spread:+.2f}"
        )
    return "\n".join(lines)


def run_leaderboard_loop(
    base_dir: Path,
    run_names: list[str],
    interval_seconds: int,
) -> None:
    """Print the leaderboard immediately, then every ``interval_seconds``."""
    print(
        f"🏁 leaderboard: tracking {', '.join(run_names)} every {interval_seconds // 60}m "
        f"(reading {base_dir}/dry_runs/)",
        flush=True,
    )
    while True:
        try:
            stats: list[RunStats] = []
            for name in run_names:
                s = gather_run_stats(base_dir, name)
                if s is not None:
                    stats.append(s)
            print("", flush=True)
            print(format_leaderboard(stats), flush=True)
            print("", flush=True)
        except Exception as exc:
            print(f"⚠️  leaderboard error: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(interval_seconds)
