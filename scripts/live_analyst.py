#!/usr/bin/env python3
"""Live-mode analyst sidecar — READ-ONLY.

Runs alongside the live bot. Every CYCLE_SECONDS minutes:
  1. Reads live state (paper_state.json + trade_journal.jsonl)
  2. Reads dry-race leaderboard for context comparison
  3. Calls `claude` CLI for an insights report
  4. Posts to TELEGRAM_CHAT_ID_LIVE — Markdown report comparing
     the live profile to the top dry-race performers

NEVER spawns new bots, NEVER modifies the live profile, NEVER kills
anything. This is pure observability — if Claude recommends a profile
switch, you do it manually.

The live runner only ever has ONE bot running, so there's no "race"
to autonomously manage in live. The analyst's value-add is the
cross-comparison with the dry race ("dry winner up +$15, your live
profile flat — consider switching to X").

Cost: ~$0.05-0.30 per call × cycle frequency. Default 1800s (30 min)
keeps daily cost under $15. Adjust LIVE_ANALYST_CYCLE_SECONDS.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DRY_RUNS_DIR = DATA_DIR / "dry_runs"


def _load_dotenv() -> None:
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
        print(f"[live-analyst] .env load failed: {exc}", file=sys.stderr, flush=True)


_load_dotenv()

CYCLE_SECONDS = int(os.environ.get("LIVE_ANALYST_CYCLE_SECONDS", "1800"))   # 30 min
CLAUDE_TIMEOUT_SECONDS = 240


@dataclass
class LiveSnapshot:
    profile: str
    cash: float
    equity: float
    open_positions: int
    invested: float
    closed: int
    wins: int
    losses: int
    win_rate: float
    realized_pnl: float
    avg_win: float
    avg_loss: float
    ticks: int


def load_live_snapshot() -> LiveSnapshot | None:
    paper_state = DATA_DIR / "paper_state.json"
    if not paper_state.exists():
        return None
    try:
        state = json.loads(paper_state.read_text())
    except Exception:
        return None
    cash = float(state.get("cash") or 0.0)
    positions = state.get("positions", []) or []
    open_positions = [p for p in positions if p.get("status") == "open"]
    # Mark-to-market preferred. Live-synced positions don't always have
    # size_usd/notional_usd, so fall back to current_price × shares,
    # then to stake/cost_basis if no current_price either.
    invested = 0.0
    for p in open_positions:
        shares = float(p.get("shares") or 0.0)
        cur = p.get("current_price")
        if cur is not None and shares > 0:
            try:
                invested += float(cur) * shares
                continue
            except (TypeError, ValueError):
                pass
        invested += float(
            p.get("size_usd") or p.get("notional_usd")
            or p.get("stake") or p.get("cost_basis") or 0.0
        )
    equity = cash + invested
    closed = wins = losses = 0
    win_pnls: list[float] = []
    loss_pnls: list[float] = []
    journal = DATA_DIR / "trade_journal.jsonl"
    if journal.exists():
        try:
            for line in journal.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("event") != "position_closed":
                    continue
                pnl = float(entry.get("realized_pnl_usd") or 0.0)
                closed += 1
                if pnl > 0:
                    wins += 1
                    win_pnls.append(pnl)
                elif pnl < 0:
                    losses += 1
                    loss_pnls.append(pnl)
        except Exception:
            pass
    decided = wins + losses
    ticks = 0
    th = DATA_DIR / "tick_history.jsonl"
    if th.exists():
        try:
            ticks = sum(1 for _ in th.open())
        except Exception:
            pass
    profile = os.environ.get("POLYMARKET_PROFILE_LABEL", "(unknown)")
    return LiveSnapshot(
        profile=profile,
        cash=cash,
        equity=equity,
        open_positions=len(open_positions),
        invested=invested,
        closed=closed,
        wins=wins,
        losses=losses,
        win_rate=(wins / decided * 100.0) if decided > 0 else 0.0,
        realized_pnl=sum(win_pnls) + sum(loss_pnls),
        avg_win=sum(win_pnls) / len(win_pnls) if win_pnls else 0.0,
        avg_loss=sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0,
        ticks=ticks,
    )


def load_dry_top_n(n: int = 5) -> list[dict]:
    """Read dry_runs/* and return top n by realized PnL."""
    if not DRY_RUNS_DIR.exists():
        return []
    rows: list[dict] = []
    for run_dir in DRY_RUNS_DIR.iterdir():
        if not run_dir.is_dir():
            continue
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
        closed = wins = losses = 0
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
                    pnl = float(entry.get("realized_pnl_usd") or 0.0)
                    closed += 1
                    if pnl > 0:
                        wins += 1
                    elif pnl < 0:
                        losses += 1
            except Exception:
                pass
        # starting cash default 20, but we don't need exact — rank on equity
        decided = wins + losses
        rows.append({
            "name": run_dir.name,
            "equity": equity,
            "closed": closed,
            "win_rate": (wins / decided * 100.0) if decided > 0 else 0.0,
        })
    rows.sort(key=lambda r: r["equity"], reverse=True)
    return [r for r in rows if r["closed"] >= 3][:n]


def call_claude(prompt: str) -> str:
    try:
        result = subprocess.run(
            ["claude", "--dangerously-skip-permissions", "-p", prompt],
            capture_output=True, text=True, timeout=CLAUDE_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return f"[claude error rc={result.returncode}]\n{result.stderr[:500]}"
        return result.stdout
    except subprocess.TimeoutExpired:
        return "[claude timeout]"
    except Exception as exc:
        return f"[claude exception: {type(exc).__name__}: {exc}]"


def telegram_post(text: str, *, live: bool = True) -> bool:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_var = "TELEGRAM_CHAT_ID_LIVE" if live else "TELEGRAM_CHAT_ID_DRY_RUN"
    chat = (os.environ.get(chat_var) or "").strip()
    if not token or not chat:
        print(f"[live-analyst] telegram disabled ({chat_var} missing)\n{text}", flush=True)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
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
        print(f"[live-analyst] telegram failed: {exc}", file=sys.stderr, flush=True)
        return False


PROMPT = """You are the live observability analyst for a Polymarket trading bot.

## LIVE bot
profile: {profile}
equity: ${equity:.2f} (cash ${cash:.2f}, invested ${invested:.2f})
positions open: {open_positions}
closed trades: {closed} ({wins}W/{losses}L, {win_rate:.0f}% wr)
realized PnL: ${realized_pnl:+.2f}
avg win: ${avg_win:+.2f}, avg loss: ${avg_loss:+.2f}
ticks elapsed: {ticks}

## DRY RACE top 5 (for context)
{dry_top}

## YOUR JOB (be terse, 4-6 lines max)

1. **One-line live status** — is the live bot healthy? (drawdown? no trades? cash stuck?)

2. **Live vs dry comparison** — which dry strategy is doing best? Is the live profile keeping up, or should the user consider switching? Give SPECIFIC numbers.

3. **One actionable recommendation** OR "hold steady".

Output: plain markdown, no code fences. Do NOT propose to switch live to a strategy with <10 closed trades or <55% win-rate."""


def cycle_once() -> None:
    snap = load_live_snapshot()
    if snap is None:
        msg = "🔵 *LIVE ANALYST* — no paper_state.json yet (bot not started?)"
        telegram_post(msg)
        print(msg, flush=True)
        return

    dry_top = load_dry_top_n(5)
    dry_table = "\n".join(
        f"  {i+1}. {r['name']:32s} equity=${r['equity']:.2f}  ({r['win_rate']:.0f}% wr, {r['closed']} closed)"
        for i, r in enumerate(dry_top)
    ) or "  (no dry-race data yet)"

    prompt = PROMPT.format(
        profile=snap.profile, equity=snap.equity, cash=snap.cash,
        invested=snap.invested, open_positions=snap.open_positions,
        closed=snap.closed, wins=snap.wins, losses=snap.losses,
        win_rate=snap.win_rate, realized_pnl=snap.realized_pnl,
        avg_win=snap.avg_win, avg_loss=snap.avg_loss, ticks=snap.ticks,
        dry_top=dry_table,
    )
    narrative = call_claude(prompt)

    stamp = time.strftime("%H:%M UTC", time.gmtime())
    msg = "\n".join([
        f"🔵 *LIVE ANALYST* · {stamp}",
        "",
        f"`{snap.profile}` equity *${snap.equity:.2f}* "
        f"({snap.closed} closed, {snap.win_rate:.0f}% wr, "
        f"{snap.open_positions} open, ${snap.realized_pnl:+.2f} realized)",
        "",
        "*Dry race top 5*",
        dry_table,
        "",
        "*🧠 Insights*",
        narrative.strip()[:1500] or "(empty response)",
    ])
    telegram_post(msg)
    print(msg, flush=True)


def main() -> int:
    print(f"[live-analyst] starting — cycle={CYCLE_SECONDS}s", flush=True)
    time.sleep(60)  # wait for first live tick
    while True:
        try:
            cycle_once()
        except Exception:
            tb = traceback.format_exc()
            print(f"[live-analyst] cycle failed:\n{tb}", file=sys.stderr, flush=True)
            telegram_post(f"⚠️ *Live analyst error*\n```\n{tb[:1500]}\n```")
        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    sys.exit(main() or 0)
