"""Telegram push-only notifications. Best-effort silent.

Sans TELEGRAM_BOT_TOKEN défini, toutes les fonctions sont no-op et
``is_enabled()`` retourne False. Aucune exception n'est jamais
remontée — toute erreur est loggée sur stdout puis ignorée.
"""

from __future__ import annotations

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
    dedupe_seen: dict[str, float] = field(default_factory=dict)


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
        dedupe_seen={str(k): float(v) for k, v in (data.get("dedupe_seen") or {}).items()},
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
        "dedupe_seen": state.dedupe_seen,
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
    # Implémentation détaillée dans une tâche ultérieure.


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
    # Implémentation détaillée dans une tâche ultérieure.


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


def notify_threshold(kind: str, payload: dict[str, Any]) -> None:
    if not is_enabled() or not _flag("TELEGRAM_ALERT_THRESHOLDS"):
        return
    # Implémentation détaillée dans une tâche ultérieure.


def notify_daily_summary(snapshot: dict[str, Any]) -> None:
    if not is_enabled() or not _flag("TELEGRAM_ALERT_DAILY_SUMMARY"):
        return
    # Implémentation détaillée dans une tâche ultérieure.
