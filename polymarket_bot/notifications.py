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
    dedupe_seen: dict[str, dict[str, Any]] = field(default_factory=dict)
    drawdown_armed: bool = False  # True quand on a déjà alerté sur ce pic
    last_heartbeat_ts: float | None = None


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
        dedupe_seen=_migrate_dedupe(data.get("dedupe_seen")),
        drawdown_armed=bool(data.get("drawdown_armed", False)),
        last_heartbeat_ts=(
            float(data["last_heartbeat_ts"])
            if data.get("last_heartbeat_ts") is not None
            else None
        ),
    )


def _migrate_dedupe(raw: Any) -> dict[str, dict[str, Any]]:
    """Migre `dedupe_seen` de l'ancien format float vers la structure imbriquée."""
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if isinstance(v, (int, float)):
            out[str(k)] = {
                "first_ts": float(v),
                "last_ts": float(v),
                "count": 1,
                "last_message": "",
            }
        elif isinstance(v, dict):
            out[str(k)] = {
                "first_ts": float(v.get("first_ts", 0) or 0),
                "last_ts": float(v.get("last_ts", 0) or 0),
                "count": int(v.get("count", 1) or 1),
                "last_message": str(v.get("last_message", "") or ""),
            }
    return out


def _dedupe_window_sec() -> float:
    try:
        return float(os.environ.get("TELEGRAM_DEDUPE_WINDOW_SEC", "300"))
    except ValueError:
        return 300.0


def _prune_dedupe(state: _State, now: float, window: float) -> None:
    """Supprime les entrées dont le `last_ts` dépasse `window × 4`."""
    cutoff = now - (window * 4)
    state.dedupe_seen = {
        k: v for k, v in state.dedupe_seen.items()
        if float(v.get("last_ts", 0)) >= cutoff
    }


def _save_state(path: Path, state: _State) -> None:
    payload = {
        "equity_peak_usd": state.equity_peak_usd,
        "equity_floor_breached": state.equity_floor_breached,
        "dedupe_seen": state.dedupe_seen,
        "drawdown_armed": state.drawdown_armed,
        "last_heartbeat_ts": state.last_heartbeat_ts,
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


def _fmt_amount(amount: float) -> str:
    """Formate un montant USD : $X.YY sous 1k, $X.Yk au-delà (en valeur absolue)."""
    abs_amt = abs(amount)
    if abs_amt >= 1000:
        sign = "-" if amount < 0 else ""
        return f"${sign}{abs_amt/1000:.1f}k"
    return f"${amount:.2f}"


def _truncate(text: str, max_len: int = 40) -> str:
    """Tronque avec ellipse `…` si > max_len ; longueur résultante == max_len."""
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def notify_trade_buy(
    *,
    market_title: str,
    token_id: str,
    price: float,
    size_usd: float,
    signal: dict[str, Any],
    market_url: str | None = None,
) -> None:
    if not is_enabled() or not _flag("TELEGRAM_ALERT_TRADES"):
        return
    wallets = int(signal.get("wallets", 0) or 0)
    copied = float(signal.get("copied_usdc", 0) or 0)
    tag = signal.get("tag")

    if tag:
        signal_part = f"tag\\={_md_escape(str(tag))}"
    elif wallets > 0:
        if copied >= 1000:
            copied_str = f"${copied/1000:.1f}k"
        else:
            copied_str = f"${copied:.0f}"
        signal_part = f"{wallets}w {_md_escape(copied_str)}"
    else:
        signal_part = None

    title = _truncate(market_title or "", 40)
    size_str = _md_escape(_fmt_amount(size_usd))
    price_str = _md_escape(f"{price:.2f}")
    parts = [
        f"🟢 *BUY* `{size_str}` @ `{price_str}`",
        f"*{_md_escape(title)}*",
    ]
    if signal_part:
        parts.append(signal_part)
    if market_url:
        parts.append(f"[🔗]({market_url})")
    _post(" · ".join(parts))


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
    win_thresh = _float_env("TELEGRAM_BIG_WIN_USD", 10.0)
    loss_thresh = _float_env("TELEGRAM_BIG_LOSS_USD", 5.0)
    thresholds_on = _flag("TELEGRAM_ALERT_THRESHOLDS")
    if thresholds_on and realized_pnl_usd >= win_thresh:
        emoji, label = "💰", "BIG WIN"
    elif thresholds_on and realized_pnl_usd <= -loss_thresh:
        emoji, label = "💸", "BIG LOSS"
    else:
        emoji, label = "🔴", "SELL"

    title = _truncate(market_title or "", 40)
    size_str = _md_escape(_fmt_amount(size_usd))
    price_str = _md_escape(f"{price:.2f}")
    sign = "+" if realized_pnl_usd >= 0 else "-"
    pnl_abs_str = _fmt_amount(abs(realized_pnl_usd))
    pnl_str = f"{_md_escape(sign)}{_md_escape(pnl_abs_str)}"
    pct_str = ""
    if realized_pnl_pct is not None:
        sign_pct = "+" if realized_pnl_pct >= 0 else "-"
        pct_str = f" \\({_md_escape(f'{sign_pct}{abs(realized_pnl_pct):.1f}%')}\\)"
    held_str = _fmt_held(held_seconds)
    held_part = f" {_md_escape(held_str)}" if held_str else ""

    parts = [
        f"{emoji} *{label}* `{size_str}` @ `{price_str}`",
        f"*{_md_escape(title)}*",
        f"{pnl_str}{pct_str}{held_part}",
        f"`{_md_escape(reason)}`",
    ]
    _post(" · ".join(parts))


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


def _handle_drawdown(payload: dict[str, Any]) -> None:
    equity = float(payload.get("equity_usd", 0) or 0)
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
            f"⚠️ *DD* {_md_escape(f'-{drawdown_pct:.1f}%')} · "
            f"eq {_md_escape(_fmt_amount(equity))} "
            f"\\(pic {_md_escape(_fmt_amount(peak))}\\)"
        )
        if _post(text):
            state.drawdown_armed = True
    _save_state(path, state)


def _handle_equity_floor(payload: dict[str, Any]) -> None:
    equity = float(payload.get("equity_usd", 0) or 0)
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
        cash = float(payload.get("cash_usd", 0) or 0)
        positions = int(payload.get("open_positions", 0) or 0)
        text = (
            f"🚨 *Floor* · eq {_md_escape(_fmt_amount(equity))} "
            f"\\< {_md_escape(_fmt_amount(floor))} · "
            f"{positions}pos cash {_md_escape(_fmt_amount(cash))}"
        )
        if _post(text):
            state.equity_floor_breached = True
            _save_state(path, state)


def _handle_auto_tune_change(payload: dict[str, Any]) -> None:
    changes = payload.get("changes") or []
    if not changes:
        return
    parts: list[str] = []
    for change in changes:
        param = str(change.get("param", "?"))
        old = change.get("old", "?")
        new = change.get("new", "?")
        parts.append(f"`{_md_escape(param)}` {_md_escape(str(old))}→{_md_escape(str(new))}")
    text = "🛠 *Tune* · " + ", ".join(parts)
    _post(text)


def notify_threshold(kind: str, payload: dict[str, Any]) -> None:
    if not is_enabled() or not _flag("TELEGRAM_ALERT_THRESHOLDS"):
        return
    if kind == "drawdown":
        _handle_drawdown(payload)
    elif kind == "equity_floor":
        _handle_equity_floor(payload)
    elif kind == "auto_tune_change":
        _handle_auto_tune_change(payload)
    # big_win / big_loss : intégrés à notify_trade_sell, ignorés ici


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
    sign = "+" if pct_24h >= 0 else "-"
    pct_str = _md_escape(f"{sign}{abs(pct_24h):.1f}%")
    cash = float(snapshot.get("cash_usd", 0))
    positions = int(snapshot.get("open_positions", 0))
    trades = int(snapshot.get("trades_24h", 0))
    wins = int(snapshot.get("wins_24h", 0))
    losses = int(snapshot.get("losses_24h", 0))
    win_rate = (wins / trades * 100) if trades > 0 else 0.0

    lines = [
        f"\U0001f4ca *Daily summary* — {_md_escape(today)}",
        f"Equity: *{_md_escape(f'${equity:.2f}')}* \\({pct_str} 24h\\)",
        f"Cash: {_md_escape(f'${cash:.2f}')} — Positions: {positions}",
        f"Trades 24h: {trades} \\({wins}W / {losses}L\\) — Win rate {_md_escape(f'{win_rate:.0f}%')}",
    ]
    top_w = snapshot.get("top_winner")
    if isinstance(top_w, dict) and top_w:
        pnl_w = float(top_w.get("pnl_usd", 0))
        lines.append(
            f"Top winner: *{_md_escape(f'+${pnl_w:.2f}')}* "
            f"on {_md_escape(str(top_w.get('title', '')))}"
        )
    top_l = snapshot.get("top_loser")
    if isinstance(top_l, dict) and top_l:
        pnl_l = float(top_l.get("pnl_usd", 0))
        lines.append(
            f"Top loser: *{_md_escape(f'-${abs(pnl_l):.2f}')}* "
            f"on {_md_escape(str(top_l.get('title', '')))}"
        )
    if _post("\n".join(lines)):
        state.last_daily_summary_date = today
        _save_state(path, state)
