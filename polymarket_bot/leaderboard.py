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


_HISTORY_PATH_DEFAULT = Path("data/leaderboard_history.json")


def _load_history(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_history(path: Path, payload: dict[str, dict]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"⚠️  leaderboard history save failed: {exc}", flush=True)


def _last_decisive_trade_since(
    base_dir: Path, run_name: str, since_ts: str | None
) -> dict | None:
    """Return the largest |realized_pnl| journal entry since ``since_ts``."""
    jp = base_dir / "dry_runs" / run_name / "journal.jsonl"
    if not jp.is_file():
        return None
    best: dict | None = None
    best_abs = 0.0
    try:
        with jp.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = rec.get("closed_at")
                if since_ts and ts and str(ts) <= since_ts:
                    continue
                pnl = float(rec.get("realized_pnl", 0) or 0)
                if abs(pnl) > best_abs:
                    best_abs = abs(pnl)
                    best = rec
    except Exception:
        return None
    return best


def _format_trade_blurb(rec: dict) -> str:
    """Render a journal entry as a short MarkdownV2-escaped blurb."""
    q = str(rec.get("question") or "?")[:35]
    pnl = float(rec.get("realized_pnl", 0) or 0)
    pct = rec.get("pnl_pct")
    sign = "+" if pnl >= 0 else ""
    pct_str = f" ({sign}{float(pct) * 100:.0f}%)" if pct is not None else ""
    body = f"'{q}' {sign}${pnl:.2f}{pct_str}"
    return notifications._md_escape(body)


def format_leaderboard_telegram(
    stats: list[RunStats],
    *,
    now: datetime | None = None,
    history: dict[str, dict] | None = None,
    base_dir: Path | None = None,
) -> str:
    """Compact Telegram leaderboard — ranked by equity, with movers section.

    Format per strategy: ``rank. medal name 🟢/🔴 ROI%  WW/LL``
    If ``history`` and ``base_dir`` are provided, a "since last refresh"
    section flags the top climbers and droppers along with the trade
    that caused the move.
    """
    if not stats:
        return "🏁 *Leaderboard*: no runs found"
    ranked = sorted(stats, key=lambda s: s.roi_pct, reverse=True)
    now = now or datetime.now(timezone.utc)
    stamp = notifications._md_escape(now.strftime("%H:%M"))

    lines = [f"🏁 *Leaderboard* · {stamp} UTC · {len(ranked)} strategies", ""]
    cur_ranks: dict[str, int] = {}
    for i, s in enumerate(ranked, 1):
        cur_ranks[s.run_name] = i
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, "  ")
        rank_str = notifications._md_escape(f"{i:>2}.")
        name = notifications._md_escape(s.run_name)
        if s.roi_pct > 0:
            color = "🟢"
        elif s.roi_pct < 0:
            color = "🔴"
        else:
            color = "⚪"
        roi_str = notifications._md_escape(f"{s.roi_pct:+5.1f}%")
        wl_str = notifications._md_escape(f"{s.wins}W/{s.losses}L")
        lines.append(f"{rank_str} {medal} `{name}` {color} {roi_str}  {wl_str}")

    # Movers section: only meaningful if we have prior state.
    if history and base_dir is not None:
        deltas: list[tuple[str, int, int, int]] = []  # (name, prev, cur, delta)
        for s in ranked:
            prev = history.get(s.run_name, {})
            prev_rank = int(prev.get("rank") or 0)
            cur_rank = cur_ranks[s.run_name]
            if prev_rank and prev_rank != cur_rank:
                deltas.append((s.run_name, prev_rank, cur_rank, prev_rank - cur_rank))

        climbers = sorted([d for d in deltas if d[3] > 0], key=lambda d: -d[3])[:3]
        droppers = sorted([d for d in deltas if d[3] < 0], key=lambda d: d[3])[:3]

        if climbers or droppers:
            lines.append("")
            lines.append("*Movers since last refresh*")
        for name, prev, cur, delta in climbers:
            since_ts = (history.get(name) or {}).get("last_closed_at")
            trade = _last_decisive_trade_since(base_dir, name, since_ts)
            blurb = f" · {_format_trade_blurb(trade)}" if trade else ""
            escaped = notifications._md_escape(f"{name} #{prev}→#{cur} (+{delta})")
            lines.append(f"🚀 {escaped}{blurb}")
        for name, prev, cur, delta in droppers:
            since_ts = (history.get(name) or {}).get("last_closed_at")
            trade = _last_decisive_trade_since(base_dir, name, since_ts)
            blurb = f" · {_format_trade_blurb(trade)}" if trade else ""
            escaped = notifications._md_escape(f"{name} #{prev}→#{cur} ({delta})")
            lines.append(f"📉 {escaped}{blurb}")

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
    history_path = base_dir / "leaderboard_history.json"
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
                history = _load_history(history_path)
                try:
                    msg = format_leaderboard_telegram(
                        stats, history=history, base_dir=base_dir
                    )
                    notifications._post(msg)
                except Exception as exc:
                    print(f"⚠️  leaderboard telegram failed: {type(exc).__name__}: {exc}", flush=True)
                # Persist new snapshot for next refresh's mover detection.
                ranked = sorted(stats, key=lambda s: s.roi_pct, reverse=True)
                new_history: dict[str, dict] = {}
                for rank, s in enumerate(ranked, 1):
                    last_closed_at = _latest_closed_at(base_dir, s.run_name)
                    new_history[s.run_name] = {
                        "rank": rank,
                        "realized_pnl": s.realized_pnl,
                        "roi_pct": s.roi_pct,
                        "last_closed_at": last_closed_at,
                    }
                _save_history(history_path, new_history)
        except Exception as exc:
            print(f"⚠️  leaderboard error: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(interval_seconds)


def _latest_closed_at(base_dir: Path, run_name: str) -> str | None:
    """Return the latest closed_at timestamp from the strategy's journal."""
    jp = base_dir / "dry_runs" / run_name / "journal.jsonl"
    if not jp.is_file():
        return None
    latest: str | None = None
    try:
        with jp.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = rec.get("closed_at")
                if ts and (latest is None or str(ts) > latest):
                    latest = str(ts)
    except Exception:
        return None
    return latest
