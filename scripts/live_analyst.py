#!/usr/bin/env python3
"""Live-mode analyst sidecar — READ-ONLY.

Runs alongside the live bot. Every CYCLE_SECONDS minutes:
  1. Reads live state (paper_state.json + trade_journal.jsonl)
  2. Reads dry-race leaderboard for context comparison
  3. Calls Codex CLI for an insights report, falling back to Ollama
  4. Posts to TELEGRAM_CHAT_ID_LIVE — Markdown report comparing
     the live profile to the top dry-race performers

NEVER spawns new bots, NEVER modifies the live profile, NEVER kills
anything. This is pure observability — if the analyst recommends a profile
switch, you do it manually.

The live runner only ever has ONE bot running, so there's no "race"
to autonomously manage in live. The analyst's value-add is the
cross-comparison with the dry race ("dry winner up +$15, your live
profile flat — consider switching to X").

Adjust LIVE_ANALYST_CYCLE_SECONDS to control report frequency.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from analyst_llm import call_analyst_llm

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DRY_RUNS_DIR = DATA_DIR / "dry_runs"


def _record_pnl(record: dict) -> float:
    for key in ("realized_pnl", "realized_pnl_usd"):
        if record.get(key) is not None:
            try:
                return float(record.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _realized_record_key(record: dict) -> str:
    token = str(record.get("token_id") or "")
    closed_at = str(record.get("closed_at") or "")
    reason = str(record.get("exit_reason") or record.get("reason") or "")
    pnl = round(_record_pnl(record), 4)
    if token or closed_at or reason:
        return f"{token}|{closed_at}|{reason}|{pnl}"
    return json.dumps(record, sort_keys=True)


def _realized_cache_path_for_journal(journal_path: Path) -> Path:
    return Path(
        os.environ.get(
            "POLYMARKET_REALIZED_CACHE_PATH",
            str(journal_path.parent / "realized_trade_cache.jsonl"),
        )
    )


def _is_realized_record(record: dict) -> bool:
    return bool(
        record.get("event") == "position_closed"
        or record.get("closed_at")
        or record.get("realized_pnl") is not None
        or record.get("realized_pnl_usd") is not None
    )


def _read_realized_records(journal_path: Path) -> list[dict]:
    records: dict[str, dict] = {}
    for path in (journal_path, _realized_cache_path_for_journal(journal_path)):
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and _is_realized_record(record):
                records[_realized_record_key(record)] = record
    return sorted(records.values(), key=lambda r: str(r.get("closed_at") or ""))


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
LLM_TIMEOUT_SECONDS = 240


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
    for entry in _read_realized_records(journal):
        pnl = _record_pnl(entry)
        closed += 1
        if pnl > 0:
            wins += 1
            win_pnls.append(pnl)
        elif pnl < 0:
            losses += 1
            loss_pnls.append(pnl)
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
        for entry in _read_realized_records(journal_file):
            pnl = _record_pnl(entry)
            closed += 1
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
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


def telegram_post(text: str, *, live: bool = True) -> bool:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_var = "TELEGRAM_CHAT_ID_LIVE" if live else "TELEGRAM_CHAT_ID_DRY_RUN"
    chat = (os.environ.get(chat_var) or "").strip()
    if not token or not chat:
        print(f"[live-analyst] telegram disabled ({chat_var} missing)\n{text}", flush=True)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"

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
        # 400 → Markdown parse error (e.g. the model's **bold** leaks into the
        # verdict). Retry as plain text so the report still lands.
        if getattr(exc, "code", None) == 400:
            try:
                return _send(False)
            except Exception as exc2:
                print(f"[live-analyst] telegram failed (plain retry): {exc2}", file=sys.stderr, flush=True)
                return False
        print(f"[live-analyst] telegram failed: {exc}", file=sys.stderr, flush=True)
        return False


def load_open_positions() -> list[dict]:
    """Read paper_state.json open positions with PnL details."""
    paper_state = DATA_DIR / "paper_state.json"
    if not paper_state.exists():
        return []
    try:
        state = json.loads(paper_state.read_text())
    except Exception:
        return []
    out = []
    for p in state.get("positions", []) or []:
        if p.get("status") != "open":
            continue
        entry = float(p.get("entry_price") or 0)
        shares = float(p.get("shares") or 0)
        cur = float(p.get("current_price") or 0)
        cost = float(p.get("stake") or p.get("size_usd") or entry * shares)
        mtm = cur * shares
        unr = mtm - cost
        unr_pct = (unr / cost * 100) if cost else 0
        out.append({
            "question": p.get("question") or p.get("market_title"),
            "side": p.get("outcome", "?"),
            "entry": entry, "cur": cur,
            "shares": shares,
            "cost": cost, "mtm": mtm,
            "unr": unr, "unr_pct": unr_pct,
            "opened_at": (p.get("opened_at") or "")[:19].replace("T", " "),
        })
    out.sort(key=lambda x: x["unr"], reverse=True)
    return out


def load_top_closed_trades(n: int = 3) -> list[dict]:
    """Pull top N closed trades by realized PnL from the live journal."""
    journal = DATA_DIR / "trade_journal.jsonl"
    if not journal.exists() and not _realized_cache_path_for_journal(journal).exists():
        return []
    rows = []
    try:
        for e in _read_realized_records(journal):
            pnl = _record_pnl(e)
            pct_raw = e.get("realized_pnl_pct") or e.get("pnl_pct")
            if pct_raw is not None:
                pct = float(pct_raw) * 100 if abs(float(pct_raw)) < 1 else float(pct_raw)
            else:
                pct = 0
            rows.append({
                "pnl": pnl, "pct": pct,
                "reason": e.get("exit_reason", "?"),
                "question": e.get("question") or e.get("market_title") or "?",
                "side": e.get("outcome", "?"),
                "entry": float(e.get("entry_price") or 0),
                "closed_at": (e.get("closed_at") or "")[:19].replace("T", " "),
            })
    except Exception:
        pass
    rows.sort(key=lambda r: r["pnl"], reverse=True)
    return rows[:n]


PROMPT = """You're the live trading analyst. The user gets this report on Telegram every 30min. Give ONE concise paragraph (3-4 lines max).

## LIVE
profile: {profile}
equity: ${equity:.2f} (was ${starting:.2f}, {roi:+.1f}%)
positions: {open_positions} open, {closed} closed ({wins}W/{losses}L, {win_rate:.0f}% wr)
realized: ${realized_pnl:+.2f}

## DRY top 5
{dry_top}

## YOUR JOB

Just 3-4 lines:
1. Health check — is the bot healthy or stuck? Give specifics.
2. Is live keeping up with the dry race leaders, or lagging? Give numbers.
3. Verdict: "hold steady" / "investigate X" / one concrete action.

No fluff. No "recommendation" headers. Just the lines."""


def cycle_once() -> None:
    snap = load_live_snapshot()
    if snap is None:
        msg = "🔵 *LIVE ANALYST* — no paper_state.json yet (bot not started?)"
        telegram_post(msg)
        print(msg, flush=True)
        return

    open_pos = load_open_positions()
    top_closed = load_top_closed_trades(3)
    dry_top = load_dry_top_n(5)

    # Find live's dry twin for direct comparison
    twin = next((r for r in dry_top if r["name"] == snap.profile), None)
    if not twin:
        # Profile not in top 5 — search the full set
        all_dry = load_dry_top_n(200)
        twin = next((r for r in all_dry if r["name"] == snap.profile), None)

    # Compute starting balance from realized + (equity - invested - cash); approximate
    # Better: read assumed_live_balance_usd from profile TOML
    starting = 20.0  # default; will refine below
    profile_file = REPO_ROOT / "configs" / "profiles" / f"{snap.profile}.toml"
    if profile_file.exists():
        try:
            import re as _re
            m = _re.search(r"^assumed_live_balance_usd\s*=\s*([\d.]+)",
                           profile_file.read_text(), _re.M)
            if m:
                starting = float(m.group(1))
        except Exception:
            pass
    pnl_total = snap.equity - starting
    roi = (pnl_total / starting * 100) if starting > 0 else 0

    dry_table = "\n".join(
        f"  {i+1}. {r['name']:38s} ${r['equity']:.2f} ({r['win_rate']:.0f}% wr, {r['closed']} closed)"
        for i, r in enumerate(dry_top)
    ) or "  (no dry-race data yet)"

    prompt = PROMPT.format(
        profile=snap.profile, equity=snap.equity, starting=starting, roi=roi,
        open_positions=snap.open_positions,
        closed=snap.closed, wins=snap.wins, losses=snap.losses,
        win_rate=snap.win_rate, realized_pnl=snap.realized_pnl,
        dry_top=dry_table,
    )
    narrative = call_analyst_llm(prompt, timeout=LLM_TIMEOUT_SECONDS, cwd=REPO_ROOT)

    stamp = time.strftime("%H:%M UTC", time.gmtime())
    sign = "+" if pnl_total >= 0 else ""

    parts = [
        f"🔵 *LIVE EXECUTIVE SUMMARY* · {stamp}",
        f"_strategy:_ `{snap.profile}`",
        "",
        f"💰 *${starting:.2f} → ${snap.equity:.2f}*  {sign}${pnl_total:.2f}  ({roi:+.1f}%)",
        f"   cash ${snap.cash:.2f}  •  invested ${snap.invested:.2f}  •  realized ${snap.realized_pnl:+.2f}",
        f"   {snap.closed} closed  •  {snap.wins}W/{snap.losses}L  •  {snap.win_rate:.0f}% wr  •  {snap.open_positions} open",
        "",
    ]

    if open_pos:
        parts.append(f"*🔓 Open positions ({len(open_pos)})*")
        for p in open_pos[:6]:
            psign = "+" if p["unr"] >= 0 else ""
            q = (p.get("question") or "?")[:55]
            parts.append(
                f"  • {q}\n"
                f"      {p.get('side','?')} @ ${p.get('entry',0):.3f} → ${p.get('cur',0):.3f}  "
                f"{psign}${p['unr']:.2f} ({p['unr_pct']:+.1f}%)"
            )
        if len(open_pos) > 6:
            parts.append(f"  _… and {len(open_pos)-6} more_")
        parts.append("")

    if top_closed:
        parts.append("*📊 Top closed trades*")
        for i, t in enumerate(top_closed, 1):
            psign = "+" if t["pnl"] >= 0 else ""
            q = (t.get("question") or "?")[:55]
            emoji = "🟢" if t["pnl"] > 0 else "🔴"
            parts.append(
                f"  {i}. {emoji} {psign}${t['pnl']:.2f} ({t['pct']:+.1f}%) "
                f"{t.get('reason','?')}\n      {q}"
            )
        parts.append("")
    elif snap.closed == 0:
        parts.append("_📊 No closed trades yet._")
        parts.append("")

    if twin:
        twin_starting = 20.0  # dry race default
        twin_roi = ((twin["equity"] - twin_starting) / twin_starting * 100) if twin_starting else 0
        delta = roi - twin_roi
        compare = (f"🏃 *vs dry twin:* live {roi:+.1f}%  |  dry "
                   f"{twin_roi:+.1f}% ({twin['closed']} closed)  |  "
                   f"Δ {delta:+.1f}pp")
        parts.append(compare)
        parts.append("")

    parts.append("*🏆 Dry race top 5*")
    for r in dry_top[:5]:
        dry_starting = 20.0
        r_roi = ((r["equity"] - dry_starting) / dry_starting * 100) if dry_starting else 0
        marker = "★" if r["name"] == snap.profile else " "
        parts.append(
            f"  {marker} {r['name']:38s} {r_roi:+6.1f}% ({r['closed']} closed, {r['win_rate']:.0f}% wr)"
        )
    parts.append("")

    parts.append("*🧠 Verdict*")
    parts.append(narrative.strip()[:1000] or "(analyst llm unavailable)")

    msg = "\n".join(parts)
    telegram_post(msg)
    print(msg, flush=True)


def main() -> int:
    print(f"[live-analyst] starting — cycle={CYCLE_SECONDS}s", flush=True)
    # Refresh immediately when possible so the live Telegram channel gets a
    # current snapshot on boot instead of waiting for the first interval.
    cycle_once()
    time.sleep(60)  # let the live bot write a first tick before the next cycle
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
