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

from ._atomic_io import atomic_write_text

_HTTP_TIMEOUT_SEC = 5.0

# Transport: callable qui prend un dict payload (chat_id, text, parse_mode)
# et retourne True si l'envoi a réussi. Injectable pour les tests.
Transport = Callable[[dict[str, Any]], bool]

_transport_override: Transport | None = None

# Mode flag set explicitly by the CLI entrypoint (see ``main.py``).
# When ``None``, we fall back to reading ``POLYMARKET_DRY_RUN`` for
# backwards compatibility with scripts that bypass the CLI flag plumbing.
_dry_run_override: bool | None = None


def _reset_for_tests() -> None:
    """Réinitialise l'état module entre tests."""
    global _transport_override, _dry_run_override
    _transport_override = None
    _dry_run_override = None


def set_transport_for_test(transport: Transport | None) -> None:
    """Injecte un transport custom (tests uniquement)."""
    global _transport_override
    _transport_override = transport


def set_dry_run(dry_run: bool) -> None:
    """Override the dry-run flag explicitly (call once from the CLI).

    Avoids the fragile coupling of reading ``POLYMARKET_DRY_RUN`` on every
    notification, which would diverge from ``Settings.dry_run`` if the env
    var changes mid-process.
    """
    global _dry_run_override
    _dry_run_override = bool(dry_run)


def _bot_token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def _is_dry_run() -> bool:
    if _dry_run_override is not None:
        return _dry_run_override
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
        atomic_write_text(path, json.dumps(payload, indent=2))
    except OSError as exc:
        print(f"[notif] failed to save state: {exc}", file=sys.stderr, flush=True)


def _default_transport(payload: dict[str, Any]) -> bool:
    """Transport par défaut: POST sur api.telegram.org via urllib.

    On HTTP 400 from Telegram (typically MarkdownV2 parse errors caused
    by an unescaped char in a strategy name), retry the same message
    with parse_mode stripped — plain text always goes through.
    """
    token = _bot_token()
    if not token:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def _send(p: dict[str, Any]) -> tuple[bool, str]:
        data = json.dumps(p).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as resp:
                return (200 <= resp.status < 300), ""
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            return False, f"HTTP {exc.code}: {body}"
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return False, f"{type(exc).__name__}: {exc}"

    ok, err = _send(payload)
    if ok:
        return True
    # 400 → typically parse_mode error → retry as plain text
    if "HTTP 400" in err and payload.get("parse_mode"):
        retry = {**payload}
        retry.pop("parse_mode", None)
        ok, err2 = _send(retry)
        if ok:
            return True
        err = f"{err} | retry-plain: {err2}"
    print(f"[notif] failed: {err}", file=sys.stderr, flush=True)
    return False


def _get_transport() -> Transport:
    return _transport_override if _transport_override is not None else _default_transport


def _run_prefix() -> str:
    """Retourne ``*\\[<run>\\]*\\n`` si ``POLYMARKET_RUN_NAME`` est défini.

    Utilisé par ``_post`` pour préfixer chaque message Telegram avec le
    nom du run/contexte (``baseline-A``, ``test-noise``, ``live``, …)
    sur sa propre ligne, pratique pour distinguer plusieurs auto-loop
    tournant en parallèle.
    """
    run = os.environ.get("POLYMARKET_RUN_NAME", "").strip()
    if not run:
        return ""
    return f"*\\[{_md_escape(run)}\\]*\n"


def _post(text: str) -> bool:
    """Envoi best-effort. Retourne False sur erreur, jamais d'exception."""
    if not is_enabled():
        return False
    payload = {
        "chat_id": _chat_id(),
        "text": _run_prefix() + text,
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


def _fmt_money_fr(amount: float) -> str:
    """Convention FR : signe `$` après le nombre (`92.34$`, `-1.20$`, `1.2k$`)."""
    abs_amt = abs(amount)
    sign = "-" if amount < 0 else ""
    if abs_amt >= 1000:
        return f"{sign}{abs_amt/1000:.1f}k$"
    return f"{sign}{abs_amt:.2f}$"


def _fmt_signed_money_fr(amount: float) -> str:
    """Montant signé FR : `+3.40$`, `-1.20$`, `0.00$` (sans signe quand nul)."""
    if amount > 0:
        return f"+{_fmt_money_fr(amount)}"
    return _fmt_money_fr(amount)


def _truncate(text: str, max_len: int = 40) -> str:
    """Tronque avec ellipse `…` si > max_len ; longueur résultante == max_len."""
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _journal_stats() -> tuple[float, int, int]:
    """Return (total_realized_pnl_usd, wins, losses) from the trade journal.

    A "win" is a journal entry whose net realized_pnl > 0; loss is < 0.
    Zero-PnL entries (rare; usually fee-only exits) count as neither.
    """
    path = Path(os.environ.get("POLYMARKET_TRADE_JOURNAL_PATH", "data/trade_journal.jsonl"))
    cache_path = Path(os.environ.get("POLYMARKET_REALIZED_CACHE_PATH", str(path.parent / "realized_trade_cache.jsonl")))
    total = 0.0
    wins = 0
    losses = 0
    seen: set[str] = set()
    for source in (path, cache_path):
        try:
            fh = source.open("r", encoding="utf-8")
        except (FileNotFoundError, OSError):
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entry_pnl = 0.0
                if rec.get("realized_pnl") is not None:
                    try:
                        entry_pnl += float(rec["realized_pnl"])
                    except (TypeError, ValueError):
                        pass
                elif rec.get("realized_pnl_usd") is not None:
                    try:
                        entry_pnl += float(rec["realized_pnl_usd"])
                    except (TypeError, ValueError):
                        pass
                for exit_rec in rec.get("exits") or []:
                    try:
                        entry_pnl += float(exit_rec.get("realized_pnl") or 0)
                    except (TypeError, ValueError):
                        pass
                dedupe_key = "|".join(
                    (
                        str(rec.get("token_id") or ""),
                        str(rec.get("closed_at") or ""),
                        str(rec.get("exit_reason") or rec.get("reason") or ""),
                        f"{entry_pnl:.4f}",
                    )
                )
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                total += entry_pnl
                if entry_pnl > 0.005:
                    wins += 1
                elif entry_pnl < -0.005:
                    losses += 1
    return total, wins, losses


def _fmt_all_time_line(
    unrealized: float = 0.0,
    *,
    total_override: float | None = None,
    extra_pnl: float = 0.0,
    return_pct: float | None = None,
    wins_override: int | None = None,
    losses_override: int | None = None,
) -> str:
    """All-time PnL + win-rate, each color-coded.

    Layout: `✅ All-time: +$X.XX  •  🟢 W/L 67% (8W/4L)`.
    No closed trades yet -> `⚪ All-time: $0.00  •  ⚪ W/L --`.

    ``unrealized`` (optional, USD) is added to the realized total so a
    currently-winning open position surfaces in the heartbeat line.

    ``extra_pnl`` is added on top of the journal total (used by sell
    notifications so the just-closed trade is counted before its journal
    record is written).
    """
    pnl, wins, losses = _journal_stats()
    base = float(total_override) if total_override is not None else pnl + float(unrealized or 0.0)
    total = base + extra_pnl
    if extra_pnl > 0.005:
        wins += 1
    elif extra_pnl < -0.005:
        losses += 1
    if wins_override is not None:
        wins = int(wins_override)
    if losses_override is not None:
        losses = int(losses_override)
    # PnL color
    if total > 0.005:
        pnl_emoji, sign = "✅", "+"
    elif total < -0.005:
        pnl_emoji, sign = "❌", "-"
    else:
        pnl_emoji, sign = "⚪", ""
    amt = _fmt_amount(abs(total))  # "$X.XX"
    pct_part = ""
    if return_pct is not None:
        pct_sign = "+" if return_pct >= 0 else "-"
        pct_part = _md_escape(f" ({pct_sign}{abs(return_pct):.1f}%)")
    pnl_part = f"{pnl_emoji} All\\-time: {_md_escape(sign + amt)}{pct_part}"

    # Win-rate color
    decided = wins + losses
    if decided == 0:
        wr_part = f"⚪ W/L \\-\\-"
    else:
        wr = wins / decided * 100.0
        if wr >= 60:
            wr_emoji = "🟢"
        elif wr >= 40:
            wr_emoji = "🟡"
        else:
            wr_emoji = "🔴"
        wr_part = f"{wr_emoji} W/L {wr:.0f}% \\({wins}W/{losses}L\\)"

    return f"{pnl_part}  •  {wr_part}"


def notify_trade_buy(
    *,
    market_title: str,
    token_id: str,
    price: float,
    size_usd: float,
    signal: dict[str, Any],
    outcome: str | None = None,
    market_url: str | None = None,
    strategy: str | None = None,
) -> None:
    if not is_enabled() or not _flag("TELEGRAM_ALERT_TRADES"):
        return
    # Optional granular suppression: TELEGRAM_ALERT_TRADES_BUY=0 hides BUY
    # alerts while keeping SELLs (which trigger via notify_trade_sell).
    if not _flag("TELEGRAM_ALERT_TRADES_BUY"):
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

    title = market_title or ""
    size_str = _md_escape(_fmt_amount(size_usd))
    price_str = _md_escape(f"{price:.2f}")
    outcome_str = _md_escape(outcome or "")
    footer_parts: list[str] = []
    if signal_part:
        footer_parts.append(signal_part)
    if market_url:
        footer_parts.append(f"[🔗]({market_url})")
    head = f"🛒 *BUY* 💵 {size_str} @ {price_str}"
    if strategy:
        head += f"  \\[`{_md_escape(strategy)}`\\]"
    lines = [head, _fmt_all_time_line()]
    if title and outcome_str:
        lines.append(f"🎯 _{_md_escape(title)}_ 👍 *{outcome_str}*")
    elif title:
        lines.append(f"🎯 _{_md_escape(title)}_")
    elif outcome_str:
        lines.append(f"👍 *{outcome_str}*")
    if footer_parts:
        lines.append(f"🏷️ {' • '.join(footer_parts)}")
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
    outcome: str | None = None,
    held_seconds: int | None = None,
    market_url: str | None = None,
    strategy: str | None = None,
) -> None:
    if not is_enabled() or not _flag("TELEGRAM_ALERT_TRADES"):
        return
    win_thresh = _float_env("TELEGRAM_BIG_WIN_USD", 10.0)
    loss_thresh = _float_env("TELEGRAM_BIG_LOSS_USD", 5.0)
    thresholds_on = _flag("TELEGRAM_ALERT_THRESHOLDS")
    is_big_win = False
    force_big_win = "big_win" in str(reason or "").lower()
    if (thresholds_on and realized_pnl_usd >= win_thresh) or (force_big_win and realized_pnl_usd >= 0):
        emoji, label = "💰", "BIG WINZZZZ"
        is_big_win = True
    elif thresholds_on and realized_pnl_usd <= -loss_thresh:
        emoji, label = "💸", "BIG LOSS"
    elif realized_pnl_usd > 0:
        emoji, label = "🟢", "SELL"
    elif realized_pnl_usd < 0:
        emoji, label = "🔴", "SELL"
    else:
        emoji, label = "⚪", "SELL"

    title = market_title or ""
    size_str = _md_escape(_fmt_amount(size_usd))
    price_str = _md_escape(f"{price:.2f}")
    sign = "+" if realized_pnl_usd >= 0 else "-"
    pnl_abs_str = _fmt_amount(abs(realized_pnl_usd))
    pnl_str = _md_escape(f"{sign}{pnl_abs_str}")
    pnl_emoji = "📉" if realized_pnl_usd < 0 else "📈"
    pct_str = ""
    if realized_pnl_pct is not None:
        sign_pct = "+" if realized_pnl_pct >= 0 else "-"
        pct_str = _md_escape(f"({sign_pct}{abs(realized_pnl_pct):.1f}%)")
    held_str = _fmt_held(held_seconds)
    held_md = _md_escape(held_str) if held_str else ""

    if is_big_win:
        banner = "🟢💰🟢💰🟢💰🟢💰"
        head_line = f"{banner} *{label}* {banner}"
        money_line_extras = " ".join(part for part in (pct_str, held_md) if part)
        money_line = f"💚🤑💵 *{pnl_str}* {money_line_extras} 💵🤑💚".replace("  ", " ")
        size_line = f"🟢 {size_str} @ {price_str} 🟢"
        action_line = "\n".join((head_line, money_line, size_line))
    else:
        head_extras: list[str] = [pnl_str]
        if pct_str:
            head_extras.append(pct_str)
        if held_md:
            head_extras.append(held_md)
        action_line = (
            f"{emoji} *{label}* 💵 {size_str} @ {price_str} "
            f"{pnl_emoji} {' '.join(head_extras)}"
        )

    outcome_str = _md_escape(outcome or "")
    tag_parts = [_md_escape(reason)]
    if strategy:
        tag_parts.append(f"`{_md_escape(strategy)}`")
    tag_line = f"🏷️ {' • '.join(tag_parts)}"
    if market_url:
        tag_line += f" • [🔗]({market_url})"
    # BIG WIN / BIG LOSS messages omit the all-time line: they're meant
    # as celebratory/lamentation banners, and the all-time line can carry
    # a contradicting color (🔴 W/L 0%) that breaks the visual mood.
    is_big = is_big_win or (thresholds_on and realized_pnl_usd <= -loss_thresh)
    lines = [action_line] if is_big else [action_line, _fmt_all_time_line(extra_pnl=realized_pnl_usd)]
    if title and outcome_str:
        lines.append(f"🎯 _{_md_escape(title)}_ 👍 *{outcome_str}*")
    elif title:
        lines.append(f"🎯 _{_md_escape(title)}_")
    elif outcome_str:
        lines.append(f"👍 *{outcome_str}*")
    lines.append(tag_line)
    _post("\n".join(lines))


def notify_error(category: str, message: str, *, dedupe_key: str | None = None) -> None:
    if not is_enabled() or not _flag("TELEGRAM_ALERT_ERRORS"):
        return
    path = _default_state_path()
    state = _load_state(path)
    now = time.time()
    window = _dedupe_window_sec()

    suffix = ""
    if dedupe_key:
        entry = state.dedupe_seen.get(dedupe_key)
        if entry is not None:
            last_ts = float(entry.get("last_ts", 0))
            if (now - last_ts) < window:
                entry["last_ts"] = now
                entry["count"] = int(entry.get("count", 1)) + 1
                entry["last_message"] = message
                state.dedupe_seen[dedupe_key] = entry
                _save_state(path, state)
                return
            prev_count = int(entry.get("count", 1))
            if prev_count > 1:
                if window >= 60:
                    suffix = f" \\(×{prev_count} in {int(window // 60)}min\\)"
                else:
                    suffix = f" \\(×{prev_count} in {int(window)}s\\)"
        state.dedupe_seen[dedupe_key] = {
            "first_ts": now,
            "last_ts": now,
            "count": 1,
            "last_message": message,
        }

    text = f"❌ *{_md_escape(category)}* · {_md_escape(message)}{suffix}"
    _post(text)
    _prune_dedupe(state, now, window)
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
    # Threshold alerts are live-only — DD / equity-floor pings from 30
    # dry bots flood the channel with non-actionable noise.
    if _is_dry_run():
        return
    if kind == "drawdown":
        _handle_drawdown(payload)
    elif kind == "equity_floor":
        _handle_equity_floor(payload)
    elif kind == "auto_tune_change":
        _handle_auto_tune_change(payload)
    # big_win / big_loss : intégrés à notify_trade_sell, ignorés ici


_HEARTBEAT_TITLE_MAX = 38


def _heartbeat_top_line(emoji: str, entry: dict[str, Any]) -> str | None:
    """Construit la ligne `🏆 +X$ _titre_` pour top winner/loser. None si pas d'entrée."""
    if not isinstance(entry, dict) or not entry:
        return None
    pnl = float(entry.get("pnl_usd", 0) or 0)
    amount = _md_escape(_fmt_signed_money_fr(pnl))
    title_raw = str(entry.get("title") or "").strip()
    title_short = _truncate(title_raw, _HEARTBEAT_TITLE_MAX)
    if title_short:
        return f"{emoji} *{amount}* _{_md_escape(title_short)}_"
    return f"{emoji} *{amount}*"


def notify_heartbeat(snapshot: dict[str, Any]) -> None:
    if not is_enabled() or not _flag("TELEGRAM_ALERT_HEARTBEAT"):
        return
    # Bilan heartbeat is live-only — 30 dry-run bots would firehose the
    # channel with stale-balance updates that have no signal value.
    if _is_dry_run():
        return
    try:
        interval_min = float(os.environ.get("TELEGRAM_HEARTBEAT_MINUTES", "30"))
    except ValueError:
        interval_min = 30.0
    if interval_min <= 0:
        interval_min = 30.0
    path = _default_state_path()
    state = _load_state(path)
    now = time.time()
    if state.last_heartbeat_ts is not None and (now - state.last_heartbeat_ts) < interval_min * 60:
        return

    equity = float(snapshot.get("equity_usd", 0) or 0)
    cash = float(snapshot.get("cash_usd", 0) or 0)
    unrealized = float(snapshot.get("unrealized_pnl_usd", 0) or 0)
    positions = int(snapshot.get("open_positions", 0) or 0)
    trades = int(snapshot.get("trades_24h", 0) or 0)
    wins = int(snapshot.get("wins_24h", 0) or 0)
    losses = int(snapshot.get("losses_24h", 0) or 0)
    realized_24h = float(snapshot.get("realized_pnl_24h_usd", 0) or 0)
    decided = wins + losses
    win_rate = (wins / decided * 100.0) if decided > 0 else 0.0

    cash_pct = (cash / equity * 100.0) if equity > 0 else 0.0
    stamp = time.strftime("%Hh%M", time.localtime(now))

    lines: list[str] = [f"💓 *Bilan* · {_md_escape(stamp)}", ""]

    equity_str = _md_escape(_fmt_money_fr(equity))
    cash_str = _md_escape(_fmt_money_fr(cash))
    cash_pct_str = _md_escape(f"{cash_pct:.0f}%")
    lines.append(
        f"💰 Equity *{equity_str}* — cash {cash_str} \\({cash_pct_str}\\)"
    )

    if positions > 0:
        unreal_str = _md_escape(_fmt_signed_money_fr(unrealized))
        label = "position" if positions == 1 else "positions"
        lines.append(f"📦 {positions} {label} — non\\-réalisé *{unreal_str}*")
    else:
        lines.append("📦 aucune position ouverte")

    if trades > 0:
        realized_str = _md_escape(_fmt_signed_money_fr(realized_24h))
        wl_str = _md_escape(f"{wins}W/{losses}L")
        rate_str = _md_escape(f"({win_rate:.0f}%)")
        lines.append(f"📊 24h: réalisé *{realized_str}* — {wl_str} {rate_str}")
    else:
        lines.append("📊 24h: aucun trade clôturé")

    lines.append(
        _fmt_all_time_line(
            unrealized=unrealized,
            total_override=(
                float(snapshot["all_time_pnl_usd"])
                if snapshot.get("all_time_pnl_usd") is not None
                else None
            ),
            return_pct=(
                float(snapshot["all_time_return_pct"])
                if snapshot.get("all_time_return_pct") is not None
                else None
            ),
            wins_override=(
                int(snapshot["all_time_wins"])
                if snapshot.get("all_time_wins") is not None
                else None
            ),
            losses_override=(
                int(snapshot["all_time_losses"])
                if snapshot.get("all_time_losses") is not None
                else None
            ),
        )
    )

    top_w_line = _heartbeat_top_line("🏆", snapshot.get("top_winner") or {})
    top_l_line = _heartbeat_top_line("💸", snapshot.get("top_loser") or {})
    if top_w_line or top_l_line:
        lines.append("")
        if top_w_line:
            lines.append(top_w_line)
        if top_l_line:
            lines.append(top_l_line)

    if _post("\n".join(lines)):
        state.last_heartbeat_ts = now
        _save_state(path, state)


# notify_daily_summary removed: the autonomous analyst sidecar
# (scripts/dry_analyst.py) now handles cross-strategy summaries, and
# the existing notify_heartbeat covers per-strategy daily context.
