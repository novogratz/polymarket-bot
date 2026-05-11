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
    last_daily_summary_equity_usd: float | None = None
    last_daily_summary_cash_usd: float | None = None
    last_portfolio_update_ts: float | None = None
    last_portfolio_update_equity_usd: float | None = None
    last_portfolio_update_cash_usd: float | None = None
    last_portfolio_update_unrealized_usd: float | None = None
    dedupe_seen: dict[str, float] = field(default_factory=dict)
    drawdown_armed: bool = False  # True quand on a déjà alerté sur ce pic
    last_post_ts: float = 0.0
    big_win_progress_notified: dict[str, float] = field(default_factory=dict)  # token_id -> timestamp


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
        last_daily_summary_equity_usd=data.get("last_daily_summary_equity_usd"),
        last_daily_summary_cash_usd=data.get("last_daily_summary_cash_usd"),
        last_portfolio_update_ts=data.get("last_portfolio_update_ts"),
        last_portfolio_update_equity_usd=data.get("last_portfolio_update_equity_usd"),
        last_portfolio_update_cash_usd=data.get("last_portfolio_update_cash_usd"),
        last_portfolio_update_unrealized_usd=data.get("last_portfolio_update_unrealized_usd"),
        dedupe_seen={str(k): float(v) for k, v in (data.get("dedupe_seen") or {}).items()},
        drawdown_armed=bool(data.get("drawdown_armed", False)),
        last_post_ts=float(data.get("last_post_ts", 0.0)),
        big_win_progress_notified={str(k): float(v) for k, v in (data.get("big_win_progress_notified") or {}).items()},
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
        "last_daily_summary_equity_usd": state.last_daily_summary_equity_usd,
        "last_daily_summary_cash_usd": state.last_daily_summary_cash_usd,
        "last_portfolio_update_ts": state.last_portfolio_update_ts,
        "last_portfolio_update_equity_usd": state.last_portfolio_update_equity_usd,
        "last_portfolio_update_cash_usd": state.last_portfolio_update_cash_usd,
        "last_portfolio_update_unrealized_usd": state.last_portfolio_update_unrealized_usd,
        "dedupe_seen": state.dedupe_seen,
        "drawdown_armed": state.drawdown_armed,
        "last_post_ts": state.last_post_ts,
        "big_win_progress_notified": state.big_win_progress_notified,
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

    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as resp:
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt == 0:
                # Rate limited. Try to extract retry-after or just wait 5s.
                retry_after = 5
                try:
                    retry_after = int(exc.headers.get("Retry-After", "5"))
                except (TypeError, ValueError):
                    pass
                print(f"[notif] rate limited (429), waiting {retry_after}s...", file=sys.stderr, flush=True)
                time.sleep(min(retry_after, 30))
                continue
            print(f"[notif] failed: {exc}", file=sys.stderr, flush=True)
            return False
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            print(f"[notif] failed: {exc}", file=sys.stderr, flush=True)
            return False
    return False


def _get_transport() -> Transport:
    return _transport_override if _transport_override is not None else _default_transport


def _post(text: str) -> bool:
    """Envoi best-effort. Retourne False sur erreur, jamais d'exception."""
    if not is_enabled():
        return False

    path = _default_state_path()
    state = _load_state(path)
    now = time.time()

    # Telegram rate limit: ~1 msg/sec per chat.
    # We enforce a small gap to be safe.
    elapsed = now - state.last_post_ts
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
        now = time.time()

    payload = {
        "chat_id": _chat_id(),
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }
    try:
        success = bool(_get_transport()(payload))
        if success:
            state.last_post_ts = time.time()
            _save_state(path, state)
        return success
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
    if _post("\n".join(lines)):
        signal["telegram_buy_notified"] = True


def notify_sync_import(positions: list[dict[str, Any]]) -> None:
    """Notifie un batch de positions importées/synchronisées."""
    if not is_enabled() or not _flag("TELEGRAM_ALERT_TRADES") or not positions:
        return

    # Ne notifier que ceux qui n'ont pas encore été notifiés
    to_notify = [p for p in positions if not p.get("telegram_buy_notified")]
    if not to_notify:
        return

    if len(to_notify) == 1:
        p = to_notify[0]
        # On délègue à notify_trade_buy pour le format standard
        notify_trade_buy(
            market_title=str(p.get("question") or ""),
            token_id=str(p.get("token_id") or ""),
            price=float(p.get("entry_price") or 0.0),
            size_usd=float(p.get("stake") or 0.0),
            signal={"tag": str(p.get("strategy") or "live_sync")},
            outcome=str(p.get("outcome") or ""),
            market_url=str(p.get("url") or ""),
        )
        p["telegram_buy_notified"] = True
        return

    lines = [f"🔄 *Sync: {len(to_notify)} positions imported/updated*"]
    for p in to_notify[:20]:  # Limite pour éviter de dépasser la taille max
        title = _short(p.get("question") or p.get("token_id") or "?", 48)
        price = float(p.get("entry_price") or 0.0)
        size = float(p.get("stake") or 0.0)
        lines.append(f"• `${_md_escape(f'{size:.1f}')}` @ `{_md_escape(f'{price:.2f}')}` *{_md_escape(title)}*")

    if len(to_notify) > 20:
        lines.append(f"_... and {len(to_notify) - 20} more_")

    if _post("\n".join(lines)):
        for p in to_notify:
            p["telegram_buy_notified"] = True


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
    if realized_pnl_usd > 0:
        lines = [
            f"\U0001f7e2✅ *WIN SELL* `${_md_escape(f'{size_usd:.2f}')}` @ `{_md_escape(f'{price:.2f}')}` — `{reason}`",
            f"*{_md_escape(market_title)}*",
            f"\U0001f7e2✅ *PROFIT* *{pnl_str}*{pct_str} ✅{held_line}",
            _md_escape("Nice win locked in."),
        ]
        _post("\n".join(lines))
        return
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


def _handle_big_win_in_progress(payload: dict[str, Any]) -> None:
    threshold = _float_env("TELEGRAM_BIG_WIN_IN_PROGRESS_PCT", 20.0)
    pnl_pct = float(payload.get("pnl_pct", 0))
    if pnl_pct < threshold:
        return
    token_id = str(payload.get("token_id", ""))
    if not token_id:
        return
    path = _default_state_path()
    state = _load_state(path)
    now = time.time()
    dedup_window = _dedupe_window_sec() * 3
    last_notified = state.big_win_progress_notified.get(token_id, 0.0)
    if now - last_notified < dedup_window:
        return
    title = str(payload.get("market_title", ""))
    bid = float(payload.get("bid", 0))
    pnl_usd = _optional_float(payload.get("pnl_usd"))
    pnl_usd_str = _fmt_loud_win_usd(pnl_usd) if pnl_usd is not None else "USD PnL pending"
    pnl_pct_str = _fmt_loud_win_pct(pnl_pct)
    title_str = f" on *{_md_escape(title)}*" if title else ""
    text = (
        f"\U0001f7e2✅✅✅ *BIG WIN IN PROGRESS* ✅✅✅\n"
        f"*{pnl_usd_str}*  *{pnl_pct_str}* {title_str}\n"
        f"{_md_escape('MAKE SOME NOISE!!!!!!')} @pmarx\n"
        f"Bid: {_md_escape(f'{bid:.3f}')}"
    )
    if _post(text):
        state.big_win_progress_notified[token_id] = now
        _save_state(path, state)


def _handle_big_win(payload: dict[str, Any]) -> None:
    threshold = _float_env("TELEGRAM_BIG_WIN_USD", 10.0)
    pnl = float(payload.get("pnl_usd", 0))
    if pnl < threshold:
        return
    reason = str(payload.get("reason", ""))
    title = str(payload.get("market_title", ""))
    pnl_pct = payload.get("pnl_pct")
    pnl_pct_value = _optional_float(pnl_pct)
    pnl_pct_str = f"  *{_fmt_loud_win_pct(pnl_pct_value)}*" if pnl_pct_value is not None else ""
    held_str = _fmt_held(payload.get("held_seconds"))
    held_line = f" after {_md_escape(held_str)}" if held_str else ""
    text = (
        f"\U0001f7e2✅✅✅ *BIG WIN* ✅✅✅\n"
        f"*{_fmt_loud_win_usd(pnl)}*{pnl_pct_str} on *{_md_escape(title)}*\n"
        f"{_md_escape('MAKE SOME NOISE!!!!!!')} @pmarx\n"
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
    pnl_pct = payload.get("pnl_pct")
    pnl_pct_str = f" ({_md_escape(f'{pnl_pct:.1f}%')})" if pnl_pct is not None else ""
    held_str = _fmt_held(payload.get("held_seconds"))
    held_line = f" after {_md_escape(held_str)}" if held_str else ""
    text = (
        f"\U0001f4b8 *BIG LOSS* {_md_escape(f'-${abs(pnl):.2f}')}{pnl_pct_str} on *{_md_escape(title)}*\n"
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
    elif kind == "big_win_in_progress":
        _handle_big_win_in_progress(payload)
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
        f"\U0001f4ca *Director daily review* — {_md_escape(today)}",
        (
            f"*Equity* {_md_escape(f'${equity:.2f}')}"
            f"{_fmt_delta_suffix(equity, state.last_daily_summary_equity_usd)}"
            f" \\({pct_icon} {pct_str} 24h\\) — *Cash* {_md_escape(f'${cash:.2f}')} — *Open* {positions}"
        ),
        f"*Activity* {trades} closed trades — ✅ {wins}W / ❌ {losses}L — Win rate {_md_escape(f'{win_rate:.0f}%')}",
    ]
    if state.last_daily_summary_date:
        lines.append(f"*Last daily review* {_md_escape(state.last_daily_summary_date)}")
    if "unrealized_pnl_usd" in snapshot:
        value = float(snapshot.get("unrealized_pnl_usd") or 0.0)
        lines.append(f"*PnL* unrealized {_pnl_icon(value)} {_md_escape(_fmt_money(value, signed=True))}")
    if "realized_total_usd" in snapshot:
        value = float(snapshot.get("realized_total_usd") or 0.0)
        lines.append(f"*All\\-time realized* {_pnl_icon(value)} {_md_escape(_fmt_money(value, signed=True))}")
    if "realized_today_usd" in snapshot:
        value = float(snapshot.get("realized_today_usd") or 0.0)
        lines.append(f"*Today realized* {_pnl_icon(value)} {_md_escape(_fmt_money(value, signed=True))}")
    top_w = snapshot.get("top_winner")
    if isinstance(top_w, dict) and top_w:
        pnl_w = float(top_w.get("pnl_usd", 0))
        lines.append(
            f"✅ *Best* {_md_escape(f'+${pnl_w:.2f}')} "
            f"on {_md_escape(str(top_w.get('title', '')))}"
        )
    top_l = snapshot.get("top_loser")
    if isinstance(top_l, dict) and top_l:
        pnl_l = float(top_l.get("pnl_usd", 0))
        lines.append(
            f"❌ *Worst* {_md_escape(f'-${abs(pnl_l):.2f}')} "
            f"on {_md_escape(str(top_l.get('title', '')))}"
        )
    if _post("\n".join(lines)):
        state = _load_state(path)
        state.last_daily_summary_date = today
        state.last_daily_summary_equity_usd = equity
        state.last_daily_summary_cash_usd = cash
        _save_state(path, state)


def _fmt_money(value: float, *, signed: bool = False) -> str:
    sign = ""
    if signed:
        sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):.2f}" if signed else f"${value:.2f}"


def _fmt_minutes_ago(seconds: float) -> str:
    minutes = max(0, int(round(seconds / 60.0)))
    if minutes < 60:
        return f"{minutes}m ago"
    hours, rem = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{rem:02d}m ago"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h ago"


def _fmt_delta_suffix(current: float, previous: float | None) -> str:
    if previous is None:
        return ""
    delta = round(current - previous, 2)
    amount = f"{'+' if delta >= 0 else '-'}${abs(delta):.2f} USD"
    return f" \\({_pnl_icon(delta)} {_md_escape(amount)}\\)"


def _fmt_equity_delta(label: str, value: float | None, pct: float | None = None) -> str:
    if value is None:
        return f"{_md_escape(label)} n/a"
    pct_str = f" \\({_md_escape(f'{pct:+.1f}%')}\\)" if pct is not None else ""
    amount = _md_escape(_fmt_money(value, signed=True))
    if value >= 0:
        amount = f"*{amount}*"
    return f"{_md_escape(label)} {_pnl_icon(value)} {amount}{pct_str}"


def _pnl_icon(value: float) -> str:
    if value > 0:
        return "✅"
    if value < 0:
        return "❌"
    return "⚪"


def _optional_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_loud_win_usd(value: float | None) -> str:
    if value is None:
        return _md_escape("USD PnL pending")
    return _md_escape(f"🟢 +${abs(value):.2f} USD 🟢")


def _fmt_loud_win_pct(value: float | None) -> str:
    if value is None:
        return _md_escape("+?%")
    return _md_escape(f"🟢 +{abs(value):.1f}% 🟢")


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
    pnl_pct = (pnl / stake * 100.0) if stake > 0 else None
    entry = _fmt_price(position.get("entry_price"))
    current = _fmt_price(position.get("current_price"))
    strategy = str(position.get("strategy") or "?")
    threshold_pct = _float_env("TELEGRAM_BIG_WIN_IN_PROGRESS_PCT", 20.0)
    threshold_usd = _float_env("TELEGRAM_BIG_WIN_USD", 10.0)
    if pnl > 0 and (pnl >= threshold_usd or (pnl_pct is not None and pnl_pct >= threshold_pct)):
        pct_part = f" *{_fmt_loud_win_pct(pnl_pct)}*" if pnl_pct is not None else ""
        return (
            f"\U0001f7e2✅✅ *WIN IN PROGRESS* *{_fmt_loud_win_usd(pnl)}*{pct_part} @pmarx\n"
            f"{_md_escape(title)} — *{_md_escape(outcome)}* "
            f"`{_md_escape(_fmt_money(stake))}` {_md_escape(entry)}→{_md_escape(current)} "
            f"`{_md_escape(strategy)}`"
        )
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


def _stake_for_position(position: dict[str, Any]) -> float:
    try:
        return float(position.get("stake") or 0.0)
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
    total_pnl = float(snapshot.get("total_pnl_usd", realized_total + unrealized) or 0.0)
    today_pnl = float(snapshot.get("today_pnl_usd", realized_today + unrealized) or 0.0)
    trades_today = int(snapshot.get("trades_today", 0) or 0)
    wins_today = int(snapshot.get("wins_today", 0) or 0)
    losses_today = int(snapshot.get("losses_today", 0) or 0)
    total_wins = int(snapshot.get("total_wins", 0) or 0)
    total_losses = int(snapshot.get("total_losses", 0) or 0)
    open_positions = snapshot.get("open_positions") if isinstance(snapshot.get("open_positions"), list) else []
    equity_30m_delta = (
        round(equity - float(state.last_portfolio_update_equity_usd), 2)
        if state.last_portfolio_update_equity_usd is not None
        else None
    )
    equity_30m_pct = (
        round(equity_30m_delta / state.last_portfolio_update_equity_usd * 100.0, 1)
        if equity_30m_delta is not None and state.last_portfolio_update_equity_usd
        else None
    )
    today_pnl_pct = (
        round(today_pnl / (equity - today_pnl) * 100.0, 1)
        if today_pnl != 0 and (equity - today_pnl) != 0
        else None
    )
    total_pnl_pct = (
        round(total_pnl / (equity - total_pnl) * 100.0, 1)
        if total_pnl != 0 and (equity - total_pnl) != 0
        else None
    )
    equity_parts = [
        _fmt_equity_delta("30m", equity_30m_delta, pct=equity_30m_pct),
        _fmt_equity_delta("Today", today_pnl, pct=today_pnl_pct),
        _fmt_equity_delta("All-time", total_pnl, pct=total_pnl_pct),
    ]

    equity_pct_str = f" \\({_md_escape(f'{total_pnl_pct:+.1f}%')}\\)" if total_pnl_pct is not None else ""
    equity_amount = _md_escape(_fmt_money(equity)) if total_pnl <= 0 else f"*{_md_escape(_fmt_money(equity))}*"
    sections: list[list[str]] = [
        [
            f"\U0001f4ca *Director review* — {_md_escape(str(snapshot.get('timestamp') or ''))}",
            "",
            f"{'✅' if total_pnl > 0 else '⚪'} *Equity* {equity_amount}{equity_pct_str} — *Cash* {_md_escape(_fmt_money(cash))}",
            *[f"  — {part}" for part in equity_parts],
            "",
            f"*Invested* {_md_escape(_fmt_money(invested))}",
            f"*Today* {trades_today} trades ✅ {wins_today} / ❌ {losses_today}",
            f"*All\\-time* {total_wins + total_losses} trades ✅ {total_wins} / ❌ {total_losses} \\({_md_escape(f'{total_wins / max(total_wins + total_losses, 1) * 100.0:.0f}%')}\\)",
            f"*Open positions* {len(open_positions)}",
        ]
    ]
    last_lines: list[str] = []
    if state.last_portfolio_update_ts is not None:
        last_lines.append(
            f"*Last 30m review* {_md_escape(_fmt_minutes_ago(now - state.last_portfolio_update_ts))}"
        )
    if last_lines:
        sections[0].extend(last_lines)

    current_len = sum(len("\n".join(s)) for s in sections) + 20

    if open_positions:
        section = ["\U0001f4cc *Open book*"]
        added = 0
        groups = [
            ("*Big trades \\> $50*", [p for p in open_positions if _stake_for_position(p) > 50.0], None),
            ("*Smaller trades*", [p for p in open_positions if _stake_for_position(p) <= 50.0], 5),
        ]
        for heading, positions, limit in groups:
            if not positions:
                continue
            positions_sorted = sorted(positions, key=_stake_for_position, reverse=True)
            positions_to_show = positions_sorted[:limit] if limit is not None else positions_sorted
            section.append("")
            section.append(heading)
            for position in positions_to_show:
                line = _line_for_open_position(position)
                if current_len + len(line) > 3900:
                    section.append("")
                    section.append(f"_… and {len(open_positions) - added} more positions_")
                    break
                section.append("")
                section.append(line)
                current_len += len(line) + 2
                added += 1
            hidden = len(positions_sorted) - len(positions_to_show)
            if hidden > 0:
                section.append("")
                section.append(f"_… and {hidden} smaller positions hidden_")
            if added >= len(open_positions):
                break
        sections.append(section)

    text = "\n\n".join("\n".join(section) for section in sections)
    _post(text)
    state = _load_state(path)
    state.last_portfolio_update_ts = now
    state.last_portfolio_update_equity_usd = equity
    state.last_portfolio_update_cash_usd = cash
    state.last_portfolio_update_unrealized_usd = unrealized
    _save_state(path, state)
