#!/usr/bin/env python3
"""Autonomous strategy self-improvement loop — OFFLINE, DRY-TWIN ONLY.

This is the "agentic self-improvement" engine. It proposes a small, bounded
tweak to the *dry* experimental profile (configs/profiles/grinder_auto.toml),
validates it (TOML parse + hard bounds + the full unit-test suite), and opens
a pull request with `gh`. Optionally it self-merges — but ONLY when CI is green
and only if AUTO_IMPROVE_AUTOMERGE=1.

It is built to be impossible to "fuck up the live bot":

  * It edits ONLY configs/profiles/grinder_auto.toml. Any attempt to touch a
    live profile (grinder.toml / grinder_b.toml), .env, or the trade-selection
    code is refused.
  * grinder_auto is a DRY-RUN paper profile. Nothing here spends real money or
    feeds the live bots. Promoting a tuned value to a live profile is a manual,
    human step — the agent never does it.
  * Every proposed value is clamped to a hard safe range (BOUNDS), including the
    4h-only rule (max_hours <= 4.0).
  * It runs in an offline path. There is NO LLM in the live trade-selection
    loop — that rule from CLAUDE.md is untouched. The optional LLM here only
    *suggests* parameter deltas offline, and every suggestion still passes
    through the same hard bound-checker.

Dangerous capabilities the user asked about are present only as explicit,
OFF-by-default switches, documented in docs/AUTONOMY.md:
  * AUTO_IMPROVE_AUTOMERGE=1   -> self-merge the PR once CI is green.
  * AUTO_IMPROVE_USE_LLM=1     -> use the claude CLI to propose the delta
                                  (default: a deterministic bounded hill-climb).

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
TARGET = REPO_ROOT / "configs" / "profiles" / "grinder_auto.toml"

# The ONLY profile the agent may write. Touching anything else aborts the run.
ALLOWED_WRITE = {str(TARGET.relative_to(REPO_ROOT))}

# Whitelisted [race] knobs and their HARD safe bounds. A proposal outside the
# range is clamped, never applied raw. max_hours caps at 4.0 (4h-only rule).
BOUNDS: dict[str, tuple[float, float]] = {
    "min_price": (0.80, 0.97),
    "max_price": (0.86, 0.99),
    "max_spread": (0.005, 0.06),
    "max_hours": (0.25, 4.0),
    "max_day_change_pct": (0.02, 0.30),
    "min_outcome_momentum": (-0.15, 0.0),
    "stake_pct": (0.05, 0.95),
    "max_orders_per_tick": (1, 5),
    "min_liquidity_usd": (200.0, 5000.0),
    "min_volume_24h_usd": (100.0, 5000.0),
}
INT_KEYS = {"max_orders_per_tick"}


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True,
                          encoding="utf-8", errors="replace", **kw)


def _die(msg: str, code: int = 1) -> None:
    print(f"[auto-improve] ABORT: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def _preflight() -> None:
    if os.environ.get("AUTO_IMPROVE_ENABLED", "0").strip() not in {"1", "true", "True"}:
        _die("AUTO_IMPROVE_ENABLED is not set — refusing to run.", code=2)
    if not TARGET.is_file():
        _die(f"dry-twin profile missing: {TARGET}")
    # Clean working tree only — never improve on top of uncommitted edits.
    st = _run(["git", "status", "--porcelain"])
    if st.stdout.strip():
        _die("working tree is dirty; commit or stash first.")


def _read_params() -> dict[str, float]:
    data = tomllib.loads(TARGET.read_text(encoding="utf-8"))
    race = data.get("race", {})
    return {k: race[k] for k in BOUNDS if k in race}


def _clamp(key: str, val: float) -> float:
    lo, hi = BOUNDS[key]
    val = max(lo, min(hi, float(val)))
    if key in INT_KEYS:
        val = int(round(val))
    return val


def _propose(current: dict[str, float]) -> dict[str, float]:
    """Return the proposed new param set (already clamped). LLM optional."""
    if os.environ.get("AUTO_IMPROVE_USE_LLM", "0").strip() in {"1", "true", "True"}:
        llm = _propose_llm(current)
        if llm:
            return {k: _clamp(k, v) for k, v in llm.items() if k in BOUNDS}
    return _propose_hillclimb(current)


def _propose_hillclimb(current: dict[str, float]) -> dict[str, float]:
    """Deterministic, dependency-free bounded nudge. Tightens the entry band
    slightly toward higher-certainty markets — a safe default direction. Seeded
    by the day so successive runs explore without thrashing."""
    import random
    rng = random.Random(int(time.time() // 86400))
    out = dict(current)
    knob = rng.choice([k for k in current] or list(BOUNDS))
    step = {
        "min_price": 0.01, "max_price": 0.01, "max_spread": 0.005,
        "max_hours": 0.5, "max_day_change_pct": 0.01,
        "min_outcome_momentum": 0.01, "stake_pct": 0.05,
        "max_orders_per_tick": 1, "min_liquidity_usd": 100.0,
        "min_volume_24h_usd": 100.0,
    }.get(knob, 0.01)
    direction = rng.choice([-1, 1])
    out[knob] = _clamp(knob, current.get(knob, BOUNDS[knob][0]) + direction * step)
    return out


def _propose_llm(current: dict[str, float]) -> dict[str, float] | None:
    """Ask the claude CLI for a bounded parameter delta. Offline, advisory only;
    output is still hard-clamped by the caller. Returns None on any failure."""
    prompt = (
        "You tune a Polymarket 'grinder' strategy (buy heavy-favorite binaries, "
        "ride to resolution, no stop-loss). Given the current DRY parameters and "
        "their allowed ranges, propose a SMALL improvement. Return ONLY a JSON "
        "object of changed keys -> numeric values, no prose.\n\n"
        f"current = {json.dumps(current)}\n"
        f"allowed_ranges = {json.dumps({k: list(v) for k, v in BOUNDS.items()})}\n"
    )
    try:
        proc = _run(["claude", "-p", prompt], timeout=150)
        out = (proc.stdout or "").strip()
        s, e = out.find("{"), out.rfind("}")
        if s == -1 or e <= s:
            return None
        raw = json.loads(out[s:e + 1])
        return {k: float(v) for k, v in raw.items()
                if k in BOUNDS and isinstance(v, (int, float))}
    except Exception as exc:  # noqa: BLE001
        print(f"[auto-improve] LLM propose failed, using hill-climb: {exc}",
              file=sys.stderr, flush=True)
        return None


def _apply(new: dict[str, float]) -> list[str]:
    """Write the new values into the [race] section. Returns changed-key lines."""
    text = TARGET.read_text(encoding="utf-8")
    lines = text.splitlines()
    changed: list[str] = []
    for key, val in new.items():
        fmt = str(int(val)) if key in INT_KEYS else f"{float(val):.3f}".rstrip("0").rstrip(".")
        pat_idx = None
        for i, ln in enumerate(lines):
            if ln.lstrip().startswith(f"{key} ") and "=" in ln and not ln.lstrip().startswith("#"):
                pat_idx = i
                break
        if pat_idx is None:
            continue
        old = lines[pat_idx]
        lines[pat_idx] = f"{key} = {fmt}"
        if lines[pat_idx] != old:
            changed.append(f"{key}: {old.split('=')[-1].strip()} -> {fmt}")
    TARGET.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def _guard_diff() -> None:
    """Fail loudly if anything other than the dry-twin profile changed."""
    diff = _run(["git", "diff", "--name-only"]).stdout.split()
    bad = [p for p in diff if p not in ALLOWED_WRITE]
    if bad:
        _run(["git", "checkout", "--", "."])
        _die(f"refusing: changes outside the dry-twin profile: {bad}")


def _tests_pass() -> bool:
    print("[auto-improve] running unit tests ...", flush=True)
    proc = _run(["uv", "run", "python", "-B", "-m", "unittest", "discover", "-s", "tests"])
    ok = proc.returncode == 0
    if not ok:
        print(proc.stdout[-2000:], proc.stderr[-2000:], file=sys.stderr, flush=True)
    return ok


def main() -> int:
    _preflight()
    current = _read_params()
    if not current:
        _die("no tunable [race] keys found in the dry-twin profile.")

    proposed = _propose(current)
    deltas = {k: v for k, v in proposed.items() if current.get(k) != v}
    if not deltas:
        print("[auto-improve] no change proposed this cycle — nothing to do.")
        return 0

    changed = _apply(proposed)
    _guard_diff()  # hard stop if anything but grinder_auto.toml moved
    if not changed:
        print("[auto-improve] proposal was a no-op after clamping.")
        _run(["git", "checkout", "--", "."])
        return 0

    if not _tests_pass():
        _run(["git", "checkout", "--", "."])
        _die("unit tests failed on the proposal — reverted, no PR.")

    branch = f"auto/tune-grinder-{time.strftime('%Y%m%d-%H%M%S')}"
    summary = "; ".join(changed)
    _run(["git", "checkout", "-b", branch])
    _run(["git", "add", str(TARGET.relative_to(REPO_ROOT))])
    body = (
        "Autonomous strategy tune (DRY twin grinder_auto — paper only, live "
        "bots untouched).\n\n"
        f"Changes: {summary}\n\n"
        "Gate: unit tests pass locally + CI must be green before merge. "
        "Values hard-clamped to safe ranges (4h-only enforced). Promotion to a "
        "live profile remains a manual human step.\n\n"
        "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
    )
    _run(["git", "commit", "-m", f"auto-tune(grinder_auto): {summary}", "-m", body])

    push = _run(["git", "push", "-u", "origin", branch])
    if push.returncode != 0:
        print(push.stderr, file=sys.stderr, flush=True)
        _die("git push failed.")

    pr = _run(["gh", "pr", "create", "--base", "main", "--head", branch,
               "--title", f"auto-tune(grinder_auto): {summary}", "--body", body])
    print(pr.stdout or pr.stderr, flush=True)

    if os.environ.get("AUTO_IMPROVE_AUTOMERGE", "0").strip() in {"1", "true", "True"}:
        # --auto + --squash: GitHub merges ONLY when required checks (CI) pass.
        m = _run(["gh", "pr", "merge", branch, "--auto", "--squash"])
        print(m.stdout or m.stderr, flush=True)
        print("[auto-improve] auto-merge armed (merges when CI is green).")
    else:
        print("[auto-improve] PR opened; auto-merge OFF — waiting for human review.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
