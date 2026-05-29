#!/usr/bin/env python3
"""Weekly Quant Summary — Polymarket Bot kzer_ai.

Generates and posts a weekly executive summary to TELEGRAM_CHAT_ID_LIVE.
Reads realized_trade_cache.jsonl (durable W/L record) for the trailing
7 days, paper_state.json for current equity, and grinder.toml for the
starting-cash baseline.

Usage:
  uv run python scripts/weekly_report.py          # current week
  uv run python scripts/weekly_report.py --days 14  # 2-week lookback
  uv run python scripts/weekly_report.py --dry-run  # print only, no Telegram
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"


# ── helpers ────────────────────────────────────────────────────────────────

def _load_dotenv() -> None:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _record_pnl(r: dict) -> float:
    for k in ("realized_pnl", "realized_pnl_usd"):
        if r.get(k) is not None:
            try:
                return float(r[k])
            except (TypeError, ValueError):
                pass
    return 0.0


def _read_trades(lookback_days: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    seen: dict[str, dict] = {}

    def _key(r: dict) -> str:
        return f"{r.get('token_id')}|{r.get('closed_at')}|{round(_record_pnl(r), 4)}"

    for path in (
        DATA_DIR / "realized_trade_cache.jsonl",
        DATA_DIR / "trade_journal.jsonl",
    ):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(r, dict) or not r.get("question"):
                continue
            closed_raw = r.get("closed_at") or ""
            try:
                closed_dt = datetime.fromisoformat(str(closed_raw).replace("Z", "+00:00"))
                if closed_dt < cutoff:
                    continue
            except (ValueError, TypeError):
                continue
            seen[_key(r)] = r

    return sorted(seen.values(), key=lambda r: str(r.get("closed_at") or ""))


def _load_equity() -> tuple[float, float, list[dict]]:
    """Return (cash, invested_current, open_positions_list)."""
    path = DATA_DIR / "paper_state.json"
    if not path.exists():
        return 0.0, 0.0, []
    try:
        state = json.loads(path.read_text())
    except Exception:
        return 0.0, 0.0, []
    cash = float(state.get("cash") or 0.0)
    positions = [p for p in (state.get("positions") or []) if p.get("status") == "open"]
    invested = 0.0
    for p in positions:
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
    return cash, invested, positions


def _starting_cash() -> float:
    import re as _re
    profile = REPO_ROOT / "configs" / "profiles" / "grinder.toml"
    if profile.exists():
        for pattern in (r"^assumed_live_balance_usd\s*=\s*([\d.]+)", r"^starting_cash\s*=\s*([\d.]+)"):
            m = _re.search(pattern, profile.read_text(), _re.M)
            if m:
                return float(m.group(1))
    baseline = DATA_DIR / "live_baseline.json"
    if baseline.exists():
        try:
            data = json.loads(baseline.read_text())
            v = float(data.get("starting_cash") or 0)
            if v > 0:
                return v
        except Exception:
            pass
    return 123.0


def _sign(v: float) -> str:
    return "+" if v >= 0 else ""


def _mood(v: float) -> str:
    return "🟢" if v >= 0 else "🔴"


def _fmt_trade_line(r: dict) -> str:
    pnl = _record_pnl(r)
    q = str(r.get("question") or "")[:52]
    outcome = str(r.get("outcome") or "")
    entry = float(r.get("entry_price") or 0)
    closed = str(r.get("closed_at") or "")[:10]
    emoji = "🟢" if pnl > 0 else "🔴"
    label = f"{q} ({outcome})" if outcome and outcome.lower() not in ("yes", "no", "") else q
    return f"  {emoji} {_sign(pnl)}${pnl:.2f}  {label[:58]}  @{entry:.3f}  [{closed}]"


def _telegram_post(text: str, dry_run: bool = False) -> None:
    if dry_run:
        print(text)
        return
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID_LIVE") or "").strip()
    if not token or not chat:
        print("[weekly-report] Telegram disabled (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID_LIVE missing)")
        print(text)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def _send(md: bool) -> bool:
        body: dict = {"chat_id": chat, "text": text[:4096], "disable_web_page_preview": True}
        if md:
            body["parse_mode"] = "Markdown"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status == 200
        except Exception as exc:
            print(f"[weekly-report] Telegram error: {exc}", file=sys.stderr)
            return False

    if not _send(True):
        _send(False)


# ── report builder ─────────────────────────────────────────────────────────

def build_report(lookback_days: int = 7) -> str:
    trades = _read_trades(lookback_days)
    cash, invested, open_pos = _load_equity()
    equity = cash + invested
    starting = _starting_cash()
    net_vs_start = equity - starting
    net_pct = (net_vs_start / starting * 100) if starting > 0 else 0.0

    wins = [t for t in trades if _record_pnl(t) > 0]
    losses = [t for t in trades if _record_pnl(t) < 0]
    flats = [t for t in trades if _record_pnl(t) == 0]
    decided = wins + losses
    total_pnl = sum(_record_pnl(t) for t in trades)
    win_pnl = sum(_record_pnl(t) for t in wins)
    loss_pnl = sum(_record_pnl(t) for t in losses)
    win_rate = (len(wins) / len(decided) * 100) if decided else 0.0
    avg_win = win_pnl / len(wins) if wins else 0.0
    avg_loss = loss_pnl / len(losses) if losses else 0.0
    profit_factor = (win_pnl / abs(loss_pnl)) if loss_pnl != 0 else float("inf")
    best = max(trades, key=_record_pnl) if trades else None
    worst = min(trades, key=_record_pnl) if trades else None

    # Week label
    now = datetime.now(timezone.utc)
    week_start = now - timedelta(days=lookback_days - 1)
    date_range = f"{week_start.strftime('%b %-d')}–{now.strftime('%b %-d, %Y')}"
    stamp = now.strftime("%H:%M UTC")
    divider = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    lines = [
        f"📊 *WEEKLY QUANT SUMMARY* — {date_range}",
        f"_Polymarket Bot_ `kzer_ai` _· Grinder · {stamp}_",
        divider,
        "",
        "*EQUITY*",
        f"  💵 Current: *${equity:.2f}*  (start ${starting:.2f})",
        f"  {_mood(net_vs_start)} P&L vs start: *{_sign(net_vs_start)}${net_vs_start:.2f}*  ({net_pct:+.1f}%)",
        "",
        "*TRADING PERFORMANCE*",
    ]

    if trades:
        pf_str = f"{profit_factor:.2f}x" if profit_factor != float("inf") else "∞"
        lines += [
            f"  📈 Trades:  {len(trades)} closed  ({len(wins)}W / {len(losses)}L"
            + (f" / {len(flats)} flat" if flats else "") + ")",
            f"  🎯 Win rate: *{win_rate:.0f}%*",
            f"  {_mood(total_pnl)} Net realized: *{_sign(total_pnl)}${total_pnl:.2f}*",
            f"  📊 Avg win: +${avg_win:.2f}  |  Avg loss: {avg_loss:.2f}  |  PF: {pf_str}",
        ]
    else:
        lines.append("  ⚪ No closed trades this period.")

    if best and _record_pnl(best) > 0:
        lines += [
            "",
            "*BEST TRADE*",
            _fmt_trade_line(best),
        ]
    if worst and _record_pnl(worst) < 0:
        lines += [
            "",
            "*WORST TRADE*",
            _fmt_trade_line(worst),
        ]

    if trades:
        lines += [
            "",
            "*ALL TRADES*",
        ]
        for t in sorted(trades, key=lambda x: str(x.get("closed_at") or "")):
            lines.append(_fmt_trade_line(t))

    if open_pos:
        lines += ["", f"*🔓 Open ({len(open_pos)})*"]
        for p in open_pos[:4]:
            q = str(p.get("question") or "")[:52]
            entry = float(p.get("entry_price") or 0)
            cur = float(p.get("current_price") or entry)
            cost = float(p.get("stake") or p.get("cost_basis") or 0)
            shares = float(p.get("shares") or 0)
            unr = cur * shares - cost if shares > 0 else 0.0
            lines.append(
                f"  {'🟢' if unr >= 0 else '🔴'} {q}  @{entry:.3f}→{cur:.3f}  {_sign(unr)}${unr:.2f}"
            )

    lines += [
        "",
        divider,
        f"🔵 *Polymarket Bot* `kzer_ai` · Grinder",
    ]

    return "\n".join(lines)


# ── main ───────────────────────────────────────────────────────────────────

def main() -> None:
    _load_dotenv()
    parser = argparse.ArgumentParser(description="Weekly quant summary")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days (default 7)")
    parser.add_argument("--dry-run", action="store_true", help="Print report, skip Telegram")
    args = parser.parse_args()

    report = build_report(lookback_days=args.days)
    _telegram_post(report, dry_run=args.dry_run)
    if not args.dry_run:
        print(report)
        print("[weekly-report] Sent to Telegram.")


if __name__ == "__main__":
    main()
