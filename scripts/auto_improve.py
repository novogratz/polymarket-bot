#!/usr/bin/env python3
"""Autonomous strategy self-improvement loop — tunes the LIVE grinder profile.

The "agentic self-improvement" engine. Each run it asks the **Claude Code CLI**
to propose a small improvement to the live strategy, applies it, validates it
(TOML parse + hard bounds + the full unit-test suite), opens a pull request with
`gh`, and auto-merges once CI is green.

It updates the LIVE profile (configs/profiles/grinder.toml) on purpose — but it
is fenced so it can only ever touch the *exit / sizing* knobs, never the entry
selection that produces the win rate:

  * ENTRY IS FROZEN. The price band, spread, time window, liquidity/volume,
    day-change and momentum filters are NOT in TUNABLE, so the agent can never
    change which bets are taken — the win rate is protected.
  * NO STOP-LOSS, EVER. sl_pct / stop_loss_pct are never tunable; the agent
    cannot introduce a stop-loss (honours "never sell losing positions").
  * Only the whitelisted exit/sizing keys move — take-profit, position size,
    concurrency, winner-exit threshold, max hold — each clamped to a hard range.
  * Only configs/profiles/grinder.toml is writable. Touching any other file
    (other profiles, .env, source code) aborts the run.
  * Tests must pass before a PR is opened. CI must be green before auto-merge.

Switches (defaults chosen per the owner's request):
  AUTO_IMPROVE_ENABLED=1      master gate (required).
  AUTO_IMPROVE_USE_LLM=1      use the claude CLI to propose (default ON).
  AUTO_IMPROVE_AUTOMERGE=1    self-merge the PR when CI is green (default ON).

Run:
    AUTO_IMPROVE_ENABLED=1 uv run python scripts/auto_improve.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:  # py311+ stdlib; tomli fallback for older
    import tomllib  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

REPO_ROOT = Path(__file__).resolve().parent.parent
# The LIVE primary profile. This is the file the agent improves.
TARGET = REPO_ROOT / "configs" / "profiles" / "grinder.toml"
ALLOWED_WRITE = {str(TARGET.relative_to(REPO_ROOT))}

# Whitelisted "section.key" knobs and their HARD safe bounds. EXIT + SIZING
# only — entry/selection keys are deliberately absent so the win rate is frozen.
# stop-loss keys are absent so a stop-loss can never be introduced.
TUNABLE: dict[str, tuple[float, float]] = {
    "race.tp_pct": (0.05, 1.0),                    # take-profit (1.0 = ride to resolution)
    "race.stake_pct": (0.10, 0.60),                # position size per trade
    "race.max_orders_per_tick": (1, 5),            # concurrency / sizing spread
    # PINNED at 0.99 (user rule 2026-06-10): winners sell at a real 0.99 book
    # bid or ride to on-chain settlement at 1.00 — the tuner must never lower
    # the winner exit back into 0.95-0.98 territory.
    "race.resolved_exit_threshold": (0.99, 0.99),
    "exits.max_hold_hours": (1.0, 4.5),            # max-hold backstop
}
INT_KEYS = {"race.max_orders_per_tick"}

# Frozen — listed so a post-apply audit can assert these never moved. They are
# simply absent from TUNABLE, so _apply never writes them.
FROZEN_ENTRY = [
    "race.min_price", "race.max_price", "race.max_spread", "race.max_hours",
    "race.min_liquidity_usd", "race.min_volume_24h_usd",
]


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True,
                          encoding="utf-8", errors="replace", **kw)


def _die(msg: str, code: int = 1) -> None:
    print(f"[auto-improve] ABORT: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def _on(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip() in {"1", "true", "True"}


def _preflight() -> None:
    if not _on("AUTO_IMPROVE_ENABLED"):
        _die("AUTO_IMPROVE_ENABLED is not set — refusing to run.", code=2)
    if not TARGET.is_file():
        _die(f"live profile missing: {TARGET}")
    if _run(["git", "status", "--porcelain"]).stdout.strip():
        _die("working tree is dirty; commit or stash first.")


def _toml() -> dict:
    return tomllib.loads(TARGET.read_text(encoding="utf-8"))


def _get(data: dict, dotted: str):
    sect, key = dotted.split(".", 1)
    return data.get(sect, {}).get(key)


def _read_params(data: dict | None = None) -> dict[str, float]:
    data = data if data is not None else _toml()
    return {k: _get(data, k) for k in TUNABLE if _get(data, k) is not None}


def _clamp(key: str, val: float) -> float:
    lo, hi = TUNABLE[key]
    val = max(lo, min(hi, float(val)))
    return int(round(val)) if key in INT_KEYS else round(val, 4)


def _propose(current: dict[str, float]) -> dict[str, float]:
    if _on("AUTO_IMPROVE_USE_LLM", "1"):
        llm = _propose_llm(current)
        if llm:
            return {k: _clamp(k, v) for k, v in llm.items() if k in TUNABLE}
    return _propose_hillclimb(current)


def _propose_llm(current: dict[str, float]) -> dict[str, float] | None:
    """Ask the Claude Code CLI for a bounded exit/sizing delta. Offline (not in
    the live trade loop); output is still hard-clamped. None on any failure."""
    metrics = _recent_performance()
    prompt = (
        "You autonomously tune a live Polymarket 'grinder' strategy (buy "
        "heavy-favorite binaries, ride toward resolution). The ENTRY selection "
        "is FROZEN and must not be touched — only optimise EXIT and SIZING. "
        "Never propose a stop-loss. Goal: improve risk-adjusted PnL while "
        "preserving the win rate.\n\n"
        f"current = {json.dumps(current)}\n"
        f"allowed_ranges = {json.dumps({k: list(v) for k, v in TUNABLE.items()})}\n"
        f"recent_performance = {json.dumps(metrics)}\n\n"
        "Return ONLY a JSON object of changed keys -> numeric values (a subset "
        "of the allowed keys). No prose."
    )
    try:
        proc = _run(["claude", "-p", prompt], timeout=180)
        out = (proc.stdout or "").strip()
        s, e = out.find("{"), out.rfind("}")
        if s == -1 or e <= s:
            return None
        raw = json.loads(out[s:e + 1])
        return {k: float(v) for k, v in raw.items()
                if k in TUNABLE and isinstance(v, (int, float))}
    except Exception as exc:  # noqa: BLE001
        print(f"[auto-improve] claude propose failed, using hill-climb: {exc}",
              file=sys.stderr, flush=True)
        return None


def _propose_hillclimb(current: dict[str, float]) -> dict[str, float]:
    """Deterministic fallback when the CLI is unavailable. Nudges one exit/
    sizing knob by a small step, seeded by the day so it doesn't thrash."""
    import random
    rng = random.Random(int(time.time() // 86400))
    out = dict(current)
    knob = rng.choice(list(current) or list(TUNABLE))
    step = {
        "race.tp_pct": 0.05, "race.stake_pct": 0.05,
        "race.max_orders_per_tick": 1, "race.resolved_exit_threshold": 0.01,
        "exits.max_hold_hours": 0.5,
    }.get(knob, 0.05)
    base = current.get(knob, TUNABLE[knob][0])
    out[knob] = _clamp(knob, base + rng.choice([-1, 1]) * step)
    return out


def _recent_performance() -> dict:
    """Cheap, read-only PnL summary from the realized-trade cache for context."""
    path = REPO_ROOT / "data" / "realized_trade_cache.jsonl"
    n = wins = 0
    pnl = 0.0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            v = r.get("realized_pnl", r.get("realized_pnl_usd"))
            if v is None:
                continue
            n += 1
            pnl += float(v)
            wins += 1 if float(v) > 0 else 0
    except Exception:  # noqa: BLE001
        pass
    return {"closed": n, "win_rate": round(wins / n, 3) if n else None,
            "total_pnl": round(pnl, 2)}


def _apply(new: dict[str, float]) -> list[str]:
    """Write new values into their correct [section]. Section-aware so a key
    present in several sections (e.g. max_orders_per_tick) is not crossed.
    Any inline comment on the line is preserved."""
    lines = TARGET.read_text(encoding="utf-8").splitlines()
    section = ""
    pos: dict[str, int] = {}
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1]
        elif "=" in s and not s.startswith("#"):
            key = s.split("=", 1)[0].strip()
            pos.setdefault(f"{section}.{key}", i)

    changed: list[str] = []
    for dotted, val in new.items():
        i = pos.get(dotted)
        if i is None:
            continue
        key = dotted.split(".", 1)[1]
        fmt = str(int(val)) if dotted in INT_KEYS else f"{float(val):.3f}".rstrip("0").rstrip(".")
        rhs = lines[i].split("=", 1)[1]
        old_val = rhs.split("#", 1)[0].strip()
        inline = f"  #{rhs.split('#', 1)[1]}" if "#" in rhs else ""
        new_line = f"{key} = {fmt}{inline}"
        if new_line != lines[i]:
            lines[i] = new_line
            changed.append(f"{dotted}: {old_val} -> {fmt}")
    TARGET.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def _audit_frozen(before: dict) -> None:
    """Hard stop if any frozen entry key or a stop-loss key changed."""
    after = _toml()
    for k in FROZEN_ENTRY:
        if _get(before, k) != _get(after, k):
            _run(["git", "checkout", "--", "."])
            _die(f"refusing: frozen entry key changed: {k}")
    for k in ("race.sl_pct", "exits.stop_loss_pct"):
        b = _get(before, k)
        if b is not None and b != _get(after, k):
            _run(["git", "checkout", "--", "."])
            _die(f"refusing: stop-loss key changed: {k}")


def _guard_diff() -> None:
    bad = [p for p in _run(["git", "diff", "--name-only"]).stdout.split()
           if p not in ALLOWED_WRITE]
    if bad:
        _run(["git", "checkout", "--", "."])
        _die(f"refusing: changes outside the live grinder profile: {bad}")


def _tests_pass() -> bool:
    print("[auto-improve] running unit tests ...", flush=True)
    proc = _run(["uv", "run", "python", "-B", "-m", "unittest", "discover", "-s", "tests"])
    if proc.returncode != 0:
        print(proc.stdout[-2000:], proc.stderr[-2000:], file=sys.stderr, flush=True)
    return proc.returncode == 0


def main() -> int:
    _preflight()
    before = _toml()
    current = _read_params(before)
    if not current:
        _die("no tunable exit/sizing keys found in the live profile.")

    proposed = _propose(current)
    delta = {k: v for k, v in proposed.items() if current.get(k) != v}
    if not delta:
        print("[auto-improve] no change proposed this cycle.")
        return 0

    changed = _apply(delta)  # write only the keys that actually changed
    _guard_diff()
    _audit_frozen(before)
    if not changed:
        print("[auto-improve] proposal was a no-op after clamping.")
        _run(["git", "checkout", "--", "."])
        return 0

    if not _tests_pass():
        _run(["git", "checkout", "--", "."])
        _die("unit tests failed on the proposal — reverted, no PR.")

    branch = f"auto/tune-grinder-{time.strftime('%Y%m%d-%H%M%S')}"
    summary = "; ".join(changed)
    body = (
        "Autonomous strategy tune of the LIVE grinder profile (exit/sizing "
        "only — entry selection frozen, no stop-loss introduced).\n\n"
        f"Changes: {summary}\n\n"
        f"Recent perf: {json.dumps(_recent_performance())}\n\n"
        "Gate: unit tests pass locally + CI must be green before auto-merge. "
        "Values hard-clamped to safe ranges. Win-rate-driving entry filters "
        "were not touched.\n\n"
        "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
    )
    _run(["git", "checkout", "-b", branch])
    _run(["git", "add", str(TARGET.relative_to(REPO_ROOT))])
    _run(["git", "commit", "-m", f"auto-tune(grinder): {summary}", "-m", body])

    if _run(["git", "push", "-u", "origin", branch]).returncode != 0:
        _die("git push failed.")

    pr = _run(["gh", "pr", "create", "--base", "main", "--head", branch,
               "--title", f"auto-tune(grinder): {summary}", "--body", body])
    print(pr.stdout or pr.stderr, flush=True)

    if _on("AUTO_IMPROVE_AUTOMERGE", "1"):
        m = _run(["gh", "pr", "merge", branch, "--auto", "--squash"])
        print(m.stdout or m.stderr, flush=True)
        print("[auto-improve] auto-merge armed — merges when CI is green.")
    else:
        print("[auto-improve] PR opened; auto-merge OFF.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
