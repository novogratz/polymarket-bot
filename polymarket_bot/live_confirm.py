"""Interactive confirmation gate for live trading.

This module is the single source of truth for the "yes/no" guard that
fires before any live trading loop. The prompt accepts only ``yes``
(case-insensitive, surrounding whitespace stripped). Any other input,
EOF, KeyboardInterrupt, or non-TTY stdin returns False.

Tests must not import any other side-effectful module while testing
the prompt (no network, no SDK).
"""

from __future__ import annotations

import os
import json
import sys
from typing import TextIO
from datetime import datetime, timezone
from pathlib import Path

from polymarket_bot.config import Settings


def prompt_live_confirmation(
    *,
    recap_text: str,
    skip: bool,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> bool:
    """Return True if the user explicitly confirms a live launch.

    Behaviour:
    - ``skip=True`` -> return True immediately, do not touch stdin/stdout.
    - stdin not a TTY -> return False (refuse to launch in the blind,
      even if "yes" was piped in).
    - Otherwise print ``recap_text`` then read a single line. Accept only
      "yes" (case-insensitive, surrounding whitespace stripped). Any
      other input, EOF, or KeyboardInterrupt -> False.

    The timeout dimension described in the spec is intentionally not
    implemented here (would require ``select`` or threading). The
    operator must respond or Ctrl-C. A later plan can add a timeout.

    Non-TTY safety: if stdin is not a TTY we refuse rather than block
    on readline. This covers ``nohup``, detached tmux without ``-d``,
    cron, and CI runners — situations where a blind ``readline`` would
    suspend the process indefinitely.
    """
    if skip:
        return True

    stream_in = stdin if stdin is not None else sys.stdin
    stream_out = stdout if stdout is not None else sys.stderr

    if not stream_in.isatty():
        stream_out.write(
            "Live trading requires interactive confirmation on a TTY. "
            "Re-run with --yes for non-TTY automation (scripts), or "
            "attach to a terminal before launching.\n"
        )
        return False

    stream_out.write(recap_text)
    if not recap_text.endswith("\n"):
        stream_out.write("\n")
    stream_out.write('Tape "yes" pour lancer (toute autre saisie annule) : ')
    stream_out.flush()

    try:
        raw = stream_in.readline()
    except KeyboardInterrupt:
        stream_out.write("\nAnnulé.\n")
        return False

    if not raw:
        stream_out.write("\nAucune saisie reçue, annulation.\n")
        return False

    answer = raw.strip().lower()
    if answer == "yes":
        return True

    stream_out.write("Annulé.\n")
    return False


def _redact(value: str | None, *, prefix: int = 6, suffix: int = 4) -> str:
    if not value:
        return "(not configured)"
    if len(value) <= prefix + suffix + 3:
        return value
    return f"{value[:prefix]}...{value[-suffix:]}"


def _env_or(name: str, fallback):
    """Return the current ``os.environ[name]`` if set, else ``fallback``.

    Settings field defaults are evaluated at class-definition time
    (see ``polymarket_bot.config``), so a Settings instance created
    after an env mutation won't pick up the new value. The recap is
    user-facing and must reflect what the bot will actually use on the
    next process start — re-reading ``os.environ`` keeps the banner
    truthful even when the operator just exported a new value.
    """
    value = os.getenv(name)
    if value is None or value == "":
        return fallback
    return value


def _parse_dt(raw) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _live_risk_lines(settings: Settings, profile_label: str) -> list[str]:
    lines: list[str] = []
    active_path = Path("data/live_active_profile.json")
    if active_path.is_file():
        try:
            active = json.loads(active_path.read_text(encoding="utf-8"))
            active_profile = str(active.get("profile") or "")
        except Exception:
            active_profile = ""
        if active_profile and active_profile != profile_label.replace(".toml", ""):
            lines.append(f"    hot_swap_profile     = {active_profile}")
    state_path = Path(_env_or("POLYMARKET_STATE_PATH", str(settings.state_path)))
    if not state_path.is_file():
        lines.append("    open_exposure        = no ledger yet")
        return lines
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        lines.append("    open_exposure        = ledger unreadable")
        return lines
    positions = state.get("positions") or []
    open_positions = [p for p in positions if p.get("status") == "open"]
    exposure = 0.0
    by_category: dict[str, float] = {}
    near_expiry_losers = 0
    now = datetime.now(timezone.utc)
    for position in open_positions:
        try:
            stake = float(position.get("stake") or position.get("cost_basis") or 0.0)
        except (TypeError, ValueError):
            stake = 0.0
        exposure += stake
        category = str(position.get("category") or "OTHER")
        by_category[category] = by_category.get(category, 0.0) + stake
        try:
            entry = float(position.get("entry_price") or 0.0)
            current = float(position.get("current_price") or entry)
        except (TypeError, ValueError):
            entry = current = 0.0
        end_at = _parse_dt(position.get("end_date") or position.get("endDate"))
        if end_at is not None and (end_at - now).total_seconds() <= 45 * 60 and current < entry:
            near_expiry_losers += 1
    leader = max(by_category.items(), key=lambda item: item[1])[0] if by_category else "none"
    lines.append(f"    open_exposure        = ${exposure:.2f} across {len(open_positions)} position(s)")
    lines.append(f"    largest_category     = {leader}")
    lines.append(f"    near_expiry_losers   = {near_expiry_losers}")
    return lines


def build_live_recap(settings: Settings, *, profile_label: str) -> str:
    """Return the human-readable banner shown before the yes/no prompt."""
    bar = "═" * 62
    sep = "─" * 62

    funder_raw = _env_or("POLYMARKET_FUNDER_ADDRESS", settings.funder_address)
    position_pct = _env_or("POLYMARKET_SMART_POSITION_PCT", settings.smart_position_pct)
    min_consensus = _env_or("POLYMARKET_SMART_MIN_CONSENSUS", settings.smart_min_consensus)
    min_copied = float(_env_or("POLYMARKET_SMART_MIN_COPIED_USDC", settings.smart_min_copied_usdc))
    max_chase = _env_or("POLYMARKET_SMART_MAX_CHASE_PREMIUM", settings.smart_max_chase_premium)
    max_ceiling = float(_env_or("POLYMARKET_SMART_MAX_POSITION_CEILING_USD", settings.smart_max_position_ceiling_usd))
    min_open = _env_or("POLYMARKET_MIN_OPEN_POSITIONS", settings.min_open_positions)
    stop_loss_pct = float(_env_or("POLYMARKET_SMART_STOP_LOSS_PCT", settings.smart_stop_loss_pct))
    stop_loss_min_age = _env_or("POLYMARKET_SMART_STOP_LOSS_MIN_AGE_MINUTES", settings.smart_stop_loss_min_age_minutes)
    trail_arm = float(_env_or("POLYMARKET_SMART_TRAILING_STOP_ARM_PCT", settings.smart_trailing_stop_arm_pct))
    trail_give = float(_env_or("POLYMARKET_SMART_TRAILING_STOP_GIVEBACK_PCT", settings.smart_trailing_stop_giveback_pct))
    tick_interval = _env_or("POLYMARKET_AUTO_INTERVAL_SECONDS", settings.auto_interval_seconds)
    state_path = _env_or("POLYMARKET_STATE_PATH", str(settings.state_path))

    lines = [
        bar,
        "  ⚠️  LIVE TRADING — ordres réels sur Polymarket",
        bar,
        "",
        f"  Profile:       {profile_label}",
        f"  Ledger:        {state_path}",
        f"  Wallet funder: {_redact(funder_raw)}",
        f"  Tick interval: {tick_interval}s",
        "",
        "  Sizing config:",
        f"    position_pct          = {position_pct}",
        f"    max_position_ceiling  = ${max_ceiling:g}",
        f"    min_open_positions    = {min_open}",
        "",
        "  Strategy filters:",
        f"    min_consensus         = {min_consensus}",
        f"    min_copied_usdc       = ${min_copied:g}",
        f"    max_chase_premium     = {max_chase}",
        "",
        "  Live risk snapshot:",
        *_live_risk_lines(settings, profile_label),
        "",
        f"  stop_loss             = -{int(stop_loss_pct * 100)}% "
        f"after {stop_loss_min_age}min",
        f"  trailing_stop arm/give = {int(trail_arm * 100)}% / "
        f"{int(trail_give * 100)}%",
        "",
        sep,
    ]
    return "\n".join(lines)
