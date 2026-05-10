"""Telegram push-only notifications. Best-effort silent.

Sans TELEGRAM_BOT_TOKEN défini, toutes les fonctions sont no-op et
``is_enabled()`` retourne False. Aucune exception n'est jamais
remontée — toute erreur est loggée sur stdout puis ignorée.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_HTTP_TIMEOUT_SEC = 5.0

# Transport: callable qui prend un dict payload (chat_id, text, parse_mode)
# et retourne True si l'envoi a réussi. Injectable pour les tests.
Transport = Callable[[dict[str, Any]], bool]

_transport_override: Transport | None = None


def _reset_for_tests() -> None:
    """Réinitialise l'état module entre tests."""
    global _transport_override
    _transport_override = None


def set_transport_for_test(transport: Transport | None) -> None:
    """Injecte un transport custom (tests uniquement)."""
    global _transport_override
    _transport_override = transport


def _bot_token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def _is_dry_run() -> bool:
    return os.environ.get("POLYMARKET_DRY_RUN", "").strip() in {"1", "true", "True"}


def _chat_id() -> str:
    if _is_dry_run():
        return os.environ.get("TELEGRAM_CHAT_ID_DRY_RUN", "").strip()
    return os.environ.get("TELEGRAM_CHAT_ID_LIVE", "").strip()


def is_enabled() -> bool:
    """True si token + chat_id pour le mode actif sont définis."""
    return bool(_bot_token()) and bool(_chat_id())


def _flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip() in {"1", "true", "True"}


# Caractères que MarkdownV2 exige d'échapper dans le texte standard.
# Ref: https://core.telegram.org/bots/api#markdownv2-style
_MD_SPECIAL_CHARS = "_*[]()~`>#+-=|{}.!"


def _md_escape(text: str) -> str:
    """Échappe les caractères MarkdownV2 spéciaux pour Telegram."""
    if not text:
        return ""
    out: list[str] = []
    for ch in str(text):
        if ch in _MD_SPECIAL_CHARS:
            out.append("\\")
        out.append(ch)
    return "".join(out)


@dataclass
class _State:
    equity_peak_usd: float | None = None
    equity_floor_breached: bool = False
    last_daily_summary_date: str | None = None
    last_portfolio_update_ts: float | None = None
    dedupe_seen: dict[str, float] = field(default_factory=dict)
    drawdown_armed: bool = False  # True quand on a déjà alerté sur ce pic


def _default_state_path() -> Path:
    if _is_dry_run():
        return Path("data/dry_run_notifications_state.json")
    return Path("data/notifications_state.json")


def _load_state(path: Path) -> _State:
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return _State()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[notif] state file corrupt at {path}, resetting", file=sys.stderr, flush=True)
        return _State()
    return _State(
        equity_peak_usd=data.get("equity_peak_usd"),
        equity_floor_breached=bool(data.get("equity_floor_breached", False)),
        last_daily_summary_date=data.get("last_daily_summary_date"),
        last_portfolio_update_ts=data.get("last_portfolio_update_ts"),
        dedupe_seen={str(k): float(v) for k, v in (data.get("dedupe_seen") or {}).items()},
        drawdown_armed=bool(data.get("drawdown_armed", False)),
    )


def _dedupe_window_sec() -> float:
    try:
        return float(os.environ.get("TELEGRAM_DEDUPE_WINDOW_SEC", "300"))
    except ValueError:
        return 300.0


def _prune_dedupe(state: _State, now: float, window: float) -> None:
    """Supprime les entrées plus anciennes que window × 4."""
    cutoff = now - (window * 4)
    state.dedupe_seen = {k: v for k, v in state.dedupe_seen.items() if v >= cutoff}


def _save_state(path: Path, state: _State) -> None:
    payload = {
        "equity_peak_usd": state.equity_peak_usd,
        "equity_floor_breached": state.equity_floor_breached,
        "last_daily_summary_date": state.last_daily_summary_date,
        "last_portfolio_update_ts": state.last_portfolio_update_ts,
        "dedupe_seen": state.dedupe_seen,
        "drawdown_armed": state.drawdown_armed,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"[notif] failed to save state: {exc}", file=sys.stderr, flush=True)


def _default_transport(payload: dict[str, Any]) -> bool:
    """Transport par défaut: POST sur api.telegram.org via urllib."""
    token = _bot_token()
    if not token:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(f"[notif] failed: {exc}", file=sys.stderr, flush=True)
        return False


def _get_transport() -> Transport:
    return _transport_override if _transport_override is not None else _default_transport


def _post(text: str) -> bool:
    """Envoi best-effort. Retourne False sur erreur, jamais d'exception."""
    if not is_enabled():
        return False
    payload = {
        "chat_id": _chat_id(),
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }
    try:
        return bool(_get_transport()(payload))
    except Exception as exc:
        print(f"[notif] failed: {exc}", file=sys.stderr, flush=True)
        return False


# --- API publique (stubs no-op tant que désactivé) ---


def _fmt_held(seconds: int | None) -> str:
    if not seconds or seconds <= 0:
        return ""
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    if h >= 24:
        d, h = divmod(h, 24)
        return f"{d}d {h}h"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def notify_trade_buy(
    *,
    market_title: str,
    token_id: str,
    price: float,
    size_usd: float,
    signal: dict[str, Any],
    outcome: str | None = None,
    market_url: str | None = None,
) -> None:
    if not is_enabled() or not _flag("TELEGRAM_ALERT_TRADES"):
        return
    wallets = int(signal.get("wallets", 0))
    copied = float(signal.get("copied_usdc", 0))
    tag = signal.get("tag")  # ex. "btc_edge", "noise_fallback"

    if tag:
        signal_line = f"Tag: `{tag}`"
    elif wallets > 0:
        copied_str = f"${copied/1000:.1f}k" if copied >= 1000 else f"${copied:.0f}"
        signal_line = (
            f"Smart\\-money: {wallets} wallets, "
            f"{_md_escape(copied_str)} copied"
        )
    else:
        signal_line = ""

    lines = [
        f"\U0001f7e2 *BUY* `${_md_escape(f'{size_usd:.2f}')}` @ `{_md_escape(f'{price:.2f}')}`",
        f"*{_md_escape(market_title)}*",
    ]
    if outcome:
        lines.append(f"Pick: *{_md_escape(outcome)}*")
    if signal_line:
        lines.append(signal_line)
    if market_url:
        lines.append(f"[market]({market_url})")
    _post("\n".join(lines))


def notify_trade_sell(
    *,
    market_title: str,
    token_id: str,
    price: float,
    size_usd: float,
    realized_pnl_usd: float,
    realized_pnl_pct: float | None,
    reason: str,
    held_seconds: int | None = None,
) -> None:
    if not is_enabled() or not _flag("TELEGRAM_ALERT_TRADES"):
        return
    sign = "+" if realized_pnl_usd >= 0 else "-"
    pnl_abs = abs(realized_pnl_usd)
    pnl_str = f"{_md_escape(sign)}\\${_md_escape(f'{pnl_abs:.2f}')}"
    pct_str = ""
    if realized_pnl_pct is not None:
        sign_pct = "+" if realized_pnl_pct >= 0 else "-"
        pct_str = f" \\({_md_escape(f'{sign_pct}{abs(realized_pnl_pct):.1f}%')}\\)"
    held_str = _fmt_held(held_seconds)
    held_line = f" — held {_md_escape(held_str)}" if held_str else ""
    lines = [
        f"\U0001f534 *SELL* `${_md_escape(f'{size_usd:.2f}')}` @ `{_md_escape(f'{price:.2f}')}` — `{reason}`",
        f"*{_md_escape(market_title)}*",
        f"PnL: *{pnl_str}*{pct_str}{held_line}",
    ]
    _post("\n".join(lines))


def notify_error(category: str, message: str, *, dedupe_key: str | None = None) -> None:
    if not is_enabled() or not _flag("TELEGRAM_ALERT_ERRORS"):
        return
    path = _default_state_path()
    state = _load_state(path)
    now = time.time()
    window = _dedupe_window_sec()
    if dedupe_key:
        last = state.dedupe_seen.get(dedupe_key)
        if last is not None and (now - last) < window:
            return
        state.dedupe_seen[dedupe_key] = now
    _prune_dedupe(state, now, window)
    text = (
        f"❌ *{_md_escape(category)}*\n"
        f"{_md_escape(message)}"
    )
    _post(text)
    _save_state(path, state)


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _handle_big_win(payload: dict[str, Any]) -> None:
    threshold = _float_env("TELEGRAM_BIG_WIN_USD", 10.0)
    pnl = float(payload.get("pnl_usd", 0))
    if pnl < threshold:
        return
    reason = str(payload.get("reason", ""))
    title = str(payload.get("market_title", ""))
    held_str = _fmt_held(payload.get("held_seconds"))
    held_line = f" after {_md_escape(held_str)}" if held_str else ""
    text = (
        f"\U0001f4b0 *BIG WIN* {_md_escape(f'+${pnl:.2f}')} on *{_md_escape(title)}*\n"
        f"Exit: `{reason}`{held_line}"
    )
    _post(text)


def _handle_big_loss(payload: dict[str, Any]) -> None:
    threshold = _float_env("TELEGRAM_BIG_LOSS_USD", 5.0)
    pnl = float(payload.get("pnl_usd", 0))
    if pnl > -threshold:
        return
    reason = str(payload.get("reason", ""))
    title = str(payload.get("market_title", ""))
    held_str = _fmt_held(payload.get("held_seconds"))
    held_line = f" after {_md_escape(held_str)}" if held_str else ""
    text = (
        f"\U0001f4b8 *BIG LOSS* {_md_escape(f'-${abs(pnl):.2f}')} on *{_md_escape(title)}*\n"
        f"Exit: `{reason}`{held_line}"
    )
    _post(text)


def _handle_drawdown(payload: dict[str, Any]) -> None:
    equity = float(payload.get("equity_usd", 0))
    if equity <= 0:
        return
    threshold_pct = _float_env("TELEGRAM_DRAWDOWN_PCT", 10.0)
    path = _default_state_path()
    state = _load_state(path)

    peak = state.equity_peak_usd or equity
    if equity > peak:
        state.equity_peak_usd = equity
        state.drawdown_armed = False
        _save_state(path, state)
        return
    state.equity_peak_usd = peak

    drawdown_pct = ((peak - equity) / peak) * 100.0
    if drawdown_pct >= threshold_pct and not state.drawdown_armed:
        text = (
            f"⚠️ *Drawdown* {_md_escape(f'-{drawdown_pct:.1f}%')} from peak\n"
            f"Equity: {_md_escape(f'${equity:.2f}')} \\(peak {_md_escape(f'${peak:.2f}')}\\)"
        )
        if _post(text):
            state.drawdown_armed = True
    _save_state(path, state)


def _handle_equity_floor(payload: dict[str, Any]) -> None:
    equity = float(payload.get("equity_usd", 0))
    floor = _float_env("TELEGRAM_EQUITY_FLOOR_USD", 50.0)
    if floor <= 0:
        return
    rearm = floor * 1.05
    path = _default_state_path()
    state = _load_state(path)
    if state.equity_floor_breached:
        if equity >= rearm:
            state.equity_floor_breached = False
            _save_state(path, state)
        return
    if equity < floor:
        cash = float(payload.get("cash_usd", 0))
        text = (
            f"\U0001f6a8 *Equity floor breached* — "
            f"{_md_escape(f'${equity:.2f}')} \\< {_md_escape(f'${floor:.2f}')}\n"
            f"Open positions: {int(payload.get('open_positions', 0))} — "
            f"cash: {_md_escape(f'${cash:.2f}')}"
        )
        if _post(text):
            state.equity_floor_breached = True
            _save_state(path, state)


def _handle_auto_tune_change(payload: dict[str, Any]) -> None:
    changes = payload.get("changes") or []
    if not changes:
        return
    lines = [f"\U0001f6e0 *Auto\\-tune* updated {len(changes)} params"]
    for change in changes:
        param = str(change.get("param", "?"))
        old = change.get("old", "?")
        new = change.get("new", "?")
        lines.append(
            f"`{param}`: {_md_escape(str(old))} → {_md_escape(str(new))}"
        )
    _post("\n".join(lines))


def notify_threshold(kind: str, payload: dict[str, Any]) -> None:
    if not is_enabled() or not _flag("TELEGRAM_ALERT_THRESHOLDS"):
        return
    if kind == "big_win":
        _handle_big_win(payload)
    elif kind == "big_loss":
        _handle_big_loss(payload)
    elif kind == "drawdown":
        _handle_drawdown(payload)
    elif kind == "equity_floor":
        _handle_equity_floor(payload)
    elif kind == "auto_tune_change":
        _handle_auto_tune_change(payload)


def notify_daily_summary(snapshot: dict[str, Any]) -> None:
    if not is_enabled() or not _flag("TELEGRAM_ALERT_DAILY_SUMMARY"):
        return
    today = str(snapshot.get("today") or dt.date.today().isoformat())
    path = _default_state_path()
    state = _load_state(path)
    if state.last_daily_summary_date == today:
        return
    equity = float(snapshot.get("equity_usd", 0))
    pct_24h = float(snapshot.get("equity_pct_24h", 0))
    pct_icon = _pnl_icon(pct_24h)
    sign = "+" if pct_24h >= 0 else "-"
    pct_str = _md_escape(f"{sign}{abs(pct_24h):.1f}%")
    cash = float(snapshot.get("cash_usd", 0))
    positions = int(snapshot.get("open_positions", 0))
    trades = int(snapshot.get("trades_24h", 0))
    wins = int(snapshot.get("wins_24h", 0))
    losses = int(snapshot.get("losses_24h", 0))
    win_rate = (wins / trades * 100) if trades > 0 else 0.0

    lines = [
        f"\U0001f4ca *Executive daily summary* — {_md_escape(today)}",
        f"*Portfolio:* Equity {_md_escape(f'${equity:.2f}')} \\({pct_icon} {pct_str} 24h\\) — Cash {_md_escape(f'${cash:.2f}')} — Open {positions}",
        f"*Closed trades:* {trades} \\(✅ {wins}W / ❌ {losses}L\\) — Win rate {_md_escape(f'{win_rate:.0f}%')}",
    ]
    if "unrealized_pnl_usd" in snapshot:
        value = float(snapshot.get("unrealized_pnl_usd") or 0.0)
        lines.append(f"*Unrealized PnL:* {_pnl_icon(value)} *{_md_escape(_fmt_money(value, signed=True))}*")
    if "realized_total_usd" in snapshot:
        value = float(snapshot.get("realized_total_usd") or 0.0)
        lines.append(f"*Realized all\\-time:* {_pnl_icon(value)} *{_md_escape(_fmt_money(value, signed=True))}*")
    if "realized_today_usd" in snapshot:
        value = float(snapshot.get("realized_today_usd") or 0.0)
        lines.append(f"*Realized today:* {_pnl_icon(value)} *{_md_escape(_fmt_money(value, signed=True))}*")
    top_w = snapshot.get("top_winner")
    if isinstance(top_w, dict) and top_w:
        pnl_w = float(top_w.get("pnl_usd", 0))
        lines.append(
            f"*Best closed trade:* ✅ *{_md_escape(f'+${pnl_w:.2f}')}* "
            f"on {_md_escape(str(top_w.get('title', '')))}"
        )
    top_l = snapshot.get("top_loser")
    if isinstance(top_l, dict) and top_l:
        pnl_l = float(top_l.get("pnl_usd", 0))
        lines.append(
            f"*Worst closed trade:* ❌ *{_md_escape(f'-${abs(pnl_l):.2f}')}* "
            f"on {_md_escape(str(top_l.get('title', '')))}"
        )
    if _post("\n".join(lines)):
        state.last_daily_summary_date = today
        _save_state(path, state)


def _fmt_money(value: float, *, signed: bool = False) -> str:
    sign = ""
    if signed:
        sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):.2f}" if signed else f"${value:.2f}"


def _pnl_icon(value: float) -> str:
    if value > 0:
        return "✅"
    if value < 0:
        return "❌"
    return "⚪"


def _fmt_price(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "?"


def _short(text: Any, limit: int = 72) -> str:
    raw = str(text or "")
    if len(raw) <= limit:
        return raw
    return raw[: limit - 1] + "…"


def _sort_by_pnl(items: list[Any], pnl_fn: Callable[[Any], float], *, reverse: bool) -> list[Any]:
    return sorted(items, key=pnl_fn, reverse=reverse)


def _line_for_open_position(position: dict[str, Any]) -> str:
    title = _short(position.get("question") or position.get("slug") or position.get("market_id") or "?", 48)
    outcome = _short(position.get("outcome") or "?", 14)
    stake = float(position.get("stake") or 0.0)
    pnl = float(position.get("unrealized_pnl") or 0.0)
    entry = _fmt_price(position.get("entry_price"))
    current = _fmt_price(position.get("current_price"))
    strategy = str(position.get("strategy") or "?")
    return (
        f"{_pnl_icon(pnl)} {_md_escape(title)} — *{_md_escape(outcome)}* "
        f"`{_md_escape(_fmt_money(stake))}` {_md_escape(entry)}→{_md_escape(current)} "
        f"uPnL *{_md_escape(_fmt_money(pnl, signed=True))}* `{_md_escape(strategy)}`"
    )


def _line_for_closed_trade(trade: dict[str, Any]) -> str:
    title = _short(trade.get("question") or trade.get("title") or trade.get("market_title") or "?", 48)
    outcome = _short(trade.get("outcome") or "?", 14)
    pnl = float(trade.get("realized_pnl") or trade.get("pnl_usd") or 0.0)
    strategy = str(trade.get("strategy") or "?")
    reason = str(trade.get("exit_reason") or trade.get("reason") or "")
    suffix = f" `{_md_escape(reason)}`" if reason else ""
    return (
        f"{_pnl_icon(pnl)} {_md_escape(title)} — *{_md_escape(outcome)}* "
        f"rPnL *{_md_escape(_fmt_money(pnl, signed=True))}* `{_md_escape(strategy)}`{suffix}"
    )


def _pnl_for_position(position: dict[str, Any]) -> float:
    try:
        return float(position.get("unrealized_pnl") or 0.0) + float(position.get("realized_pnl") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _pnl_for_trade(trade: dict[str, Any]) -> float:
    try:
        return float(trade.get("realized_pnl") or trade.get("pnl_usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def notify_portfolio_update(snapshot: dict[str, Any]) -> None:
    if not is_enabled() or not _flag("TELEGRAM_ALERT_PORTFOLIO_UPDATES"):
        return
    interval_min = _float_env("TELEGRAM_PORTFOLIO_UPDATE_MINUTES", 30.0)
    if interval_min <= 0:
        interval_min = 30.0
    path = _default_state_path()
    state = _load_state(path)
    now = time.time()
    if state.last_portfolio_update_ts is not None and (now - state.last_portfolio_update_ts) < interval_min * 60.0:
        return

    equity = float(snapshot.get("equity_usd", 0.0) or 0.0)
    cash = float(snapshot.get("cash_usd", 0.0) or 0.0)
    invested = float(snapshot.get("invested_usd", 0.0) or 0.0)
    unrealized = float(snapshot.get("unrealized_pnl_usd", 0.0) or 0.0)
    realized_total = float(snapshot.get("realized_total_usd", 0.0) or 0.0)
    realized_today = float(snapshot.get("realized_today_usd", 0.0) or 0.0)
    trades_today = int(snapshot.get("trades_today", 0) or 0)
    open_positions = snapshot.get("open_positions") if isinstance(snapshot.get("open_positions"), list) else []
    recent_trades = snapshot.get("recent_trades") if isinstance(snapshot.get("recent_trades"), list) else []
    winners = _sort_by_pnl(
        [("open", position) for position in open_positions if _pnl_for_position(position) >= 0]
        + [("closed", trade) for trade in recent_trades if _pnl_for_trade(trade) >= 0],
        lambda item: _pnl_for_position(item[1]) if item[0] == "open" else _pnl_for_trade(item[1]),
        reverse=True,
    )
    losers = _sort_by_pnl(
        [("open", position) for position in open_positions if _pnl_for_position(position) < 0]
        + [("closed", trade) for trade in recent_trades if _pnl_for_trade(trade) < 0],
        lambda item: _pnl_for_position(item[1]) if item[0] == "open" else _pnl_for_trade(item[1]),
        reverse=False,
    )

    sections: list[list[str]] = [
        [
            f"\U0001f4ca *Director review* — {_md_escape(str(snapshot.get('timestamp') or ''))}",
            f"*Equity* {_md_escape(_fmt_money(equity))} | *Cash* {_md_escape(_fmt_money(cash))} | *Invested* {_md_escape(_fmt_money(invested))}",
            f"*PnL* unrealized {_pnl_icon(unrealized)} {_md_escape(_fmt_money(unrealized, signed=True))} | today {_pnl_icon(realized_today)} {_md_escape(_fmt_money(realized_today, signed=True))} | all\\-time {_pnl_icon(realized_total)} {_md_escape(_fmt_money(realized_total, signed=True))}",
            f"*Activity* {trades_today} closed trades | {len(open_positions)} open positions",
        ]
    ]
    if winners:
        section = ["✅ *Top winners*"]
        for kind, item in winners[:5]:
            section.append(_line_for_open_position(item) if kind == "open" else _line_for_closed_trade(item))
        sections.append(section)
    if losers:
        section = ["❌ *Top losers*"]
        for kind, item in losers[:5]:
            section.append(_line_for_open_position(item) if kind == "open" else _line_for_closed_trade(item))
        sections.append(section)
    if open_positions:
        section = ["\U0001f4cc *Open book*"]
        section.extend(_line_for_open_position(position) for position in open_positions)
        sections.append(section)

    text = "\n\n".join("\n".join(section) for section in sections)
    if len(text) > 3900:
        text = text[:3850] + "\n…truncated"
    if _post(text):
        state.last_portfolio_update_ts = now
        _save_state(path, state)
