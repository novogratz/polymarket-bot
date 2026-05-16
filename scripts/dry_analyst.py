#!/usr/bin/env python3
"""Autonomous dry-run analyst sidecar.

Runs alongside the 62-bot dry race. Every 15 minutes:
  1. Reads per-strategy state + journal from data/dry_runs/
  2. Computes leaderboard (PnL, win-rate, sample size, avg win/loss)
  3. Calls Claude CLI for a narrative + optional new-strategy proposal
  4. Spawns new dry-run bot if proposal accepted (TOML-only, additive)
  5. Kills auto-spawned bots that are clearly losing (≥KILL_MIN_TRADES
     closed, ROI ≤ KILL_ROI_THRESHOLD, win_rate ≤ KILL_WR_THRESHOLD)
  6. Posts a Markdown leaderboard + commentary to Telegram

Hard rules (see MEMORY.md feedback_autonomous_analyst_override.md):
  - Dry-run only. Never touches live profiles or live ledger.
  - Kill scope: ONLY auto_* strategies the analyst itself spawned.
    NEVER kills or modifies the 62 human-curated bots.
  - TOML-only spawning: new strategies must reuse an existing mode.
  - Hard caps: 1 spawn/cycle, MAX_BOTS_TOTAL ceiling, kill-switch.
  - All spawned profile names prefixed `auto_` for traceability.

Kill switch: write `{"enabled": false}` to data/autonomous_state.json
to halt new spawns. Existing auto bots keep running; reporting continues.

Cost note: ~$0.05-0.30 per Claude call × 96 calls/day ≈ $5-30/day.
Adjust CYCLE_SECONDS or set enabled=false in autonomous_state.json
if cost matters.
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

CYCLE_SECONDS = int(os.environ.get("ANALYST_CYCLE_SECONDS", "900"))   # 15 min
MAX_BOTS_TOTAL = int(os.environ.get("ANALYST_MAX_BOTS", "100"))
SPAWN_PREFIX = "auto_"
CLAUDE_TIMEOUT_SECONDS = 240
MIN_TRADES_TO_RATE = 3  # require ≥3 closed trades before a strategy is "rated"

# Kill criteria for auto-spawned bots only. Conservative: must have enough
# sample AND be clearly losing on both PnL and win-rate.
KILL_MIN_TRADES = int(os.environ.get("ANALYST_KILL_MIN_TRADES", "10"))
KILL_ROI_THRESHOLD = float(os.environ.get("ANALYST_KILL_ROI", "-10.0"))   # ROI% ≤ -10
KILL_WR_THRESHOLD = float(os.environ.get("ANALYST_KILL_WR", "35.0"))      # win_rate% ≤ 35


@dataclass
class StratMetrics:
    name: str
    cash: float
    equity: float
    pnl: float
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
    """Read starting_cash from the profile TOML (defaults to 100)."""
    profile = PROFILES_DIR / f"{name}.toml"
    if not profile.exists():
        return 100.0
    try:
        text = profile.read_text()
        m = re.search(r"^starting_cash\s*=\s*([\d.]+)", text, re.M)
        return float(m.group(1)) if m else 100.0
    except Exception:
        return 100.0


def collect_metrics() -> list[StratMetrics]:
    """Walk data/dry_runs/* and compute per-strategy metrics."""
    if not DRY_RUNS_DIR.exists():
        return []
    out: list[StratMetrics] = []
    for run_dir in sorted(DRY_RUNS_DIR.iterdir()):
        if not run_dir.is_dir():
            continue
        name = run_dir.name
        state_file = run_dir / "state.json"
        journal_file = run_dir / "journal.jsonl"
        if not state_file.exists():
            continue
        try:
            state = json.loads(state_file.read_text())
        except Exception:
            continue
        cash = float(state.get("cash") or 0.0)
        positions = state.get("positions", []) or []
        open_positions = [p for p in positions if p.get("status") == "open"]
        invested = sum(
            float(p.get("size_usd") or p.get("notional_usd") or 0.0)
            for p in open_positions
        )
        equity = cash + invested
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
                    if entry.get("event") != "position_closed":
                        continue
                    pnl_t = float(entry.get("realized_pnl_usd") or 0.0)
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
        out.append(
            StratMetrics(
                name=name,
                cash=cash,
                equity=equity,
                pnl=pnl,
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
# Claude CLI
# ────────────────────────────────────────────────────────────────────


PROMPT_TEMPLATE = """You analyse a Polymarket prediction-market dry-run race. {n_total} strategies running, {n_rated} have ≥{min_trades} closed trades.

## Per-strategy metrics (sorted by PnL)
{leaderboard_table}

## Your job
1. **Narrative (3-5 lines max).** Why are the top 3 winning? Why are the bottom 3 losing? Be specific about what filter combination or exit logic likely drove the result. Avoid generic statements.

2. **Optional new strategy proposal.** If you see a clear pattern that suggests a tunable variant of a winner, propose ONE new TOML profile. Otherwise reply `NO_PROPOSAL`.

If you propose, output a TOML block fenced like this:
```toml
# auto_<short_descriptive_name> — derived from <parent_strategy>
# Hypothesis: <one sentence why this should outperform parent>
[run]
starting_cash = 100.0
mode = "<existing mode name, copy from parent>"

[sizing]
...
[race]
...
[telemetry]
quiet = true
auto_interval_seconds = <staggered: pick a value 30-90 not used by others>
stdout_heartbeat_minutes = 15
```

Constraints (HARD — your output will be rejected if violated):
- The new profile name MUST start with `auto_`.
- The `mode` MUST be one of the existing modes (no new Python selectors).
- ONLY parameter variants. Don't invent new TOML sections.
- starting_cash MUST be 100.0.
- max_hours = 4.0 hard rule (4h-only).

Existing modes you can reuse: smart_money (default — leave mode unset), edge, news, mirror, hybrid_smart_money, smart_wallet_consensus, whale_entry_detection, wallet_cluster_correlation, early_momentum_detection, liquidity_vacuum_breakout, mean_reversion_fade, range_channel_trading, aggressive_buyer_detection, orderbook_imbalance, late_momentum_chase, weak_holder_flush, weak_holder_flush_inverse, pmlepgm_counter_panic_fade, pm_le_pgm_weak_holder_flush_inverse, championdumonde_breakout, late_favorite, panic_fade, underdog, favorite, contrarian, random, multi_signal_consensus, claude_resolution_sniper, claude_endgame_sweep, claude_blue_chip, claude_balanced_mid, claude_late_pump, claude_strong_breakout, claude_extreme_consensus, claude_resolution_clock, etc.

Existing parent strategies to draw inspiration from (don't re-propose): {existing_auto_names}

Output format: narrative first, then either the toml block or `NO_PROPOSAL`."""


def call_claude(prompt: str) -> str:
    """Invoke `claude -p` in non-interactive mode. Returns stdout."""
    try:
        result = subprocess.run(
            ["claude", "--dangerously-skip-permissions", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return f"[claude error rc={result.returncode}]\n{result.stderr[:500]}"
        return result.stdout
    except subprocess.TimeoutExpired:
        return "[claude timeout]"
    except Exception as exc:
        return f"[claude exception: {type(exc).__name__}: {exc}]"


def parse_response(text: str) -> tuple[str, str | None, str | None]:
    """Return (narrative, toml_content, profile_name) — toml_content/name may be None."""
    # Strip toml block
    toml_match = re.search(r"```toml\s*(.*?)```", text, re.S)
    if not toml_match:
        return text.strip(), None, None
    toml_body = toml_match.group(1).strip()
    # Extract profile name from header comment `# auto_<name>`
    name_match = re.search(r"^#\s*(auto_[a-z0-9_]+)", toml_body, re.M)
    if not name_match:
        return text.strip(), None, None
    narrative = (text[: toml_match.start()] + text[toml_match.end():]).strip()
    return narrative, toml_body, name_match.group(1)


def validate_proposal(name: str, toml_body: str) -> str | None:
    """Return error string if invalid, None if OK."""
    if not name.startswith(SPAWN_PREFIX):
        return f"name must start with `{SPAWN_PREFIX}`"
    if (PROFILES_DIR / f"{name}.toml").exists():
        return f"profile {name} already exists"
    if "starting_cash" not in toml_body or "starting_cash = 100" not in toml_body:
        return "starting_cash must be 100.0"
    # Reject anything that looks like Python (defensive). Match only at
    # start-of-line so the words "from"/"import" in comments are fine.
    if re.search(r"^\s*(import|from|def|class)\s+\w+", toml_body, re.M):
        return "contains Python-like syntax"
    # Quick TOML parse check
    try:
        import tomllib
        tomllib.loads(toml_body)
    except Exception as exc:
        return f"TOML parse error: {exc}"
    return None


# ────────────────────────────────────────────────────────────────────
# Spawning
# ────────────────────────────────────────────────────────────────────


def write_profile(name: str, body: str) -> Path:
    path = PROFILES_DIR / f"{name}.toml"
    path.write_text(body)
    return path


def spawn_bot(name: str) -> int | None:
    """Launch a dry-run bot in the background. Returns PID.

    NOTE: We deliberately keep the child in the parent's process group
    (no start_new_session/setsid) so Ctrl+C on the main race script
    propagates and kills these too. No orphan bots after shutdown.
    """
    env = os.environ.copy()
    env["POLYMARKET_QUIET"] = "1"
    env["POLYMARKET_SUPPRESS_BUY_LOGS"] = "1"
    # Tee output into the same logs dir so the user can see it
    log_dir = REPO_ROOT / "data" / "dry_runs" / name
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "spawn_stdout.log"
    log_fh = log_path.open("ab", buffering=0)
    proc = subprocess.Popen(
        [
            "uv", "run", "pmbot", "auto-loop",
            "--dry-run", "--profile", name, "--run", name,
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )
    return proc.pid


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
    # Telegram messages capped at 4096 chars
    payload = json.dumps({
        "chat_id": chat,
        "text": text[:4000],
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
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
                        killed: list[str], n_total: int) -> str:
    stamp = time.strftime("%H:%M UTC", time.gmtime())
    parts = [f"🤖 *AUTONOMOUS REPORT* · {stamp}",
             f"_{n_total} strategies running, {len(top)+len(bottom)} rated (≥{MIN_TRADES_TO_RATE} closed trades)_",
             ""]
    if top:
        parts.append("*🏆 Top 5 (by PnL)*")
        for i, m in enumerate(top, 1):
            parts.append(f"  {i}. `{m.name}` {m.pnl:+.2f}$ ({m.win_rate:.0f}% wr, {m.closed} closed)")
        parts.append("")
    if bottom:
        parts.append("*📉 Bottom 3*")
        for m in bottom:
            parts.append(f"  • `{m.name}` {m.pnl:+.2f}$ ({m.win_rate:.0f}% wr, {m.closed} closed)")
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
    if killed:
        parts.append("*💀 Killed (auto-spawned underperformers)*")
        for name in killed:
            parts.append(f"  • `{name}`")
    return "\n".join(parts)


# ────────────────────────────────────────────────────────────────────
# Main loop
# ────────────────────────────────────────────────────────────────────


def existing_auto_strategies() -> list[str]:
    return sorted(p.stem for p in PROFILES_DIR.glob(f"{SPAWN_PREFIX}*.toml"))


def total_running_bots() -> int:
    """Count dry_runs subdirs as a rough proxy for running bots."""
    if not DRY_RUNS_DIR.exists():
        return 0
    return sum(1 for p in DRY_RUNS_DIR.iterdir() if p.is_dir())


def evaluate_kills(metrics: list[StratMetrics], state: dict) -> list[str]:
    """Identify auto-spawned bots that are clearly losing, kill them.

    ONLY kills bots in state['spawned'] (auto-generated by this analyst).
    NEVER touches human-curated bots.

    Returns names of killed bots.
    """
    spawned_records = {r["name"]: r for r in state.get("spawned", [])
                       if not r.get("killed_at")}
    killed: list[str] = []
    for m in metrics:
        if m.name not in spawned_records:
            continue  # not auto-spawned — protected
        if m.closed < KILL_MIN_TRADES:
            continue  # not enough sample
        if m.roi_pct <= KILL_ROI_THRESHOLD and m.win_rate <= KILL_WR_THRESHOLD:
            rec = spawned_records[m.name]
            pid = rec.get("pid")
            ok = kill_bot(pid, m.name) if pid else False
            archive_profile(m.name)
            rec["killed_at"] = int(time.time())
            rec["killed_reason"] = f"ROI={m.roi_pct:.1f}% wr={m.win_rate:.0f}% n={m.closed}"
            rec["kill_success"] = ok
            killed.append(m.name)
            print(f"[analyst] killed {m.name} (pid={pid}, ok={ok}): {rec['killed_reason']}",
                  flush=True)
    return killed


def cycle_once() -> None:
    state = load_autonomous_state()
    if not state.get("enabled", True):
        print("[analyst] disabled via state file; reporting only", flush=True)

    metrics = collect_metrics()
    top, bottom = rank(metrics)
    n_total = len(metrics)

    # Kill auto-spawned underperformers first (frees up bot slot for new spawn)
    killed = evaluate_kills(metrics, state)
    if killed:
        save_autonomous_state(state)

    narrative = ""
    spawned: list[str] = []

    if top:
        existing_autos = existing_auto_strategies()
        prompt = PROMPT_TEMPLATE.format(
            n_total=n_total,
            n_rated=len(top) + len(bottom),
            min_trades=MIN_TRADES_TO_RATE,
            leaderboard_table=fmt_leaderboard(top + bottom),
            existing_auto_names=", ".join(existing_autos) or "(none yet)",
        )
        response = call_claude(prompt)
        narrative, toml_body, name = parse_response(response)

        if (state.get("enabled", True)
                and toml_body and name
                and total_running_bots() < MAX_BOTS_TOTAL):
            err = validate_proposal(name, toml_body)
            if err:
                narrative += f"\n\n⚠ proposal rejected: {err}"
            else:
                write_profile(name, toml_body)
                pid = spawn_bot(name)
                spawned.append(name)
                state.setdefault("spawned", []).append({
                    "name": name, "pid": pid, "ts": int(time.time()),
                })
                save_autonomous_state(state)
                print(f"[analyst] spawned {name} (pid={pid})", flush=True)
    else:
        narrative = f"No strategy has ≥{MIN_TRADES_TO_RATE} closed trades yet — waiting for sample."

    state["last_cycle_ts"] = int(time.time())
    save_autonomous_state(state)

    msg = build_main_message(narrative, top, bottom, spawned, killed, n_total)
    telegram_post(msg)
    print(msg, flush=True)


def main() -> int:
    print(f"[analyst] starting — cycle={CYCLE_SECONDS}s, max_bots={MAX_BOTS_TOTAL}",
          flush=True)
    # First cycle after 60s so dry race has time to write initial state
    time.sleep(60)
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
