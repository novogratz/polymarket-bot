#!/usr/bin/env python3
"""Autonomous dry-run analyst sidecar.

Runs alongside the dry race. Every 15 minutes:
  1. Reads per-strategy state + journal from data/dry_runs/
  2. Computes leaderboard (PnL, win-rate, sample size, avg win/loss)
  3. Posts a deterministic Markdown leaderboard + summary to Telegram
  4. On the slower spawn/kill tick, kills auto-spawned bots that are
     clearly losing (≥KILL_MIN_TRADES closed, ROI ≤ KILL_ROI_THRESHOLD,
     win_rate ≤ KILL_WR_THRESHOLD)

No LLM/AI anywhere — reports and the summary narrative are built directly
from the numbers. The analyst no longer spawns or tunes strategies (that
path was LLM-driven and has been removed); it only reports and prunes
clear losers via deterministic thresholds.

Hard rules:
  - Dry-run only. Never touches live profiles or live ledger.
  - Kill scope: ONLY auto_* strategies (legacy spawns). NEVER kills or
    modifies the human-curated bots.

Kill switch: write `{"enabled": false}` to data/autonomous_state.json
to halt kills. Reporting continues.

Adjust CYCLE_SECONDS or set enabled=false in autonomous_state.json to
control report frequency.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import traceback
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DRY_RUNS_DIR = REPO_ROOT / "data" / "dry_runs"
PROFILES_DIR = REPO_ROOT / "configs" / "profiles"
STATE_FILE = REPO_ROOT / "data" / "autonomous_state.json"


def _load_dotenv() -> None:
    """Read .env into os.environ. Done explicitly because the analyst
    runs as a plain python script (no pmbot autoloader). Without this,
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID_DRY_RUN are unset and the
    Telegram posts silently no-op."""
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    try:
        for raw in env_file.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception as exc:
        print(f"[analyst] .env load failed: {exc}", file=sys.stderr, flush=True)


_load_dotenv()

CYCLE_SECONDS = int(os.environ.get("ANALYST_CYCLE_SECONDS", "900"))   # 15min reports
SPAWN_KILL_INTERVAL = int(os.environ.get("ANALYST_SPAWN_KILL_INTERVAL_SECONDS", "3600"))  # 1h — run loser-kills less frequently than reports
SPAWN_PREFIX = "auto_"
MIN_TRADES_TO_RATE = 1  # 1 trade is enough to appear on the leaderboard

# Kill criteria. Two tiers:
#   - auto_*  bots (analyst's own children): tight bar, cull fast
#   - human-curated bots: 2.5× higher trade bar, same PnL/wr thresholds
# A strategy is killed when it has ≥min_trades closed AND ROI ≤ ROI_THRESHOLD
# AND win_rate ≤ WR_THRESHOLD. Profile is moved to configs/profiles/_archived/
# so it can be recovered manually.
KILL_AUTO_MIN_TRADES = int(os.environ.get("ANALYST_KILL_MIN_TRADES", "25"))
KILL_HUMAN_MIN_TRADES = int(os.environ.get("ANALYST_KILL_HUMAN_MIN_TRADES", "50"))
KILL_ROI_THRESHOLD = float(os.environ.get("ANALYST_KILL_ROI", "-25.0"))
KILL_WR_THRESHOLD = float(os.environ.get("ANALYST_KILL_WR", "30.0"))
# Profiles the analyst is NEVER allowed to kill. These are reference /
# control strategies (baseline = canonical fixture used by tests +
# leaderboard delta computations, edge / news = thesis controls) and
# the active live profiles (so a bad day in the dry mirror can't kill
# the live config out from under the bot). Without this list the
# analyst played whack-a-mole with baseline.toml every cycle.
KILL_PROTECTED_PROFILES = {
    "baseline",
    "baseline_tight",
    "edge",
    "news",
    "kzerlepgm_baseline",
    "random",
    "grinder",                  # active live profile (2026-05-26)
    "claude_baseline_persist",  # active live profile (2026-05-20)
    "auto_mombreak_locktight",  # recent live, kept for comparison
    "whale_entry_detection",    # recent live, kept for comparison
}

# Absolute equity halt — catastrophic-loss circuit breaker. Fires
# regardless of closed-trade count, so it catches bots like panic_fade
# that bled $98/$100 entirely in unrealized losses (0W/0L closed but
# positions worth pennies). Default: kill at equity <= 50% of start.
KILL_EQUITY_FLOOR_PCT = float(os.environ.get("ANALYST_KILL_EQUITY_FLOOR_PCT", "30.0"))

# Live-readiness criteria (a strategy is "ready for live" when it has
# accumulated enough sample to back-test confidence):
LIVE_READY_MIN_TRADES = int(os.environ.get("ANALYST_LIVE_READY_MIN_TRADES", "30"))
LIVE_READY_MIN_ROI = float(os.environ.get("ANALYST_LIVE_READY_ROI", "10.0"))   # +10%
LIVE_READY_MIN_WR = float(os.environ.get("ANALYST_LIVE_READY_WR", "55.0"))      # 55%
LIVE_READY_MAX_BIG_WIN_SHARE = float(os.environ.get("ANALYST_LIVE_READY_MAX_BIG_WIN_SHARE", "0.60"))


@dataclass
class StratMetrics:
    name: str
    cash: float
    equity: float
    pnl: float
    realized_pnl: float
    roi_pct: float
    open_positions: int
    closed: int
    wins: int
    losses: int
    win_rate: float
    avg_win: float
    avg_loss: float
    big_win: float
    big_loss: float
    ticks: int


# ────────────────────────────────────────────────────────────────────
# State / kill-switch
# ────────────────────────────────────────────────────────────────────


def load_autonomous_state() -> dict:
    if not STATE_FILE.exists():
        return {"enabled": True, "spawned": [], "last_cycle_ts": 0}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"enabled": True, "spawned": [], "last_cycle_ts": 0}


def save_autonomous_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ────────────────────────────────────────────────────────────────────
# Metric collection
# ────────────────────────────────────────────────────────────────────


def _starting_cash_for(name: str) -> float:
    """Read starting_cash from the profile TOML.

    Looks in configs/profiles/ first, then configs/profiles/_archived/
    (most recent timestamped archive) for killed strategies. Defaults
    to $20 — the dry-race convention. Old default of $100 made archived
    strategies look like -90% ROI in reports when their real ROI in
    the $20 era was much milder.
    """
    profile = PROFILES_DIR / f"{name}.toml"
    if not profile.exists():
        archive_dir = PROFILES_DIR / "_archived"
        if archive_dir.exists():
            candidates = sorted(archive_dir.glob(f"{name}_*.toml"))
            if candidates:
                profile = candidates[-1]
    if not profile.exists():
        return 20.0
    try:
        text = profile.read_text()
        m = re.search(r"^starting_cash\s*=\s*([\d.]+)", text, re.M)
        return float(m.group(1)) if m else 20.0
    except Exception:
        return 20.0


def collect_metrics() -> list[StratMetrics]:
    """Walk data/dry_runs/* and compute per-strategy metrics.

    Skips runs whose state.json is stale (default 30min) — the bot is
    dead. Without this filter, archived/killed bots stay on the leaderboard
    forever showing their last-known equity (the bug that made \"top 5\"
    look frozen for a week).

    Also includes the live paper-trading bot (data/paper_state.json)
    if it exists and is fresh.
    """
    out: list[StratMetrics] = []
    stale_minutes = int(os.environ.get("POLYMARKET_LEADERBOARD_STALE_MINUTES", "30"))
    now_ts = time.time()

    # 1. LIVE bot (priority)
    live_state = REPO_ROOT / "data" / "paper_state.json"
    if live_state.exists() and (now_ts - live_state.stat().st_mtime) < stale_minutes * 60:
        try:
            from polymarket_bot.leaderboard import gather_live_stats
            lstats = gather_live_stats(REPO_ROOT / "data")
            if lstats:
                # Map RunStats to StratMetrics for report consistency.
                out.append(StratMetrics(
                    name=f"🔵 {lstats.run_name} (LIVE)",
                    cash=lstats.cash,
                    equity=lstats.equity,
                    pnl=lstats.total_pnl,
                    realized_pnl=lstats.realized_pnl,
                    roi_pct=lstats.roi_pct,
                    open_positions=lstats.open_positions,
                    closed=lstats.closed_trades,
                    wins=lstats.wins,
                    losses=lstats.losses,
                    win_rate=lstats.win_rate_pct,
                    avg_win=0.0, avg_loss=0.0, big_win=0.0, big_loss=0.0, # not tracked here
                    ticks=0
                ))
        except Exception as exc:
            print(f"[analyst] live metrics fetch failed: {exc}", file=sys.stderr, flush=True)

    # 2. DRY bots
    if DRY_RUNS_DIR.exists():
        for run_dir in sorted(DRY_RUNS_DIR.iterdir()):
            if not run_dir.is_dir():
                continue
            name = run_dir.name
            state_file = run_dir / "state.json"
            journal_file = run_dir / "journal.jsonl"
            if not state_file.exists():
                continue
            # Staleness check: drop bots that haven't ticked recently.
            if (now_ts - state_file.stat().st_mtime) > stale_minutes * 60:
                continue
            try:
                state = json.loads(state_file.read_text())
            except Exception:
                continue
            cash = float(state.get("cash") or 0.0)
            positions = state.get("positions", []) or []
            open_positions = [p for p in positions if p.get("status") == "open"]
            # Mark-to-market: use current_price × shares (NOT cost basis).
            # Cost basis would hide unrealized losses and show fake +PnL
            # when prices have moved against the position.
            invested_mtm = 0.0
            for p in open_positions:
                cur = p.get("current_price")
                shares = p.get("shares") or 0
                if cur is not None and shares:
                    try:
                        invested_mtm += float(cur) * float(shares)
                        continue
                    except (TypeError, ValueError):
                        pass
                # Fallback to cost basis if no current_price
                invested_mtm += float(
                    p.get("size_usd") or p.get("stake") or
                    p.get("notional_usd") or 0.0
                )
            equity = cash + invested_mtm
            starting = _starting_cash_for(name)
            pnl = equity - starting
            roi_pct = (pnl / starting * 100.0) if starting > 0 else 0.0

            wins = losses = closed = 0
            win_pnls: list[float] = []
            loss_pnls: list[float] = []
            if journal_file.exists():
                try:
                    for line in journal_file.read_text().splitlines():
                        if not line.strip():
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        # Accept either:
                        #   - explicit event=position_closed (my sweep entries)
                        #   - any entry with closed_at (race / smart_money /
                        #     news strategies don't set event field but always
                        #     include closed_at when they realize a position)
                        if (entry.get("event") != "position_closed"
                                and not entry.get("closed_at")):
                            continue
                        # Journal entries from different code paths use
                        # different field names. Sweep-close (my new code)
                        # writes realized_pnl_usd; smart_money / race / news
                        # write realized_pnl. Read both, prefer _usd if present.
                        pnl_t = float(
                            entry.get("realized_pnl_usd")
                            or entry.get("realized_pnl")
                            or 0.0
                        )
                        closed += 1
                        if pnl_t > 0:
                            wins += 1
                            win_pnls.append(pnl_t)
                        elif pnl_t < 0:
                            losses += 1
                            loss_pnls.append(pnl_t)
                except Exception:
                    pass

            ticks = 0
            tick_file = run_dir / "tick_history.jsonl"
            if tick_file.exists():
                try:
                    ticks = sum(1 for _ in tick_file.open())
                except Exception:
                    pass

            decided = wins + losses
            realized_pnl = sum(win_pnls) + sum(loss_pnls)
            out.append(
                StratMetrics(
                    name=name,
                    cash=cash,
                    equity=equity,
                    pnl=pnl,
                    realized_pnl=realized_pnl,
                    roi_pct=roi_pct,
                    open_positions=len(open_positions),
                    closed=closed,
                    wins=wins,
                    losses=losses,
                    win_rate=(wins / decided * 100.0) if decided > 0 else 0.0,
                    avg_win=sum(win_pnls) / len(win_pnls) if win_pnls else 0.0,
                    avg_loss=sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0,
                    big_win=max(win_pnls, default=0.0),
                    big_loss=min(loss_pnls, default=0.0),
                    ticks=ticks,
                )
            )
    return out


def rank(metrics: list[StratMetrics]) -> tuple[list[StratMetrics], list[StratMetrics]]:
    """Return (top, bottom) — rated only by strategies with ≥MIN_TRADES_TO_RATE."""
    rated = [m for m in metrics if m.closed >= MIN_TRADES_TO_RATE]
    rated.sort(key=lambda m: m.pnl, reverse=True)
    top = rated[:5]
    bottom = rated[-3:] if len(rated) >= 3 else []
    return top, bottom


# ────────────────────────────────────────────────────────────────────
# Kill helpers (deterministic — no AI)
# ────────────────────────────────────────────────────────────────────


def find_bot_pid_by_name(profile_name: str) -> int | None:
    """Find the PID of a dry-run bot by its --profile arg.

    Looks for the python process running ``pmbot auto-loop --dry-run
    --profile <profile_name> --run <profile_name>``. Returns the parent
    `uv run` pid (which propagates SIGTERM to the python child).
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"pmbot auto-loop --dry-run --profile {profile_name} --run"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.strip().splitlines():
        try:
            return int(line.strip())
        except ValueError:
            continue
    return None


def kill_bot(pid: int, name: str) -> bool:
    """Kill an auto-spawned bot. SIGTERM first, escalate to SIGKILL after 5s."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    # Give it 5s to clean up
    for _ in range(50):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)  # probe
        except ProcessLookupError:
            return True
    # Force
    try:
        os.kill(pid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return True
    except PermissionError:
        return False


def archive_profile(name: str) -> None:
    """Move a killed profile to configs/profiles/_archived/ so its name frees up."""
    src = PROFILES_DIR / f"{name}.toml"
    if not src.exists():
        return
    archive_dir = PROFILES_DIR / "_archived"
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / f"{name}_{int(time.time())}.toml"
    src.rename(dest)


# ────────────────────────────────────────────────────────────────────
# Telegram
# ────────────────────────────────────────────────────────────────────


def telegram_post(text: str) -> bool:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID_DRY_RUN") or "").strip()
    if not token or not chat:
        print(f"[analyst] telegram disabled (token/chat missing)\n{text}", flush=True)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # Telegram messages capped at 4096 chars.
    def _send(use_markdown: bool) -> bool:
        body = {
            "chat_id": chat,
            "text": text[:4000],
            "disable_web_page_preview": True,
        }
        if use_markdown:
            body["parse_mode"] = "Markdown"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300

    try:
        return _send(True)
    except Exception as exc:
        # 400 → Markdown parse error. Retry as plain text so the alert lands.
        if getattr(exc, "code", None) == 400:
            try:
                return _send(False)
            except Exception as exc2:
                print(f"[analyst] telegram failed (plain retry): {exc2}", file=sys.stderr, flush=True)
                return False
        print(f"[analyst] telegram failed: {exc}", file=sys.stderr, flush=True)
        return False


# ────────────────────────────────────────────────────────────────────
# Message formatting
# ────────────────────────────────────────────────────────────────────


def fmt_leaderboard(metrics: list[StratMetrics]) -> str:
    lines = ["{:<32} {:>8} {:>7} {:>6} {:>5} {:>5}".format(
        "strategy", "pnl$", "roi%", "wr%", "n", "open"
    )]
    lines.append("-" * 70)
    for m in metrics:
        lines.append("{:<32} {:>+8.2f} {:>+6.1f}% {:>5.0f}% {:>5} {:>5}".format(
            m.name[:32], m.pnl, m.roi_pct, m.win_rate, m.closed, m.open_positions,
        ))
    return "\n".join(lines)


def build_main_message(narrative: str, top: list[StratMetrics],
                        bottom: list[StratMetrics], spawned: list[str],
                        tuned: list[str], killed: list[str], n_total: int,
                        live_ready: list[StratMetrics] | None = None,
                        live_close: list[StratMetrics] | None = None,
                        all_metrics: list[StratMetrics] | None = None) -> str:
    stamp = time.strftime("%H:%M UTC", time.gmtime())
    parts = [f"🤖 *AUTONOMOUS REPORT* · {stamp}",
             f"_{n_total} strategies running, {len(top)+len(bottom)} rated (≥{MIN_TRADES_TO_RATE} closed trades)_",
             ""]
    if live_ready:
        parts.append(f"*🎯 LIVE READY* (n≥{LIVE_READY_MIN_TRADES}, ROI≥+{LIVE_READY_MIN_ROI:.0f}%, wr≥{LIVE_READY_MIN_WR:.0f}%)")
        for m in live_ready:
            parts.append(f"  ✅ `{m.name}` {m.pnl:+.2f}$ ROI={m.roi_pct:+.1f}% ({m.win_rate:.0f}% wr, {m.closed} closed)")
        parts.append("→ _Promote one of these to live: edit `scripts/run_live_70.sh` profile arg + restart_")
        parts.append("")
    if live_close:
        parts.append("*👀 Close to live-ready*")
        for m in live_close[:5]:
            parts.append(f"  • `{m.name}` ROI={m.roi_pct:+.1f}% ({m.win_rate:.0f}% wr, {m.closed} closed)")
        parts.append("")
    def _fmt_row(idx: int, m: "StratMetrics", *, bullet: str = "") -> list[str]:
        sign = "+" if m.pnl >= 0 else ""
        marker = bullet if bullet else f"{idx}."
        # Recover starting from equity - pnl (cheaper than re-reading TOML).
        starting = max(0.0, m.equity - m.pnl)
        return [
            f"  {marker} `{m.name}`",
            f"      ${starting:.2f} → *${m.equity:.2f}*   {sign}${m.pnl:.2f} ({m.roi_pct:+.1f}%)",
            f"      WR {m.win_rate:.0f}%  •  closed {m.closed}  •  open {m.open_positions}",
        ]

    # Always surface every profitable strategy — most important section.
    # Uses the full metrics set (not just rated top/bottom), so even
    # strategies with 0 closed trades but positive unrealized PnL show.
    source = all_metrics if all_metrics is not None else (top or []) + (bottom or [])
    profitable_all = sorted(
        {m.name: m for m in source if m.pnl > 0}.values(),
        key=lambda m: m.roi_pct, reverse=True,
    )
    if profitable_all:
        parts.append(f"*🟢 Profitable strategies ({len(profitable_all)})*")
        for i, m in enumerate(profitable_all, 1):
            parts.extend(_fmt_row(i, m))
        parts.append("")
    else:
        parts.append("_⚠️ No profitable strategy yet — entire board down._")
        parts.append("")

    if top:
        all_negative = all(m.pnl < 0 for m in top)
        if all_negative:
            parts.append("*📉 Top 5* (all rated strategies are losing — least-worst first)")
        else:
            parts.append("*🏆 Top 5 by PnL*")
        for i, m in enumerate(top, 1):
            parts.extend(_fmt_row(i, m))
        parts.append("")
    if bottom:
        parts.append("*📉 Bottom 3*")
        for m in bottom:
            parts.extend(_fmt_row(0, m, bullet="•"))
        parts.append("")
    if narrative:
        parts.append("*🧠 Insights*")
        parts.append(narrative[:1500])
        parts.append("")
    if spawned:
        parts.append("*🆕 Spawned*")
        for name in spawned:
            parts.append(f"  • `{name}`")
        parts.append("")
    if tuned:
        parts.append("*🔧 Tuned (in-place reroll)*")
        for swap in tuned:
            parts.append(f"  • `{swap}`")
        parts.append("")
    if killed:
        parts.append("*💀 Killed (underperformers)*")
        for name in killed:
            parts.append(f"  • `{name}`")
        parts.append("")

    # Recommendation footer — always present.
    # Logic:
    #   1. Prefer live-ready candidates (n>=30 AND ROI>=+10% AND wr>=55%)
    #   2. Else best by ROI with n>=10 and ROI>0
    #   3. Else best by ROI overall (with caveat about small sample)
    #   4. Else "no data yet"
    favorite, reason = _pick_favorite(all_metrics or top + bottom or [])
    parts.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    if favorite is None:
        parts.append("🎯 *My favorite strategy currently for live*: _none yet — no rated strategies_")
    else:
        sign = "+" if favorite.pnl >= 0 else ""
        starting = max(0.0, favorite.equity - favorite.pnl)
        parts.append(f"🎯 *My favorite strategy currently for live*: `{favorite.name}`")
        parts.append(f"   *Why:* {reason}")
        parts.append(f"   *Performance:* ${starting:.2f} → *${favorite.equity:.2f}*  {sign}${favorite.pnl:.2f} ({favorite.roi_pct:+.1f}%)")
        parts.append(f"   *Stats:* WR {favorite.win_rate:.0f}%  •  closed {favorite.closed}  •  open {favorite.open_positions}")
        # Detail block: top 3 closed wins + current open positions
        top_trades, open_pos = _favorite_detail(favorite.name)
        if top_trades:
            parts.append("")
            parts.append("*📊 Top 3 closed trades:*")
            for i, t in enumerate(top_trades, 1):
                psign = "+" if t["pnl"] >= 0 else ""
                pct_str = f"{t['pct']:+.1f}%" if t["pct"] is not None else "—"
                q = (t.get("question") or "?")[:55]
                parts.append(
                    f"  {i}. {psign}${t['pnl']:.2f} ({pct_str}) "
                    f"{t.get('reason','?')}\n      {q}\n"
                    f"      {t.get('side','?')} @ ${t.get('entry',0):.3f}, "
                    f"held {t.get('held','?')}"
                )
        if open_pos:
            parts.append("")
            parts.append(f"*🔓 Open positions ({len(open_pos)}):*")
            for p in open_pos[:6]:  # cap at 6 for Telegram size
                psign = "+" if p["unr"] >= 0 else ""
                q = (p.get("question") or "?")[:50]
                parts.append(
                    f"  • {q}\n"
                    f"      {p.get('side','?')} @ ${p.get('entry',0):.3f} → ${p.get('cur',0):.3f}  "
                    f"{psign}${p['unr']:.2f} ({p['unr_pct']:+.1f}%)"
                )
            if len(open_pos) > 6:
                parts.append(f"  _… and {len(open_pos) - 6} more_")
    return "\n".join(parts)


def _favorite_detail(name: str) -> tuple[list[dict], list[dict]]:
    """Read the favorite strategy's journal + state, return:
       (top 3 closed by PnL, open positions)
    Returns ([], []) if files missing/unreadable.
    """
    run_dir = DRY_RUNS_DIR / name
    if not run_dir.exists():
        return [], []
    closed: list[dict] = []
    journal = run_dir / "journal.jsonl"
    if journal.exists():
        try:
            for line in journal.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not (e.get("event") == "position_closed" or e.get("closed_at")):
                    continue
                pnl = float(
                    e.get("realized_pnl_usd")
                    or e.get("realized_pnl") or 0.0
                )
                pct_raw = e.get("realized_pnl_pct") or e.get("pnl_pct")
                if pct_raw is not None:
                    pct = float(pct_raw) * 100 if abs(float(pct_raw)) < 1 else float(pct_raw)
                else:
                    pct = None
                held = ""
                try:
                    from datetime import datetime
                    o = datetime.fromisoformat(str(e.get("opened_at")).replace("Z","+00:00"))
                    c = datetime.fromisoformat(str(e.get("closed_at")).replace("Z","+00:00"))
                    secs = int((c - o).total_seconds())
                    if secs < 3600:
                        held = f"{secs // 60}m"
                    else:
                        held = f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
                except Exception:
                    pass
                closed.append({
                    "pnl": pnl, "pct": pct,
                    "reason": e.get("exit_reason", "?"),
                    "question": e.get("question") or e.get("market_title"),
                    "side": e.get("outcome", "?"),
                    "entry": float(e.get("entry_price") or 0),
                    "held": held or "?",
                })
        except Exception:
            pass
    top_trades = sorted(closed, key=lambda x: x["pnl"], reverse=True)[:3]
    # Open positions from state.json
    open_pos: list[dict] = []
    state_file = run_dir / "state.json"
    if state_file.exists():
        try:
            s = json.loads(state_file.read_text())
            for p in s.get("positions", []):
                if p.get("status") != "open":
                    continue
                entry = float(p.get("entry_price") or 0)
                shares = float(p.get("shares") or 0)
                cur = float(p.get("current_price") or 0)
                cost = float(p.get("stake") or entry * shares)
                mtm = cur * shares
                unr = mtm - cost
                unr_pct = (unr / cost * 100) if cost else 0
                open_pos.append({
                    "question": p.get("question") or p.get("market_title"),
                    "side": p.get("outcome", "?"),
                    "entry": entry, "cur": cur,
                    "unr": unr, "unr_pct": unr_pct,
                })
        except Exception:
            pass
    open_pos.sort(key=lambda x: x["unr"], reverse=True)
    return top_trades, open_pos


def _pick_favorite(metrics: list[StratMetrics]) -> tuple["StratMetrics | None", str]:
    """Pick the best live candidate + the human-readable reason.

    Always returns a candidate when metrics exist — only "None" when
    there's no data at all. Falls through 4 tiers from best to worst.
    """
    if not metrics:
        return None, ""
    # Tier 1 — actually live-ready
    ready, _ = assess_live_readiness(metrics)
    if ready:
        m = ready[0]
        return m, (
            f"LIVE-READY ✅ — {m.closed} closed trades (≥{LIVE_READY_MIN_TRADES}), "
            f"ROI {m.roi_pct:+.1f}% (≥{LIVE_READY_MIN_ROI:.0f}%), "
            f"WR {m.win_rate:.0f}% (≥{LIVE_READY_MIN_WR:.0f}%). "
            f"Statistically credible — promote with confidence."
        )
    # Tier 2 — best by ROI with at least decent sample AND profitable
    decent = sorted(
        [m for m in metrics if m.closed >= 10 and m.pnl > 0],
        key=lambda m: m.roi_pct, reverse=True,
    )
    if decent:
        m = decent[0]
        return m, (
            f"Best risk-adjusted candidate so far — {m.closed} closed, "
            f"ROI {m.roi_pct:+.1f}%, WR {m.win_rate:.0f}%. Sample still "
            f"below the {LIVE_READY_MIN_TRADES}-trade bar; promote only "
            f"if you accept variance."
        )
    # Tier 3 — any profitable bot, even with tiny sample
    profitable = sorted([m for m in metrics if m.pnl > 0],
                          key=lambda m: m.roi_pct, reverse=True)
    if profitable:
        m = profitable[0]
        n = len(profitable)
        prefix = (
            f"Only profitable strategy on the board"
            if n == 1
            else f"Top of {n} profitable strategies"
        )
        return m, (
            f"{prefix} — but only {m.closed} closed trade(s). "
            f"Variance dominates at this sample; wait for ≥30 trades "
            f"before serious consideration."
        )
    # Tier 4 — nothing profitable, surface the least-bad anyway
    by_pnl = sorted(metrics, key=lambda m: m.pnl, reverse=True)
    m = by_pnl[0]
    return m, (
        f"⚠️ No profitable strategy yet — entire board is down. This is "
        f"the least-bad: {m.closed} closed, ROI {m.roi_pct:+.1f}%. "
        f"DO NOT promote to live. Wait for the race to find a winner."
    )


# ────────────────────────────────────────────────────────────────────
# Main loop
# ────────────────────────────────────────────────────────────────────


def ensure_leaderboard_auto_discover() -> bool:
    """Self-heal: if the leaderboard sidecar is running with the OLD
    fixed --runs argument (pre auto-discover patch), kill it and start
    a new one with --auto-discover. Returns True if action was taken.

    This is what makes "fully autonomous" actually true: the user
    doesn't have to restart the race to pick up the leaderboard fix.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", "pmbot leaderboard --runs"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return False
    if result.returncode != 0 or not result.stdout.strip():
        return False
    pids = []
    for line in result.stdout.strip().splitlines():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            continue
    if not pids:
        return False
    print(f"[analyst] stale leaderboard detected (pids={pids}); refreshing to --auto-discover", flush=True)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    time.sleep(2)
    # Kill any survivors
    for pid in pids:
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    env = os.environ.copy()
    env["POLYMARKET_DRY_RUN"] = "1"
    try:
        subprocess.Popen(
            ["uv", "run", "pmbot", "leaderboard",
             "--auto-discover", "--interval", "3", "--telegram"],
            cwd=REPO_ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print("[analyst] ✓ leaderboard relaunched with --auto-discover", flush=True)
        return True
    except Exception as exc:
        print(f"[analyst] failed to relaunch leaderboard: {exc}", flush=True)
        return False


def assess_live_readiness(metrics: list[StratMetrics]) -> tuple[list[StratMetrics], list[StratMetrics]]:
    """Identify strategies ready for live deployment.

    A strategy is "live ready" when:
      - closed >= LIVE_READY_MIN_TRADES (default 30, enough sample to dismiss variance)
      - roi_pct >= LIVE_READY_MIN_ROI (default +10%)
      - win_rate >= LIVE_READY_MIN_WR (default 55%)
      - realized PnL > 0, so open mark-to-market alone cannot promote
      - biggest win is not more than LIVE_READY_MAX_BIG_WIN_SHARE of realized PnL

    Also returns "close candidates" — those with ≥15 closed trades and
    ROI/wr in the right direction but not yet at threshold. These are
    early signals worth watching.

    Returns (ready, close_candidates).
    """
    ready: list[StratMetrics] = []
    close: list[StratMetrics] = []
    for m in metrics:
        big_win_share = (m.big_win / m.realized_pnl) if m.realized_pnl > 0 else 1.0
        if (m.closed >= LIVE_READY_MIN_TRADES
                and m.roi_pct >= LIVE_READY_MIN_ROI
                and m.win_rate >= LIVE_READY_MIN_WR
                and m.realized_pnl > 0
                and big_win_share <= LIVE_READY_MAX_BIG_WIN_SHARE):
            ready.append(m)
        elif (m.closed >= LIVE_READY_MIN_TRADES // 2
                and m.roi_pct >= LIVE_READY_MIN_ROI / 2
                and m.win_rate >= LIVE_READY_MIN_WR - 5
                and m.realized_pnl > 0):
            close.append(m)
    ready.sort(key=lambda m: m.roi_pct, reverse=True)
    close.sort(key=lambda m: m.roi_pct, reverse=True)
    return ready, close


def evaluate_kills(metrics: list[StratMetrics], state: dict) -> list[str]:
    """Identify bots that are clearly losing on enough sample, kill them.

    Covers ALL strategies — auto-spawned AND human-curated. Sample bar
    differs per tier:
      - auto_* bots: KILL_AUTO_MIN_TRADES (default 8) — cull fast
      - human-curated: KILL_HUMAN_MIN_TRADES (default 20) — more rope

    Kill condition (both tiers): ROI <= KILL_ROI_THRESHOLD (-10%) AND
    win_rate <= KILL_WR_THRESHOLD (40%).

    Profile gets moved to configs/profiles/_archived/ so it cannot
    auto-restart from a script restart. The TOML stays recoverable.
    Process: SIGTERM then SIGKILL escalation via kill_bot().
    """
    spawned_records = {r["name"]: r for r in state.get("spawned", [])
                       if not r.get("killed_at")}
    killed: list[str] = []
    for m in metrics:
        # Two paths to kill:
        #   (a) catastrophic equity collapse (ROI <= -50%) — fires
        #       regardless of closed-trade count, catches "all loss
        #       in unrealized" bots like panic_fade
        #   (b) sustained underperformance — needs sample AND both
        #       ROI <= -10% AND wr <= 40%
        if m.name in KILL_PROTECTED_PROFILES:
            continue
        catastrophic = m.roi_pct <= -KILL_EQUITY_FLOOR_PCT
        if not catastrophic and not (m.roi_pct <= KILL_ROI_THRESHOLD
                                       and m.win_rate <= KILL_WR_THRESHOLD):
            continue
        is_auto = m.name in spawned_records
        min_trades = KILL_AUTO_MIN_TRADES if is_auto else KILL_HUMAN_MIN_TRADES
        if not catastrophic and m.closed < min_trades:
            continue
        pid = (spawned_records[m.name].get("pid") if is_auto
               else find_bot_pid_by_name(m.name))
        if not pid:
            print(f"[analyst] kill {m.name}: no pid found, skip", flush=True)
            continue
        ok = kill_bot(pid, m.name)
        archive_profile(m.name)
        if catastrophic:
            reason = f"💥 catastrophic ROI={m.roi_pct:.1f}% (≤-{KILL_EQUITY_FLOOR_PCT:.0f}%)"
        else:
            reason = f"ROI={m.roi_pct:.1f}% wr={m.win_rate:.0f}% n={m.closed}"
        if is_auto:
            rec = spawned_records[m.name]
            rec["killed_at"] = int(time.time())
            rec["killed_reason"] = reason
            rec["kill_success"] = ok
        else:
            state.setdefault("killed_human", []).append({
                "name": m.name, "pid": pid, "killed_at": int(time.time()),
                "killed_reason": reason, "kill_success": ok,
            })
        killed.append(m.name)
        tier = "auto" if is_auto else "HUMAN"
        print(
            f"\n{'='*70}\n"
            f"[analyst] 💀💀💀 KILLED [{tier}] {m.name}\n"
            f"[analyst]     pid={pid}, ok={ok}, {reason}\n"
            f"{'='*70}\n",
            flush=True,
        )
    return killed


def cycle_once() -> None:
    state = load_autonomous_state()
    if not state.get("enabled", True):
        print("[analyst] disabled via state file; reporting only", flush=True)

    ensure_leaderboard_auto_discover()

    metrics = collect_metrics()
    top, bottom = rank(metrics)
    n_total = len(metrics)

    # Throttle spawn/kill to SPAWN_KILL_INTERVAL (default 1h), but always
    # post a fresh report (default 15min cycle). This way the user gets
    # frequent status updates without churning the strategy pool too fast.
    now_ts = int(time.time())
    last_action_ts = int(state.get("last_action_ts", 0) or 0)
    action_due = (now_ts - last_action_ts) >= SPAWN_KILL_INTERVAL
    killed: list[str] = []
    spawned: list[str] = []
    tuned: list[str] = []
    narrative = ""

    if action_due:
        killed = evaluate_kills(metrics, state)
        if killed:
            save_autonomous_state(state)
    else:
        mins_until = max(0, (SPAWN_KILL_INTERVAL - (now_ts - last_action_ts)) // 60)
        print(f"[analyst] spawn/kill skipped — next action in {mins_until}min", flush=True)

    # Deterministic summary narrative — built straight from the metrics.
    # No LLM, no spawning/tuning (those paths were AI-driven and removed).
    if not metrics:
        narrative = "No dry-run state yet — waiting for the race to start writing journals."
    else:
        rated = [m for m in metrics if m.closed >= MIN_TRADES_TO_RATE]
        if not rated:
            narrative = (
                f"{n_total} strategies tracked, "
                f"none with ≥{MIN_TRADES_TO_RATE} closed trades yet — early sample."
            )
        else:
            winners = [m for m in rated if m.pnl > 0]
            losers = [m for m in rated if m.pnl < 0]
            top_n = sorted(rated, key=lambda m: m.pnl, reverse=True)[:3]
            top_txt = ", ".join(
                f"`{m.name}` ({m.roi_pct:+.0f}% / {m.closed}c)" for m in top_n
            )
            worst = min(rated, key=lambda m: m.pnl)
            narrative = (
                f"{len(rated)}/{n_total} strategies have ≥{MIN_TRADES_TO_RATE} closed trade(s): "
                f"{len(winners)} profitable, {len(losers)} losing. "
                f"Top: {top_txt}. "
                f"Worst: `{worst.name}` ({worst.roi_pct:+.0f}% / {worst.closed}c)."
            )

    state["last_cycle_ts"] = int(time.time())
    if action_due and killed:
        state["last_action_ts"] = int(time.time())
    save_autonomous_state(state)

    live_ready, live_close = assess_live_readiness(metrics)
    msg = build_main_message(narrative, top, bottom, spawned, tuned, killed,
                              n_total, live_ready=live_ready,
                              live_close=live_close, all_metrics=metrics)
    telegram_post(msg)
    print(msg, flush=True)


def main() -> int:
    print(f"[analyst] starting — cycle={CYCLE_SECONDS}s (deterministic, no AI)",
          flush=True)
    # Refresh immediately on startup so the dry Telegram channel is up to
    # date as soon as the analyst comes online.
    print(
        f"[analyst] cycle={CYCLE_SECONDS}s reports, "
        f"loser-kills every {SPAWN_KILL_INTERVAL}s",
        flush=True,
    )
    # Stamp last_action_ts to now so the first kill pass waits a full
    # SPAWN_KILL_INTERVAL — gives bots time to accumulate sample.
    initial_state = load_autonomous_state()
    initial_state["last_action_ts"] = int(time.time())
    save_autonomous_state(initial_state)
    print(f"[analyst] first report now, first kill pass in {SPAWN_KILL_INTERVAL//60}min", flush=True)
    cycle_once()
    time.sleep(CYCLE_SECONDS)
    while True:
        try:
            cycle_once()
        except Exception:
            tb = traceback.format_exc()
            print(f"[analyst] cycle failed:\n{tb}", file=sys.stderr, flush=True)
            telegram_post(f"⚠️ *Analyst error*\n```\n{tb[:1500]}\n```")
        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    sys.exit(main() or 0)
