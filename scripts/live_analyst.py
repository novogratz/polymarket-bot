#!/usr/bin/env python3
"""Live-mode analyst sidecar — READ-ONLY.

Runs alongside the live bot. Every CYCLE_SECONDS minutes:
  1. Reads live state (paper_state.json + realized_trade_cache.jsonl)
  2. Posts to TELEGRAM_CHAT_ID_LIVE — a deterministic Markdown report
     of the live bot only (equity/ROI, open positions, top closed trades)

LIVE ONLY (2026-05-26) — no dry-race comparison. No LLM/AI anywhere; the
report is built from the numbers directly.

NEVER spawns new bots, NEVER modifies the live profile, NEVER kills
anything. Pure observability.

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

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"


def _record_pnl(record: dict) -> float:
    for key in ("realized_pnl", "realized_pnl_usd"):
        if record.get(key) is not None:
            try:
                return float(record.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _realized_record_key(record: dict) -> str:
    # Dedup by token_id — one close event per position token.
    # Previous key included full closed_at (with microseconds) which caused
    # duplicate journal entries written within the same second to slip through.
    token = str(record.get("token_id") or "")
    if token:
        return token
    # Fallback for records without token_id
    closed_at = str(record.get("closed_at") or "")[:10]  # date only
    question = str(record.get("question") or "")
    return f"{closed_at}|{question}"


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
DAILY_REPORT_HOUR_UTC = int(os.environ.get("LIVE_ANALYST_DAILY_REPORT_HOUR", "15"))  # 5 PM Paris (CEST = UTC+2)


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
    flats: int
    win_rate: float
    realized_pnl: float
    avg_win: float
    avg_loss: float
    ticks: int


def _fetch_live_equity() -> tuple[float, float] | None:
    """Return (available_cash, total_positions_value) from Polymarket live APIs.

    Uses: CLOB for available cash + Data API for all open/pending positions.
    Returns None on any failure so callers can fall back to local ledger.
    """
    try:
        import sys
        sys.path.insert(0, str(REPO_ROOT))
        from polymarket_bot.profiles import load_profile, apply_profile_to_env
        from polymarket_bot.config import Settings
        from polymarket_bot.trading import build_client
        from polymarket_bot.smart_money import DataApiClient
        profile_path = REPO_ROOT / "configs" / "profiles" / "grinder.toml"
        if profile_path.exists():
            apply_profile_to_env(load_profile(profile_path), override=False)
        settings = Settings()
        if not settings.funder_address or settings.dry_run:
            return None
        # Available cash from CLOB
        client = build_client(settings)
        try:
            avail = float(client.live_available_balance() or 0.0)
        except Exception:
            avail = float(settings.paper_balance_usd or 0.0)
        # All active positions from Data API — same filter as _sync_live_positions:
        # size > 0 AND currentValue >= min_value (drops dust / redeemed positions).
        pos_value = 0.0
        min_val = float(getattr(settings, "live_position_min_value_usd", 0.5) or 0.5)
        try:
            live_positions = DataApiClient(settings.data_api_base_url).positions(
                user=settings.funder_address
            )
            for item in live_positions:
                try:
                    size = float(item.get("size") or 0)
                    cv = float(item.get("currentValue") or 0)
                except (TypeError, ValueError):
                    continue
                if size <= 0 or cv < min_val:
                    continue
                pos_value += cv
        except Exception:
            pass
        return avail, pos_value
    except Exception:
        return None


def load_live_snapshot() -> LiveSnapshot | None:
    paper_state = DATA_DIR / "paper_state.json"
    if not paper_state.exists():
        return None
    try:
        state = json.loads(paper_state.read_text())
    except Exception:
        return None
    # Fetch real equity from Polymarket APIs so the report matches what the
    # user sees on polymarket.com. The local ledger cash is unreliable after
    # manual force-closes or mid-tick interruptions.
    live_data = _fetch_live_equity()
    positions = state.get("positions", []) or []
    open_positions = [p for p in positions if p.get("status") == "open"]
    if live_data is not None:
        avail_cash, pos_value = live_data
        cash = avail_cash
        invested = pos_value  # already includes ALL live positions from Data API
    else:
        cash = float(state.get("cash") or 0.0)
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
        # Skip ghost/test trades: no question means pre-strategy init artifacts.
        if not entry.get("question"):
            continue
        pnl = _record_pnl(entry)
        closed += 1
        if pnl > 0:
            wins += 1
            win_pnls.append(pnl)
        elif pnl < 0:
            losses += 1
            loss_pnls.append(pnl)
    flats = closed - wins - losses  # zero-PnL exits (expired at cost, arb sweeps)
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
        flats=flats,
        win_rate=(wins / decided * 100.0) if decided > 0 else 0.0,
        realized_pnl=sum(win_pnls) + sum(loss_pnls),
        avg_win=sum(win_pnls) / len(win_pnls) if win_pnls else 0.0,
        avg_loss=sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0,
        ticks=ticks,
    )




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


def _today_utc() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def load_todays_trades() -> list[dict]:
    """Closed trades with closed_at on today's UTC date."""
    today = _today_utc()
    journal = DATA_DIR / "trade_journal.jsonl"
    rows = []
    for e in _read_realized_records(journal):
        closed_at = str(e.get("closed_at") or "")
        if not closed_at.startswith(today):
            continue
        pnl = _record_pnl(e)
        pct_raw = e.get("realized_pnl_pct") or e.get("pnl_pct")
        if pct_raw is not None:
            pct = float(pct_raw) * 100 if abs(float(pct_raw)) < 1 else float(pct_raw)
        else:
            cost = float(e.get("cost_basis") or e.get("stake") or 1.0)
            pct = (pnl / cost * 100) if cost else 0.0
        rows.append({
            "pnl": pnl,
            "pct": pct,
            "question": (e.get("question") or "?")[:50],
            "reason": e.get("exit_reason", "?"),
        })
    rows.sort(key=lambda r: r["pnl"], reverse=True)
    return rows


def _segment_analysis(all_records: list[dict]) -> list[str]:
    """Analyse which market segments the bot wins on."""
    segments: dict[str, dict] = {}

    def _classify(q: str) -> str:
        q = q.lower()
        if "o/u" in q or "over/under" in q or "over " in q or "under " in q:
            return "Soccer O/U"
        if "win " in q or "win on" in q or "beat " in q:
            return "Match Result"
        if "die " in q or "survive" in q or "season" in q:
            return "Entertainment"
        if "btc" in q or "eth" in q or "bitcoin" in q or "crypto" in q:
            return "Crypto"
        if "seats" in q or "election" in q or "win the most" in q or "president" in q:
            return "Politics"
        return "Other"

    for rec in all_records:
        q = str(rec.get("question") or "")
        if not q:
            continue
        seg = _classify(q)
        pnl = _record_pnl(rec)
        if seg not in segments:
            segments[seg] = {"n": 0, "wins": 0, "pnl": 0.0}
        segments[seg]["n"] += 1
        segments[seg]["pnl"] += pnl
        if pnl > 0:
            segments[seg]["wins"] += 1

    if not segments:
        return []

    lines = ["*EDGE ANALYSIS:*"]
    sorted_segs = sorted(segments.items(), key=lambda x: x[1]["pnl"], reverse=True)
    for seg, d in sorted_segs:
        n, wins, pnl = d["n"], d["wins"], d["pnl"]
        wr = (wins / n * 100) if n else 0
        sign = "+" if pnl >= 0 else ""
        mood = "🟢" if pnl >= 0 else "🔴"
        lines.append(
            f"  {mood} {seg}: {n} trade{'s' if n!=1 else ''}  "
            f"{wins}W/{n-wins}L ({wr:.0f}% WR)  {sign}${pnl:.2f}"
        )
    best = sorted_segs[0]
    worst = sorted_segs[-1]
    if best[1]["pnl"] > 0:
        lines.append(f"  → Best edge: *{best[0]}* ({best[1]['wins']}W/{best[1]['n']-best[1]['wins']}L, ${best[1]['pnl']:+.2f})")
    if len(sorted_segs) > 1 and worst[1]["pnl"] < 0:
        lines.append(f"  → Avoid: *{worst[0]}* ({worst[1]['wins']}W/{worst[1]['n']-worst[1]['wins']}L, ${worst[1]['pnl']:+.2f})")
    return lines


def daily_report_once() -> None:
    """5 PM Paris daily quant summary."""
    snap = load_live_snapshot()
    if snap is None:
        return

    today_trades = load_todays_trades()
    open_pos = load_open_positions()

    # Starting balance from profile TOML
    starting = 123.0
    profile_file = REPO_ROOT / "configs" / "profiles" / f"{snap.profile}.toml"
    if profile_file.exists():
        try:
            m = re.search(r"^starting_cash\s*=\s*([\d.]+)", profile_file.read_text(), re.M)
            if m:
                starting = float(m.group(1))
        except Exception:
            pass

    net_vs_start = snap.equity - starting
    net_vs_start_pct = (net_vs_start / starting * 100) if starting > 0 else 0.0
    balance = snap.equity
    unrealized = sum(float(p.get("unr", 0) or 0) for p in open_pos)

    date_str = time.strftime("%B %-d, %Y", time.gmtime())
    divider = "━━━━━━━━━━━━━━━━━━━━━━━━"

    def _sign(v: float) -> str:
        return "+" if v >= 0 else ""

    def _mood(v: float) -> str:
        return "🟢" if v >= 0 else "🔴"

    # All-time stats from journal
    all_records = _read_realized_records(DATA_DIR / "trade_journal.jsonl")
    all_records = [r for r in all_records if r.get("question")]
    total_trades = len(all_records)
    total_wins = sum(1 for r in all_records if _record_pnl(r) > 0)
    total_losses = sum(1 for r in all_records if _record_pnl(r) < 0)
    alltime_wr = (total_wins / total_trades * 100) if total_trades else 0.0

    parts = [
        f"📋 *DAILY QUANT REPORT — {date_str}*",
        f"_Polymarket Bot_ `kzer_ai` _· Grinder_",
        divider,
        "",
        "*PROFIT & LOSS:*",
        f"  {_mood(net_vs_start)} P&L vs start:  ${_sign(net_vs_start)}{net_vs_start:.2f} ({_sign(net_vs_start_pct)}{net_vs_start_pct:.2f}%)  |  Equity: ${balance:.2f}",
        f"  📊 All-time: {total_trades} trade{'s' if total_trades!=1 else ''}  ({total_wins}W / {total_losses}L)  Win rate: {alltime_wr:.0f}%",
        "",
        "*ACTIVITY (TODAY):*",
    ]

    if today_trades:
        for t in today_trades:
            emoji = "🟢" if t["pnl"] >= 0 else "🔴"
            parts.append(
                f"  {emoji} {t['question']}: "
                f"{_sign(t['pnl'])}${t['pnl']:.2f} ({_sign(t['pct'])}{t['pct']:.2f}%)"
            )
    else:
        parts.append("  — No closed trades today")

    if open_pos:
        parts.append("")
        parts.append("*OPEN POSITIONS:*")
        for p in open_pos:
            unr = float(p.get("unr", 0) or 0)
            emoji = "🟢" if unr >= 0 else "🔴"
            q = (p.get("question") or "?")[:40]
            parts.append(
                f"  {emoji} {q} ({p.get('side','?')}): "
                f"cur {p.get('cur', 0):.3f}  {_sign(unr)}${unr:.2f}"
            )
        parts.append(
            f"  _Unrealized: {_sign(unrealized)}${unrealized:.2f}_"
        )

    parts.append("")
    edge_lines = _segment_analysis(all_records)
    parts.extend(edge_lines)

    parts += [
        "",
        "_Polymarket Bot_ `kzer_ai` _· Grinder_",
    ]

    msg = "\n".join(parts)
    telegram_post(msg)
    print(msg, flush=True)


def cycle_once() -> None:
    """30-min heartbeat — same format as the daily quant report."""
    daily_report_once()


def _cycle_once_old() -> None:
    snap = load_live_snapshot()
    if snap is None:
        msg = "🔵 *LIVE ANALYST* — no paper_state.json yet (bot not started?)"
        telegram_post(msg)
        print(msg, flush=True)
        return

    open_pos = load_open_positions()
    top_closed = load_top_closed_trades(3)

    # Starting balance / ROI baseline from the profile's assumed_live_balance_usd.
    starting = 43.0  # default; refined from the profile TOML below
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
    # Primary metric: equity vs starting balance (reflects deposits correctly).
    # Cumulative realized PnL is shown as context but not as the headline %.
    unrealized = sum(float(p.get("unr", 0) or 0) for p in open_pos)
    realized = snap.realized_pnl
    net_vs_start = snap.equity - starting
    net_pct = (net_vs_start / starting * 100) if starting > 0 else 0.0
    net_mood = "🟢" if net_vs_start >= 0 else "🔴"
    net_sign = "+" if net_vs_start >= 0 else ""

    stamp = time.strftime("%H:%M UTC", time.gmtime())
    r_sign = "+" if realized >= 0 else ""
    r_mood = "🟢" if realized >= 0 else "🔴"
    unr_sign = "+" if unrealized >= 0 else ""
    unr_mood = "🟢" if unrealized >= 0 else "🔴"
    status_word = "IN PROFIT 🤑" if net_vs_start > 0 else "DOWN 📉" if net_vs_start < 0 else "FLAT"

    # Daily P&L (trades closed today)
    today_trades = load_todays_trades()
    daily_pnl = sum(t["pnl"] for t in today_trades)
    daily_pct = (daily_pnl / starting * 100) if starting > 0 else 0.0
    daily_mood = "🟢" if daily_pnl >= 0 else "🔴"
    daily_sign = "+" if daily_pnl >= 0 else ""

    divider = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    parts = [
        f"🔵 *Polymarket Bot* `kzer_ai` · Grinder · {stamp}",
        "",
        divider,
        f"{net_mood} *P&L vs start: {net_sign}${net_vs_start:.2f}  ({net_pct:+.1f}%)*",
        f"{daily_mood} *Daily P&L:    {daily_sign}${daily_pnl:.2f}  ({daily_sign}{daily_pct:.1f}%)*",
        f"💵 *EQUITY: ${snap.equity:.2f}*  ({r_mood} realized {r_sign}${realized:.2f} cumul)",
        divider,
        "",
        f"{net_mood} *{status_word}*",
        f"   realized: {r_sign}${realized:.2f}  •  "
        f"unrealized: {unr_sign}${unrealized:.2f}",
        f"   cash ${snap.cash:.2f}  •  deployed ${snap.invested:.2f}",
        "",
        f"📊 {snap.closed} closed  •  🟢 {snap.wins}W / 🔴 {snap.losses}L"
        + (f" / {snap.flats} flat" if snap.flats else "")
        + f"  •  {snap.win_rate:.0f}% wr  •  {snap.open_positions} open",
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

    msg = "\n".join(parts)
    telegram_post(msg)
    print(msg, flush=True)


def main() -> int:
    print(f"[live-analyst] starting — cycle={CYCLE_SECONDS}s, daily report at {DAILY_REPORT_HOUR_UTC:02d}:00 UTC", flush=True)
    daily_report_sent_date: str = ""

    cycle_once()
    time.sleep(60)
    while True:
        try:
            cycle_once()
        except Exception:
            tb = traceback.format_exc()
            print(f"[live-analyst] cycle failed:\n{tb}", file=sys.stderr, flush=True)
            telegram_post(f"⚠️ *Live analyst error*\n```\n{tb[:1500]}\n```")

        # Daily 4 PM UTC report — fires once per day in the cycle after 16:00.
        try:
            now_utc = time.gmtime()
            today = _today_utc()
            if now_utc.tm_hour >= DAILY_REPORT_HOUR_UTC and daily_report_sent_date != today:
                daily_report_once()
                daily_report_sent_date = today
        except Exception:
            tb = traceback.format_exc()
            print(f"[live-analyst] daily report failed:\n{tb}", file=sys.stderr, flush=True)

        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    sys.exit(main() or 0)
