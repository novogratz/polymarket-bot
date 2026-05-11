"""Compute and format side-by-side statistics for dry-run runs.

A "RunStats" bundles the headline numbers an operator wants when
comparing two or more named runs: starting cash, current equity, total
ticks, realized P&L, win rate, max drawdown. Stats are derived from
the run's metadata.json, state.json, journal.jsonl, and equity_curve.jsonl.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from polymarket_bot.dry_run_runs import DryRunPaths, load_metadata


@dataclass(frozen=True)
class RunStats:
    run_name: str
    profile_source: str
    starting_cash: float
    cash: float
    invested: float
    unrealized: float
    equity: float
    return_pct: float
    total_ticks: int
    started_at: str
    realized_pnl: float
    trades_closed: int
    win_rate: float
    max_drawdown: float
    avg_pnl: float
    open_positions: int = 0


def _read_journal_stats(journal_path: Path) -> dict:
    if not journal_path.is_file():
        return {"realized_pnl": 0.0, "trades_closed": 0, "win_rate": 0.0, "max_drawdown": 0.0, "avg_pnl": 0.0}
    trades = [json.loads(l) for l in journal_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not trades:
        return {"realized_pnl": 0.0, "trades_closed": 0, "win_rate": 0.0, "max_drawdown": 0.0, "avg_pnl": 0.0}
    pnls = [float(t.get("realized_pnl", 0.0)) for t in trades]
    total = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    return {
        "realized_pnl": total,
        "trades_closed": len(trades),
        "win_rate": wins / len(trades) if trades else 0.0,
        "max_drawdown": min(pnls),
        "avg_pnl": total / len(trades) if trades else 0.0,
    }


def compute_run_stats(base_dir: Path, run_name: str) -> RunStats:
    paths = DryRunPaths.for_run(base_dir, run_name)
    metadata = load_metadata(paths)

    cash = float(metadata.starting_cash)
    invested = 0.0
    unrealized = 0.0
    open_positions = 0
    if paths.state.is_file():
        state = json.loads(paths.state.read_text(encoding="utf-8"))
        cash = float(state.get("cash", cash))
        positions = state.get("positions", [])
        for p in positions:
            stake = float(p.get("stake", 0.0))
            if stake <= 0 or p.get("status") not in (None, "open"):
                continue
            invested += stake
            unrealized += float(p.get("unrealized_pnl", 0.0))
            open_positions += 1

    equity = cash + invested + unrealized
    return_pct = (equity - metadata.starting_cash) / metadata.starting_cash if metadata.starting_cash else 0.0

    j = _read_journal_stats(paths.journal)

    return RunStats(
        run_name=metadata.run_name,
        profile_source=metadata.profile_source,
        starting_cash=metadata.starting_cash,
        cash=round(cash, 2),
        invested=round(invested, 2),
        unrealized=round(unrealized, 2),
        equity=round(equity, 2),
        return_pct=round(return_pct, 4),
        total_ticks=metadata.total_ticks,
        started_at=metadata.started_at,
        realized_pnl=round(j["realized_pnl"], 2),
        trades_closed=j["trades_closed"],
        win_rate=round(j["win_rate"], 3),
        max_drawdown=round(j["max_drawdown"], 2),
        avg_pnl=round(j["avg_pnl"], 2),
        open_positions=open_positions,
    )


def format_comparison_table(stats_list: list[RunStats]) -> str:
    """Render a fixed-width table comparing several runs side by side."""
    if not stats_list:
        return "(no runs)"

    rows: list[tuple[str, list[str]]] = [
        ("Profile",       [s.profile_source for s in stats_list]),
        ("Starting cash", [f"{s.starting_cash:.2f}$" for s in stats_list]),
        ("Cash now",      [f"{s.cash:.2f}$" for s in stats_list]),
        ("Invested",      [f"{s.invested:.2f}$" for s in stats_list]),
        ("Open pos.",     [str(s.open_positions) for s in stats_list]),
        ("Unrealized",    [f"{s.unrealized:+.2f}$" for s in stats_list]),
        ("Equity",        [f"{s.equity:.2f}$" for s in stats_list]),
        ("Return",        [f"{s.return_pct * 100:+.2f}%" for s in stats_list]),
        ("Ticks",         [str(s.total_ticks) for s in stats_list]),
        ("",              ["" for _ in stats_list]),
        ("P&L réalisé",   [f"{s.realized_pnl:+.2f}" for s in stats_list]),
        ("Trades clos",   [str(s.trades_closed) for s in stats_list]),
        ("Win rate",      [f"{s.win_rate * 100:.0f}%" for s in stats_list]),
        ("Avg PnL/trade", [f"{s.avg_pnl:+.2f}" for s in stats_list]),
        ("Max drawdown",  [f"{s.max_drawdown:+.2f}" for s in stats_list]),
    ]

    label_width = max(len(label) for label, _ in rows) + 2
    col_width = max(
        max((len(v) for v in vals), default=0)
        for _, vals in rows
    )
    col_width = max(col_width, max(len(s.run_name) for s in stats_list))
    col_width += 4

    header = " " * label_width + "".join(s.run_name.ljust(col_width) for s in stats_list)
    lines = [header, "-" * len(header)]
    for label, values in rows:
        line = label.ljust(label_width) + "".join(v.ljust(col_width) for v in values)
        lines.append(line.rstrip())
    return "\n".join(lines)
