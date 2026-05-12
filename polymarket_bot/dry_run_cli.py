"""Typer sub-commands for managing named dry-run simulations.

Exposes ``pmbot dry-run {list,show,reset,rm,compare}`` plus the
``import-legacy`` migration helper. Each sub-command works on
``data/dry_runs/<name>/`` and never touches the live ledger
(``data/paper_state.json``).
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import typer

from polymarket_bot.dry_run_compare import (
    RunStats,
    compute_live_stats,
    compute_run_stats,
    format_comparison_table,
)
from polymarket_bot.dry_run_runs import (
    DryRunPaths,
    ensure_run_directory,
    list_runs,
    load_metadata,
    remove_run,
    reset_run,
    save_metadata,
)


app = typer.Typer(help="Manage named dry-run simulations.")


def _data_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data"


def _humanize_age(iso_ts: str | None) -> str:
    """Compact relative age (``12s``, ``3m``, ``1.5h``, ``4d``) from an ISO ts."""
    if not iso_ts:
        return "-"
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return "-"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts
    seconds = max(0.0, delta.total_seconds())
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


@app.command("list")
def cmd_list(
    all_: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="Inclure les runs sans activité (jamais lancés ou reset non relancés).",
    ),
) -> None:
    """List the live ledger (if present) and every run in data/dry_runs/.

    Par défaut, les runs sans activité (``total_ticks == 0`` et
    ``last_tick_at`` absent — typiquement un run fraîchement reset ou
    créé mais jamais lancé) sont masqués. Utiliser ``--all`` pour
    forcer leur affichage.
    """
    all_runs = list_runs(_data_dir())
    if all_:
        runs = all_runs
        hidden = 0
    else:
        runs = [r for r in all_runs if r.total_ticks > 0 or r.last_tick_at is not None]
        hidden = len(all_runs) - len(runs)
    live = compute_live_stats(_data_dir())
    if not runs and live is None:
        if hidden:
            typer.echo(
                f"(no active dry-run runs and no live ledger; "
                f"{hidden} reset/never-launched run(s) hidden — use --all to show)"
            )
        else:
            typer.echo("(no dry-run runs and no live ledger)")
        return
    typer.echo(
        f"{'NAME':<20}  {'PROFILE':<20}  {'MODE':<8}  {'STARTING':>10}  "
        f"{'CASH':>10}  {'INV':>10}  {'POS':>3}  {'EQUITY':>10}  {'RETURN':>8}  "
        f"{'TRADES':>6}  {'WIN%':>5}  {'TICKS':>7}  {'LAST_TICK':>9}  STARTED_AT"
    )

    def _emit(name: str, profile: str, mode: str, starting: float, ticks: int, started_at: str, stats: RunStats | None) -> None:
        if stats is None:
            cash = inv = equity = "n/a".rjust(10)
            pos = "n/a".rjust(3)
            ret = "n/a".rjust(8)
            trades = "n/a".rjust(6)
            win = "n/a".rjust(5)
            last_tick = "-".rjust(9)
        else:
            cash = f"{stats.cash:>9.2f}$"
            inv = f"{stats.invested:>9.2f}$"
            pos = f"{stats.open_positions:>3}"
            equity = f"{stats.equity:>9.2f}$"
            ret = f"{stats.return_pct * 100:>+7.2f}%"
            trades = f"{stats.trades_closed:>6}"
            win = f"{stats.win_rate * 100:>4.0f}%"
            last_tick = f"{_humanize_age(stats.last_tick_at):>9}"
        starting_s = f"{starting:>9.2f}$"
        typer.echo(
            f"{name:<20}  {profile:<20}  {mode:<8}  {starting_s}  "
            f"{cash}  {inv}  {pos}  {equity}  {ret}  {trades}  {win}  "
            f"{ticks:>7}  {last_tick}  {started_at}"
        )

    if live is not None:
        _emit(live.run_name, live.profile_source, live.mode, live.starting_cash, live.total_ticks, live.started_at, live)
    for r in runs:
        try:
            s = compute_run_stats(_data_dir(), r.run_name)
        except Exception:
            s = None
        _emit(r.run_name, r.profile_source, s.mode if s else "dry-run", r.starting_cash, r.total_ticks, r.started_at, s)
    if hidden:
        typer.echo(
            f"({hidden} reset/never-launched run(s) hidden — use --all to show)"
        )


@app.command("show")
def cmd_show(run: str = typer.Argument(..., help="Run name")) -> None:
    """Show detailed metadata + current stats for a run."""
    paths = DryRunPaths.for_run(_data_dir(), run)
    if not paths.metadata.is_file():
        typer.echo(f"run '{run}' not found in {paths.root}", err=True)
        raise typer.Exit(code=1)
    metadata = load_metadata(paths)
    stats = compute_run_stats(_data_dir(), run)
    typer.echo(f"Run:           {metadata.run_name}")
    typer.echo(f"Started:       {metadata.started_at}")
    typer.echo(f"Last tick:     {metadata.last_tick_at or '(never)'}")
    typer.echo(f"Total ticks:   {metadata.total_ticks}")
    typer.echo(f"Profile:       {metadata.profile_source}")
    typer.echo(f"Mode:          {metadata.mode}")
    typer.echo(f"Git sha:       {metadata.git_sha or '(unknown)'}")
    typer.echo("")
    typer.echo(f"Starting cash: {stats.starting_cash:.2f}$")
    typer.echo(f"Cash now:      {stats.cash:.2f}$")
    typer.echo(f"Invested:      {stats.invested:.2f}$")
    typer.echo(f"Open pos.:     {stats.open_positions}")
    typer.echo(f"Unrealized:    {stats.unrealized:+.2f}$")
    typer.echo(f"Equity:        {stats.equity:.2f}$")
    typer.echo(f"Return:        {stats.return_pct * 100:+.2f}%")
    typer.echo("")
    typer.echo(f"Trades closed: {stats.trades_closed}")
    typer.echo(f"Realized PnL:  {stats.realized_pnl:+.2f}")
    typer.echo(f"Win rate:      {stats.win_rate * 100:.0f}%")
    typer.echo(f"Max drawdown:  {stats.max_drawdown:+.2f}")
    typer.echo(f"Avg PnL/trade: {stats.avg_pnl:+.2f}")


@app.command("reset")
def cmd_reset(run: str = typer.Argument(..., help="Run name")) -> None:
    """Wipe state/journal/equity/decisions of a run (preserve metadata + config snapshot)."""
    paths = DryRunPaths.for_run(_data_dir(), run)
    if not paths.metadata.is_file():
        typer.echo(f"run '{run}' not found in {paths.root}", err=True)
        raise typer.Exit(code=1)
    reset_run(paths)
    typer.echo(f"reset: {paths.root}")


@app.command("rm")
def cmd_rm(
    run: str = typer.Argument(..., help="Run name"),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
) -> None:
    """Delete a run directory entirely."""
    paths = DryRunPaths.for_run(_data_dir(), run)
    if not paths.root.is_dir():
        typer.echo(f"run '{run}' not found in {paths.root}", err=True)
        raise typer.Exit(code=1)
    if not yes:
        typer.echo(f"Delete '{paths.root}'? Re-run with --yes to confirm.")
        raise typer.Exit(code=1)
    remove_run(paths)
    typer.echo(f"removed: {paths.root}")


@app.command("compare")
def cmd_compare(
    runs: list[str] = typer.Argument(..., help="Two or more run names"),
) -> None:
    """Side-by-side comparison of several runs."""
    if len(runs) < 2:
        typer.echo("compare needs at least 2 runs", err=True)
        raise typer.Exit(code=2)
    stats_list = []
    for r in runs:
        paths = DryRunPaths.for_run(_data_dir(), r)
        if not paths.metadata.is_file():
            typer.echo(f"run '{r}' not found in {paths.root}", err=True)
            raise typer.Exit(code=1)
        stats_list.append(compute_run_stats(_data_dir(), r))
    typer.echo(format_comparison_table(stats_list))


@app.command("import-legacy")
def cmd_import_legacy(
    name: str = typer.Option("legacy", "--name", help="Name of the imported run."),
) -> None:
    """Migrate data/dry_run_state.json + data/dry_run_journal.jsonl into a named run.

    Reconstructs ``starting_cash`` from the flow accounting:
        starting = cash + Σ open_stakes + Σ closed_cost_basis - Σ closed_proceeds

    The original ``data/dry_run_*`` files are NOT deleted; the operator
    cleans them up when ready.
    """
    base = _data_dir()
    legacy_state = base / "dry_run_state.json"
    legacy_journal = base / "dry_run_journal.jsonl"
    if not legacy_state.is_file() and not legacy_journal.is_file():
        typer.echo("no legacy files found (data/dry_run_state.json, data/dry_run_journal.jsonl)", err=True)
        raise typer.Exit(code=1)

    target_paths = DryRunPaths.for_run(base, name)
    if target_paths.metadata.is_file():
        typer.echo(
            f"target run '{name}' already exists at {target_paths.root}. "
            "Choose another --name or `pmbot dry-run rm {name}` first.",
            err=True,
        )
        raise typer.Exit(code=1)

    # Compute starting cash from flow accounting.
    closed_cost = 0.0
    closed_proceeds = 0.0
    trades = []
    if legacy_journal.is_file():
        trades = [
            json.loads(l)
            for l in legacy_journal.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        for t in trades:
            cost = float(t.get("cost_basis", 0.0))
            pnl = float(t.get("realized_pnl", 0.0))
            closed_cost += cost
            closed_proceeds += cost + pnl

    cash = 0.0
    open_stakes = 0.0
    if legacy_state.is_file():
        state = json.loads(legacy_state.read_text(encoding="utf-8"))
        cash = float(state.get("cash", 0.0))
        for p in state.get("positions", []):
            stake = float(p.get("stake", 0.0))
            if stake > 0:
                open_stakes += stake

    starting_cash = round(cash + open_stakes + closed_cost - closed_proceeds, 2)

    target_paths = ensure_run_directory(
        base, name, starting_cash=starting_cash, profile_source="(legacy import)"
    )
    if legacy_state.is_file():
        shutil.copy2(legacy_state, target_paths.state)
    if legacy_journal.is_file():
        shutil.copy2(legacy_journal, target_paths.journal)
    for src, dst in (
        ("dry_run_last_tick.json", target_paths.tick_state),
        ("dry_run_tick_history.jsonl", target_paths.tick_history),
        ("dry_run_strategy_overrides.json", target_paths.overrides),
    ):
        src_path = base / src
        if src_path.is_file():
            shutil.copy2(src_path, dst)

    # Inject tick count if tick_history.jsonl exists.
    metadata = load_metadata(target_paths)
    if target_paths.tick_history.is_file():
        metadata.total_ticks = sum(
            1 for l in target_paths.tick_history.read_text(encoding="utf-8").splitlines() if l.strip()
        )
        save_metadata(target_paths, metadata)

    typer.echo(f"imported as '{name}' at {target_paths.root}")
    typer.echo(f"  starting_cash (reconstructed): ${starting_cash:.2f}")
    typer.echo(f"  closed trades:                 {len(trades)}")
    typer.echo("  ⚠ no config_snapshot.toml (impossible to reconstruct)")
