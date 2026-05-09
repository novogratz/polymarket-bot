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


def _format_action_line(action: dict) -> str:
    """Format a single tick action into one indented line.

    `action` is a dict with a `kind` key in {buy, sell, noise, btc} and the
    payload-specific fields populated by `format_tick_footer` from the raw
    tick payload.
    """
    kind = action.get("kind")
    if kind == "buy":
        return _format_buy_or_noise(action, verb="BUY", color=cyan)
    if kind == "noise":
        return _format_buy_or_noise(action, verb="NOISE", color=cyan)
    if kind == "sell":
        return _format_sell(action)
    if kind == "btc":
        return _format_btc(action)
    return f"  → {kind or '?'}"


def _format_buy_or_noise(action: dict, *, verb: str, color) -> str:
    signal = action.get("signal") or {}
    outcome = str(signal.get("outcome") or "?")
    question = _truncate_question(signal.get("question"))
    order = action.get("order") or {}
    size_usdc = order.get("size_usdc")
    price = signal.get("best_ask")
    size_str = f"${float(size_usdc):.2f}" if isinstance(size_usdc, (int, float)) else "$?"
    price_str = f"{float(price):.2f}" if isinstance(price, (int, float)) else "?"
    context_bits = []
    strategy = action.get("strategy")
    if strategy:
        context_bits.append(str(strategy))
    consensus = signal.get("consensus")
    copied = signal.get("copied_usdc")
    if consensus and copied:
        context_bits.append(f"{int(consensus)} wallets / ${float(copied) / 1000:.1f}k copied")
    elif consensus:
        context_bits.append(f"{int(consensus)} wallets")
    context = f" ({', '.join(context_bits)})" if context_bits else ""
    return f"  → {color(verb)} {outcome}  {question}  {size_str} @ {price_str}{context}"


def _format_sell(action: dict) -> str:
    outcome = str(action.get("outcome") or "?")
    question = _truncate_question(action.get("question"))
    order = action.get("order") or {}
    size_usdc = order.get("size_usdc")
    price = order.get("price")
    size_str = f"${float(size_usdc):.2f}" if isinstance(size_usdc, (int, float)) else "$?"
    price_str = f"{float(price):.2f}" if isinstance(price, (int, float)) else "?"
    reason = str(action.get("reason") or "?")
    pnl_pct = action.get("pnl_pct")
    pnl_str = f", {colorize_pct(float(pnl_pct))}" if isinstance(pnl_pct, (int, float)) else ""
    return f"  → {yellow('SELL')} {outcome}  {question}  {size_str} @ {price_str}  ({reason}{pnl_str})"


def _format_btc(action: dict) -> str:
    side = str(action.get("side") or "?")
    strike = action.get("strike")
    strike_str = f"{int(strike)}" if isinstance(strike, (int, float)) else "?"
    size_usdc = action.get("size_usdc")
    size_str = f"${float(size_usdc):.2f}" if isinstance(size_usdc, (int, float)) else "$?"
    edge_pct = action.get("edge_pct")
    edge_str = f"edge {float(edge_pct) * 100:.1f}%" if isinstance(edge_pct, (int, float)) else "edge ?"
    return f"  → {bold('BTC')} {side} {strike_str}  {size_str}  ({edge_str})"


def _format_error_line(payload: dict) -> str:
    """One-line error summary for a failed tick (used in quiet mode footer)."""
    tick = payload.get("tick", "?")
    time_str = _format_time_hhmm(payload.get("started_at"))
    err = payload.get("error") or {}
    err_type = err.get("type") or "?"
    err_msg = err.get("message") or "(no message)"
    return f"{red(CROSS)} #{tick} {time_str} error: {err_type}: {err_msg}"


def format_tick_footer(payload: dict, settings) -> str:
    """Build the human-readable footer for one tick (used in POLYMARKET_QUIET).

    `payload` is the dict written by `strategy_loop` around `smart_money_once`:
    {tick, strategy, started_at, result | error}. `settings` is the bot
    Settings object (currently unused; reserved for future toggles).
    """
    if "error" in payload:
        return _format_error_line(payload)

    lines = [_format_summary_line(payload)]
    actions = _collect_actions(payload.get("result") or {})
    max_actions = 6
    visible = actions[:max_actions]
    hidden = max(0, len(actions) - max_actions)
    for action in visible:
        lines.append(_format_action_line(action))
    if hidden:
        lines.append(f"  … +{hidden} more action(s)")
    return "\n".join(lines)


def _collect_actions(result: dict) -> list[dict]:
    actions: list[dict] = []
    trade = result.get("trade")
    if isinstance(trade, dict) and trade.get("strategy") and result.get("strategy") != "noise_fallback":
        actions.append({"kind": "buy", **trade})
    for noise in result.get("noise_trades") or []:
        if isinstance(noise, dict):
            actions.append({"kind": "noise", **noise})
    for exit_record in result.get("exits") or []:
        if isinstance(exit_record, dict) and exit_record.get("action") == "sell":
            actions.append({"kind": "sell", **exit_record})
    btc = result.get("btc_edge") or {}
    for btc_trade in btc.get("trades") or []:
        if isinstance(btc_trade, dict):
            actions.append({"kind": "btc", **btc_trade})
    return actions
