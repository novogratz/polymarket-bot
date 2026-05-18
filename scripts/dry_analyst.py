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

CYCLE_SECONDS = int(os.environ.get("ANALYST_CYCLE_SECONDS", "300"))   # 5 min (was 15)
MAX_BOTS_TOTAL = int(os.environ.get("ANALYST_MAX_BOTS", "150"))
MAX_SPAWNS_PER_CYCLE = int(os.environ.get("ANALYST_MAX_SPAWNS", "3"))   # was 1
MAX_TUNES_PER_CYCLE = int(os.environ.get("ANALYST_MAX_TUNES", "2"))     # in-place reroll
SPAWN_PREFIX = "auto_"
CLAUDE_TIMEOUT_SECONDS = 240
MIN_TRADES_TO_RATE = 2  # was 3 — get insights faster

# Kill criteria for auto-spawned bots only. Conservative: must have enough
# sample AND be clearly losing on both PnL and win-rate.
KILL_MIN_TRADES = int(os.environ.get("ANALYST_KILL_MIN_TRADES", "8"))    # was 10
KILL_ROI_THRESHOLD = float(os.environ.get("ANALYST_KILL_ROI", "-10.0"))   # ROI% ≤ -10
KILL_WR_THRESHOLD = float(os.environ.get("ANALYST_KILL_WR", "40.0"))      # was 35


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


PROMPT_TEMPLATE = """You are running a fully autonomous Polymarket strategy race. {n_total} strategies running, {n_rated} rated (≥{min_trades} closed trades). Your job is to GENERATE new strategies aggressively and keep the race exploring.

## Per-strategy metrics (sorted by PnL)
{leaderboard_table}

## Existing auto-spawned strategies (don't dupe these)
{existing_auto_names}

## YOUR JOB — REQUIRED ACTIONS

1. **Narrative (3-6 lines).** What's working? What's not? Be specific about parameter combinations (cohort tightness, momentum thresholds, exit timing). No generic statements.

2. **Propose 1-3 new strategies.** ALWAYS propose at least 1 unless the data is identical to your last cycle. Each must explore a DIFFERENT hypothesis. Bias toward:
   - Variants of profitable strategies with one parameter shifted
   - Inverse/contrarian versions of clear losers
   - Combinations of two winners' features
   - Aggressive risk variants when slow winners exist

3. **Optionally tune existing auto_ strategies** that have ≥{min_trades} trades but ROI between -10% and 0% (borderline). Propose a parameter shift to test.

## OUTPUT FORMAT — strict

For each new strategy, emit a TOML fenced block:
```toml
# auto_<short_name> — derived from <parent>
# Hypothesis: <one sentence — what specific param/combo change you're testing>
[run]
starting_cash = 20.0
mode = "<existing mode name, copy from parent>"

[sizing]
...
[race]
max_hours = 4.0
...
[telemetry]
quiet = true
auto_interval_seconds = <staggered 30-180>
stdout_heartbeat_minutes = 15
```

For each tune action, emit:
```tune
target: auto_<existing_name>
hypothesis: <why this change>
```
followed by a full new TOML block (same format as new strategy, with a DIFFERENT auto_ name).

## HARD CONSTRAINTS — your output is rejected if violated
- New profile name MUST start with `auto_` and be unique (not in existing list above).
- `mode` MUST be one of: smart_money (default — leave unset), edge, news, mirror, hybrid_smart_money, smart_wallet_consensus, whale_entry_detection, wallet_cluster_correlation, early_momentum_detection, liquidity_vacuum_breakout, mean_reversion_fade, range_channel_trading, aggressive_buyer_detection, orderbook_imbalance, late_momentum_chase, weak_holder_flush, weak_holder_flush_inverse, pmlepgm_counter_panic_fade, pm_le_pgm_weak_holder_flush_inverse, championdumonde_breakout, late_favorite, panic_fade, underdog, favorite, contrarian, random, multi_signal_consensus, claude_resolution_sniper, claude_endgame_sweep, claude_blue_chip, claude_balanced_mid, claude_late_pump, claude_strong_breakout, claude_extreme_consensus, claude_resolution_clock, probability_drift, resolution_compression, liquidity_absorption, momentum_exhaustion_reversal, micro_scalping, kzerlepgm_ultimatestrategy.
- ONLY parameter variants. No new TOML sections, no Python in TOML.
- starting_cash MUST be 20.0.
- 4h-only rule: max_hours = 4.0 in [race], max_hours_to_close = 4.0 in [filters] for smart_money mode.

Output: narrative first, then TOML/tune blocks separated by blank lines. Be decisive."""


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


def parse_response(text: str) -> tuple[str, list[dict]]:
    """Parse Claude's response into a narrative + list of proposed actions.

    Each action is: {'kind': 'spawn'|'tune', 'name': str, 'body': str,
                     'target': str | None}
    """
    actions: list[dict] = []
    # Pull all toml blocks
    for m in re.finditer(r"```toml\s*(.*?)```", text, re.S):
        body = m.group(1).strip()
        name_match = re.search(r"^#\s*(auto_[a-z0-9_]+)", body, re.M)
        if not name_match:
            continue
        actions.append({"kind": "spawn", "name": name_match.group(1),
                        "body": body, "target": None, "span": m.span()})
    # Pull all tune blocks (each followed by a toml block)
    for m in re.finditer(r"```tune\s*(.*?)```", text, re.S):
        tune_body = m.group(1)
        target_match = re.search(r"target:\s*(auto_[a-z0-9_]+)", tune_body)
        if not target_match:
            continue
        # Find the next toml block after this tune block
        after = text[m.end():]
        toml_match = re.search(r"```toml\s*(.*?)```", after, re.S)
        if not toml_match:
            continue
        body = toml_match.group(1).strip()
        name_match = re.search(r"^#\s*(auto_[a-z0-9_]+)", body, re.M)
        if not name_match:
            continue
        # Mark this toml as part of a tune (not a separate spawn)
        for a in actions:
            if a["name"] == name_match.group(1):
                a["kind"] = "tune"
                a["target"] = target_match.group(1)
                break
    # Narrative = everything outside the code blocks
    narrative = re.sub(r"```(?:toml|tune).*?```", "", text, flags=re.S).strip()
    return narrative, actions


def validate_proposal(name: str, toml_body: str) -> str | None:
    """Return error string if invalid, None if OK."""
    if not name.startswith(SPAWN_PREFIX):
        return f"name must start with `{SPAWN_PREFIX}`"
    if (PROFILES_DIR / f"{name}.toml").exists():
        return f"profile {name} already exists"
    if "starting_cash" not in toml_body or "starting_cash = 20" not in toml_body:
        return "starting_cash must be 20.0"
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
    # Full schema validation: load_profile catches unknown keys/sections.
    # Write to a temp path, validate, delete — if invalid, the analyst
    # never publishes a broken profile.
    import tempfile
    try:
        from polymarket_bot.profiles import load_profile  # type: ignore
    except Exception:
        # If we can't import the validator, fall back to TOML-parse-only.
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as tmp:
        tmp.write(toml_body)
        tmp_path = tmp.name
    try:
        load_profile(Path(tmp_path))
    except Exception as exc:
        return f"schema validation: {exc}"
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
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
                        tuned: list[str], killed: list[str], n_total: int) -> str:
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
    if tuned:
        parts.append("*🔧 Tuned (in-place reroll)*")
        for swap in tuned:
            parts.append(f"  • `{swap}`")
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
    tuned: list[str] = []

    if top or bottom or n_total >= 5:
        existing_autos = existing_auto_strategies()
        prompt = PROMPT_TEMPLATE.format(
            n_total=n_total,
            n_rated=len(top) + len(bottom),
            min_trades=MIN_TRADES_TO_RATE,
            leaderboard_table=fmt_leaderboard(top + bottom or metrics[:10]),
            existing_auto_names=", ".join(existing_autos) or "(none yet)",
        )
        response = call_claude(prompt)
        narrative, actions = parse_response(response)

        if state.get("enabled", True):
            spawn_count = tune_count = 0
            for action in actions:
                if total_running_bots() >= MAX_BOTS_TOTAL:
                    narrative += f"\n\n⚠ bot cap {MAX_BOTS_TOTAL} reached; stopping spawns"
                    break
                if action["kind"] == "spawn" and spawn_count >= MAX_SPAWNS_PER_CYCLE:
                    continue
                if action["kind"] == "tune" and tune_count >= MAX_TUNES_PER_CYCLE:
                    continue
                err = validate_proposal(action["name"], action["body"])
                if err:
                    narrative += f"\n⚠ {action['name']} rejected: {err}"
                    continue
                # For tune: kill the target auto_ bot first
                if action["kind"] == "tune" and action["target"]:
                    target_record = next(
                        (r for r in state.get("spawned", [])
                         if r["name"] == action["target"] and not r.get("killed_at")),
                        None,
                    )
                    if target_record:
                        kill_bot(target_record.get("pid"), action["target"])
                        archive_profile(action["target"])
                        target_record["killed_at"] = int(time.time())
                        target_record["killed_reason"] = f"tuned → {action['name']}"
                # Spawn the new one
                write_profile(action["name"], action["body"])
                pid = spawn_bot(action["name"])
                rec = {"name": action["name"], "pid": pid, "ts": int(time.time())}
                if action["kind"] == "tune":
                    rec["tuned_from"] = action["target"]
                    tuned.append(f"{action['target']}→{action['name']}")
                    tune_count += 1
                else:
                    spawned.append(action["name"])
                    spawn_count += 1
                state.setdefault("spawned", []).append(rec)
                save_autonomous_state(state)
                print(f"[analyst] {action['kind']} {action['name']} (pid={pid})", flush=True)
    elif not metrics:
        narrative = "No dry-run state yet — waiting for the race to start writing journals."
    else:
        narrative = f"Only {n_total} strategies tracked, none rated yet (need ≥{MIN_TRADES_TO_RATE} closed trades)."

    state["last_cycle_ts"] = int(time.time())
    save_autonomous_state(state)

    msg = build_main_message(narrative, top, bottom, spawned, tuned, killed, n_total)
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
