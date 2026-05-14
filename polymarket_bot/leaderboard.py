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

from . import notifications


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
    biggest_win_today: float = 0.0
    biggest_loss_today: float = 0.0

    @property
    def total_pnl(self) -> float:
        return self.equity - self.starting_cash

    @property
    def roi_pct(self) -> float:
        return (self.total_pnl / self.starting_cash * 100.0) if self.starting_cash > 0 else 0.0

    @property
    def win_rate_pct(self) -> float:
        decided = self.wins + self.losses
        return (self.wins / decided * 100.0) if decided > 0 else 0.0


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
    biggest_win_today = 0.0
    biggest_loss_today = 0.0
    today = datetime.now(timezone.utc).date()
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
                    closed_at = rec.get("closed_at")
                    if closed_at:
                        try:
                            closed_dt = datetime.fromisoformat(str(closed_at).replace("Z", "+00:00"))
                        except (ValueError, TypeError):
                            closed_dt = None
                        if closed_dt is not None and closed_dt.date() == today:
                            if pnl > biggest_win_today:
                                biggest_win_today = pnl
                            if pnl < biggest_loss_today:
                                biggest_loss_today = pnl
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
        biggest_win_today=biggest_win_today,
        biggest_loss_today=biggest_loss_today,
    )


def format_leaderboard(stats: list[RunStats], *, now: datetime | None = None) -> str:
    """Render a ranked text leaderboard. Pure function."""
    if not stats:
        return "🏁 LEADERBOARD: no runs found"

    ranked = sorted(stats, key=lambda s: s.roi_pct, reverse=True)
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%H:%M:%S")

    bar = "═" * 92
    lines: list[str] = [
        bar,
        f"🏁 STRATEGY LEADERBOARD · {stamp} UTC",
        bar,
        f" #  {'STRATEGY':<10} {'EQUITY':>10} {'PnL':>10} {'ROI':>8} "
        f"{'WIN%':>5} {'CLOSED':>7} {'POS':>4} "
        f"{'BIG WIN':>9} {'BIG LOSS':>9} {'TICKS':>6}",
        "─" * 92,
    ]
    for i, s in enumerate(ranked, 1):
        medal_emoji = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, "")
        rank_label = f"{i:>2}. {medal_emoji}".rstrip()
        pnl_str = f"{s.total_pnl:+9.2f}"
        roi_str = f"{s.roi_pct:+6.1f}%"
        big_win_str = f"+{s.biggest_win_today:.2f}" if s.biggest_win_today > 0 else "  —  "
        big_loss_str = f"{s.biggest_loss_today:.2f}" if s.biggest_loss_today < 0 else "  —  "
        lines.append(
            f"{rank_label:<6} {s.run_name:<10} "
            f"${s.equity:>9.2f} "
            f"{pnl_str:>10} "
            f"{roi_str:>8} "
            f"{s.win_rate_pct:>4.0f}% "
            f"{s.closed_trades:>7d} "
            f"{s.open_positions:>4d} "
            f"{big_win_str:>9} "
            f"{big_loss_str:>9} "
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


_TELEGRAM_TOP_N = 12


def format_leaderboard_telegram(stats: list[RunStats], *, now: datetime | None = None) -> str:
    """Compact Telegram leaderboard — one line per strategy, top N only.

    Format: ``rank. name ROI%  R±$X  WW/LL``
    Strategies are ranked by realized PnL first (the only signal that
    isn't noise), with ties broken by ROI. Bottom losers compressed to
    one summary line.
    """
    if not stats:
        return "🏁 *Leaderboard*: no runs found"
    # Rank by realized PnL primarily (the metric that matters), ROI as tiebreaker.
    ranked = sorted(stats, key=lambda s: (s.realized_pnl, s.roi_pct), reverse=True)
    now = now or datetime.now(timezone.utc)
    stamp = notifications._md_escape(now.strftime("%H:%M"))

    lines = [f"🏁 *Leaderboard* · {stamp} UTC · top {_TELEGRAM_TOP_N}", ""]
    for i, s in enumerate(ranked[:_TELEGRAM_TOP_N], 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, "  ")
        rank_str = notifications._md_escape(f"{i:>2}.")
        name = notifications._md_escape(_short(s.run_name, 20))
        roi_str = notifications._md_escape(f"{s.roi_pct:+5.1f}%")
        rsign = "+" if s.realized_pnl >= 0 else ""
        real_str = notifications._md_escape(f"R{rsign}${s.realized_pnl:.2f}")
        wl_str = notifications._md_escape(f"{s.wins}W/{s.losses}L")
        lines.append(f"{rank_str} {medal} `{name}` {roi_str}  {real_str}  {wl_str}")

    # Compact summary footer.
    leader = ranked[0]
    if leader.realized_pnl > 0 or leader.total_pnl > 0:
        led_name = notifications._md_escape(_short(leader.run_name, 20))
        led_pnl = notifications._md_escape(f"+${leader.total_pnl:.2f}")
        lines.append("")
        lines.append(f"🏆 *{led_name}* leads \\({led_pnl}\\)")
    elif all(s.total_pnl == 0 for s in stats):
        lines.append("")
        lines.append("⏸ all flat")
    return "\n".join(lines)


def _short(name: str, max_len: int) -> str:
    return name if len(name) <= max_len else name[: max_len - 1] + "…"


def run_leaderboard_loop(
    base_dir: Path,
    run_names: list[str],
    interval_seconds: int,
    *,
    telegram: bool = False,
) -> None:
    """Print the leaderboard immediately, then every ``interval_seconds``.

    When ``telegram=True`` and the Telegram integration is enabled
    (env vars set), each refresh is also posted to Telegram.
    """
    print(
        f"🏁 leaderboard: tracking {', '.join(run_names)} every {interval_seconds // 60}m "
        f"(reading {base_dir}/dry_runs/)"
        + (" + Telegram" if telegram and notifications.is_enabled() else ""),
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
            if telegram and notifications.is_enabled() and stats:
                try:
                    notifications._post(format_leaderboard_telegram(stats))
                except Exception as exc:
                    print(f"⚠️  leaderboard telegram failed: {type(exc).__name__}: {exc}", flush=True)
        except Exception as exc:
            print(f"⚠️  leaderboard error: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(interval_seconds)
