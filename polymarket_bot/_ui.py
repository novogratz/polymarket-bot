"""Petits helpers de présentation pour les commandes CLI.

Couleurs ANSI auto-désactivées si stdout n'est pas un TTY ou si la variable
d'environnement ``NO_COLOR`` est positionnée (https://no-color.org). Aucune
dépendance externe : juste des codes ANSI standards.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime


def _color_enabled() -> bool:
    if os.getenv("NO_COLOR"):
        return False
    if os.getenv("POLYMARKET_FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


def _wrap(code: str, text: str) -> str:
    if not _color_enabled():
        return text
    return f"\033[{code}m{text}\033[0m"


def green(text: str) -> str:
    return _wrap("32", text)


def red(text: str) -> str:
    return _wrap("31", text)


def yellow(text: str) -> str:
    return _wrap("33", text)


def cyan(text: str) -> str:
    return _wrap("36", text)


def dim(text: str) -> str:
    return _wrap("2", text)


def bold(text: str) -> str:
    return _wrap("1", text)


CHECK = "✓"
CROSS = "✗"
WARN = "⚠"
DASH = "—"


def ok(label: str = "") -> str:
    return green(CHECK) + (f" {label}" if label else "")


def ko(label: str = "") -> str:
    return red(CROSS) + (f" {label}" if label else "")


def warn(label: str = "") -> str:
    return yellow(WARN) + (f" {label}" if label else "")


def skip(label: str = "") -> str:
    return dim(DASH) + (f" {label}" if label else "")


def colorize_pnl(value: float) -> str:
    text = f"{value:+.2f}"
    if value > 0:
        return green(text)
    if value < 0:
        return red(text)
    return text


def colorize_pct(value: float) -> str:
    text = f"{value:+.1%}"
    if value > 0:
        return green(text)
    if value < 0:
        return red(text)
    return text


def _truncate_question(text: str | None, max_len: int = 40) -> str:
    """Truncate a market question for one-line display, ending with … if cut."""
    if not text:
        return "—"
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _format_summary_line(payload: dict) -> str:
    """One-line summary of a tick payload (used in quiet mode footer)."""
    tick = payload.get("tick", "?")
    time_str = _format_time_hhmm(payload.get("started_at"))
    result = payload.get("result") or {}
    scan_report = result.get("scan_report") or {}
    opps = scan_report.get("opportunities") or []
    scan_part = f"scan: {len(opps)} opps"

    summary = result.get("summary")
    if isinstance(summary, dict):
        cash = float(summary.get("cash") or 0.0)
        equity = float(summary.get("equity") or 0.0)
        pnl = float(summary.get("unrealized_pnl") or 0.0)
        positions = int(summary.get("open_positions") or 0)
        snapshot = (
            f"cash ${cash:.2f} / equity ${equity:.2f} "
            f"/ {colorize_pnl(pnl)} / {positions} pos"
        )
    else:
        status = result.get("status")
        snapshot = f"({status})" if status else "(no summary)"

    return f"{green(CHECK)} #{tick} {time_str} | {scan_part} | {snapshot}"


def _format_time_hhmm(iso_str: str | None) -> str:
    """Extract HH:MM from an ISO 8601 timestamp; return ??:?? on parse failure.

    No timezone conversion: the substring is read as-is. The bot writes
    `started_at` via `utc_now().isoformat()`, so this prints UTC time, which
    is the consistent convention across all bot logs.
    """
    if not iso_str:
        return "??:??"
    try:
        # Accept "...Z" suffix as +00:00.
        normalized = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except (TypeError, ValueError):
        return "??:??"
    return dt.strftime("%H:%M")
