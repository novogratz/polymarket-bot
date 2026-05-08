"""Petits helpers de présentation pour les commandes CLI.

Couleurs ANSI auto-désactivées si stdout n'est pas un TTY ou si la variable
d'environnement ``NO_COLOR`` est positionnée (https://no-color.org). Aucune
dépendance externe : juste des codes ANSI standards.
"""

from __future__ import annotations

import os
import sys


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
