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
    total_predictions: int = 0  # unique markets ever entered (closed + open)
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


def _starting_cash_from_profile(base_dir: Path, run_name: str) -> float | None:
    """Look up [run].starting_cash from configs/profiles/<run_name>.toml.

    Lets the leaderboard infer the correct bankroll baseline when no
    metadata.json exists (which is the common case — the bot doesn't
    currently write one).
    """
    try:
        import tomllib  # py311+
    except ImportError:  # pragma: no cover
        return None
    candidate = base_dir.parent / "configs" / "profiles" / f"{run_name}.toml"
    if not candidate.is_file():
        return None
    try:
        data = tomllib.loads(candidate.read_text(encoding="utf-8"))
    except Exception:
        return None
    run_section = data.get("run") or {}
    val = run_section.get("starting_cash")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def gather_run_stats(base_dir: Path, run_name: str) -> RunStats | None:
    """Read one dry-run directory and compute its standings.

    Always returns a RunStats — if the run directory doesn't exist yet
    (bot hasn't ticked), returns a stub seeded with the profile's
    declared starting_cash so it still appears in the leaderboard with
    the right baseline.
    """
    profile_cash = _starting_cash_from_profile(base_dir, run_name) or 100.0
    root = base_dir / "dry_runs" / run_name
    if not root.is_dir():
        return RunStats(
            run_name=run_name,
            starting_cash=profile_cash,
            cash=profile_cash,
            invested=0.0,
            unrealized_pnl=0.0,
            equity=profile_cash,
            open_positions=0,
            closed_trades=0,
            wins=0,
            losses=0,
            realized_pnl=0.0,
            started_at=None,
            total_ticks=0,
            biggest_win_today=0.0,
            biggest_loss_today=0.0,
            total_predictions=0,
        )

    # Profile is the source of truth for starting_cash — stale metadata
    # from a prior run with a different bankroll would otherwise produce
    # wildly wrong ROI%. Metadata still drives started_at / total_ticks.
    starting_cash = profile_cash
    total_ticks = 0
    started_at: str | None = None
    meta_path = root / "metadata.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            total_ticks = int(meta.get("total_ticks", 0))
            started_at = meta.get("started_at")
            # Only fall back to metadata's starting_cash when no profile
            # exists (e.g. ad-hoc run without a shipped TOML).
            if _starting_cash_from_profile(base_dir, run_name) is None:
                starting_cash = float(meta.get("starting_cash", profile_cash))
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

    # Dedupe by market_id: each unique market = 1 "prediction" with its
    # aggregate realized_pnl as the W/L vote. If the bot trades the same
    # market multiple times (exit + re-entry), all those cycles count as
    # one prediction with summed PnL.
    market_pnl: dict[str, float] = {}
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
                    try:
                        pnl = float(rec.get("realized_pnl", 0) or 0)
                    except (TypeError, ValueError):
                        pnl = 0.0
                    realized_pnl += pnl
                    mid = str(rec.get("market_id") or "")
                    if mid:
                        market_pnl[mid] = market_pnl.get(mid, 0.0) + pnl
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

    # One W/L per unique market based on net realized PnL across all cycles.
    closed_trades = len(market_pnl)
    wins = sum(1 for pnl in market_pnl.values() if pnl > 0)
    losses = sum(1 for pnl in market_pnl.values() if pnl < 0)

    # Total predictions = unique closed markets + currently open markets.
    open_market_ids = {str(p.get("market_id") or "") for p in open_positions if p.get("market_id")}
    total_predictions = len(set(market_pnl.keys()) | open_market_ids)

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
        total_predictions=total_predictions,
    )


def _live_strategy_name(base_dir: Path) -> str:
    """Read the active live profile from data/live_config_snapshot.toml.

    Falls back to ``"live"`` if the snapshot is missing or unparsable.
    The bot writes this file on every live start, so it tracks profile
    switches without code changes here.
    """
    snap = base_dir / "live_config_snapshot.toml"
    if not snap.is_file():
        return "live"
    try:
        import tomllib
    except ImportError:  # pragma: no cover
        return "live"
    try:
        data = tomllib.loads(snap.read_text(encoding="utf-8"))
        mode = data.get("run", {}).get("mode")
        if mode:
            return str(mode)
    except Exception:
        pass
    return "live"


def _live_baseline_path(base_dir: Path) -> Path:
    return base_dir / "live_baseline.json"


def _load_or_init_live_baseline(base_dir: Path, current_equity: float) -> float:
    """Read or snapshot the live starting_cash baseline.

    First call after deletion snapshots ``current_equity`` so ROI starts
    at 0% from that point. Subsequent calls reuse the stored value.
    """
    path = _live_baseline_path(base_dir)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return float(data.get("starting_cash", current_equity))
        except Exception:
            pass
    try:
        path.write_text(
            json.dumps(
                {
                    "starting_cash": float(current_equity),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass
    return float(current_equity)


def gather_live_stats(base_dir: Path) -> RunStats | None:
    """Build a RunStats for the LIVE paper-trading bot.

    Reads ``data/paper_state.json`` for cash + open positions, and
    ``data/trade_journal.jsonl`` for closed trades. ``starting_cash`` is
    persisted in ``data/live_baseline.json`` (snapshotted on first call)
    so ROI% is meaningful and comparable to dry-run strategies.
    """
    state_path = base_dir / "paper_state.json"
    if not state_path.is_file():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    cash = float(state.get("cash", 0.0) or 0.0)
    positions = state.get("positions", []) or []
    open_positions = [p for p in positions if p.get("status") == "open"]
    invested = sum(float(p.get("stake", 0) or 0) for p in open_positions)
    unrealized = sum(float(p.get("unrealized_pnl", 0) or 0) for p in open_positions)
    equity = cash + invested + unrealized

    # Baseline at cost basis (cash + open stakes) instead of current mark so
    # existing unrealized PnL on positions held at snapshot time shows up as
    # ROI from tick one.
    cost_basis = cash + invested
    starting_cash = _load_or_init_live_baseline(base_dir, cost_basis)

    market_pnl: dict[str, float] = {}
    realized_pnl = 0.0
    biggest_win_today = 0.0
    biggest_loss_today = 0.0
    today = datetime.now(timezone.utc).date()
    journal_path = base_dir / "trade_journal.jsonl"
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
                    try:
                        pnl = float(rec.get("realized_pnl", 0) or 0)
                    except (TypeError, ValueError):
                        pnl = 0.0
                    realized_pnl += pnl
                    mid = str(rec.get("market_id") or "")
                    if mid:
                        market_pnl[mid] = market_pnl.get(mid, 0.0) + pnl
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

    closed_trades = len(market_pnl)
    wins = sum(1 for pnl in market_pnl.values() if pnl > 0)
    losses = sum(1 for pnl in market_pnl.values() if pnl < 0)
    open_market_ids = {str(p.get("market_id") or "") for p in open_positions if p.get("market_id")}
    total_predictions = len(set(market_pnl.keys()) | open_market_ids)

    return RunStats(
        run_name=_live_strategy_name(base_dir),
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
        started_at=None,
        total_ticks=0,
        biggest_win_today=biggest_win_today,
        biggest_loss_today=biggest_loss_today,
        total_predictions=total_predictions,
    )


def format_leaderboard(
    stats: list[RunStats],
    *,
    live: RunStats | None = None,
    now: datetime | None = None,
) -> str:
    """Render a ranked text leaderboard. Pure function."""
    if not stats and live is None:
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
        rank_label = f"{i:>2}."
        pnl_str = f"{s.total_pnl:+9.2f}"
        roi_str = f"{s.roi_pct:+6.1f}%"
        big_win_str = f"+{s.biggest_win_today:.2f}" if s.biggest_win_today > 0 else "  —  "
        big_loss_str = f"{s.biggest_loss_today:.2f}" if s.biggest_loss_today < 0 else "  —  "
        lines.append(
            f"{rank_label:<4} {s.run_name:<10} "
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
    if live is not None:
        hypo_rank = 1 + sum(1 for s in ranked if s.roi_pct > live.roi_pct)
        total = len(ranked) + 1
        lines.append(f"🔵 {live.run_name.upper()} LIVE — running on real money")
        lines.append(
            f"   Equity: ${live.equity:.2f}  PnL: {live.total_pnl:+.2f}  "
            f"ROI: {live.roi_pct:+.1f}%  "
            f"{live.wins}W/{live.losses}L  Open: {live.open_positions}  "
            f"Closed: {live.closed_trades}"
        )
        lines.append(f"   If ranked among dry strategies: #{hypo_rank} of {total}")
        lines.append(bar)
    return "\n".join(lines)


def format_leaderboard_telegram(
    stats: list[RunStats],
    *,
    live: RunStats | None = None,
    now: datetime | None = None,
) -> str:
    """Compact Telegram leaderboard — ranked by ROI."""
    if not stats and live is None:
        return "🏁 *Leaderboard*: no runs found"
    ranked = sorted(stats, key=lambda s: s.roi_pct, reverse=True)
    now = now or datetime.now(timezone.utc)
    stamp = notifications._md_escape(now.strftime("%H:%M"))

    lines = [f"🏁 *Leaderboard* · {stamp} UTC · {len(ranked)} strategies", ""]
    for i, s in enumerate(ranked, 1):
        rank_str = notifications._md_escape(f"{i:>2}.")
        name = notifications._md_escape(s.run_name)
        if s.roi_pct > 0:
            color = "🟢"
        elif s.roi_pct < 0:
            color = "🔴"
        else:
            color = "⚪"
        roi_str = notifications._md_escape(f"{s.roi_pct:+5.1f}%")
        eq_str = notifications._md_escape(f"${s.equity:.0f}")
        cash_str = notifications._md_escape(f"💵${s.cash:.0f}")
        open_str = notifications._md_escape(f"📦{s.open_positions}")
        wl_str = notifications._md_escape(f"{s.wins}W/{s.losses}L")
        lines.append(
            f"{rank_str} `{name}` {color} {eq_str} {cash_str} {open_str} {roi_str} {wl_str}"
        )

    if live is not None:
        hypo_rank = 1 + sum(1 for s in ranked if s.roi_pct > live.roi_pct)
        total = len(ranked) + 1
        if live.roi_pct > 0:
            live_color = "🟢"
        elif live.roi_pct < 0:
            live_color = "🔴"
        else:
            live_color = "⚪"
        eq_l = notifications._md_escape(f"${live.equity:.2f}")
        roi_l = notifications._md_escape(f"{live.roi_pct:+.1f}%")
        pnl_l = notifications._md_escape(f"{live.total_pnl:+.2f}")
        wl_l = notifications._md_escape(f"{live.wins}W/{live.losses}L")
        rank_l = notifications._md_escape(f"#{hypo_rank} of {total}")
        sep = notifications._md_escape("━━━━━━━━━━━━━━━━━")
        name_l = notifications._md_escape(live.run_name)
        lines.append("")
        lines.append(sep)
        lines.append(f"🔵 *{name_l} LIVE is also running\\!*")
        lines.append(f"   Current: {live_color} {eq_l}  {roi_l}  PnL {pnl_l}  {wl_l}")
        lines.append(f"   If listed: would rank {rank_l}")

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
            live = gather_live_stats(base_dir)
            print("", flush=True)
            print(format_leaderboard(stats, live=live), flush=True)
            print("", flush=True)
            if telegram and notifications.is_enabled() and (stats or live is not None):
                try:
                    notifications._post(format_leaderboard_telegram(stats, live=live))
                except Exception as exc:
                    print(f"⚠️  leaderboard telegram failed: {type(exc).__name__}: {exc}", flush=True)
        except Exception as exc:
            print(f"⚠️  leaderboard error: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(interval_seconds)
