"""Profile loader: parse TOML, validate, expose typed config + env mapping.

Profiles are TOML files in ``configs/profiles/`` that group strategy
parameters under thematic sections. This module is responsible for
reading one, validating its structure against the supported schema, and
producing a :class:`ProfileConfig`. Other modules consume the resulting
``values`` dict to drive ``os.environ`` updates — this module does not
write to the environment itself.

Adding a new tunable: register it in ``_SCHEMA`` below with the matching
environment variable name. The schema is the single source of truth.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ProfileValidationError(Exception):
    """Raised when a profile file is missing, malformed, or has unknown keys."""


# Schema:  section -> { toml_key: (env_var, value_type) }
# value_type can be "float", "int", "bool", "str". It guards the TOML
# value's actual Python type and dictates how the value is stringified
# when written back to ``os.environ``.
_SCHEMA: dict[str, dict[str, tuple[str, str]]] = {
    "run": {
        "starting_cash": ("POLYMARKET_PAPER_BALANCE_USD", "float"),
    },
    "sizing": {
        "position_pct": ("POLYMARKET_SMART_POSITION_PCT", "float"),
        "max_position_ceiling_usd": ("POLYMARKET_SMART_MAX_POSITION_CEILING_USD", "float"),
        "max_position_ceiling_pct": ("POLYMARKET_SMART_MAX_POSITION_CEILING_PCT", "float"),
        "cash_floor_pct": ("POLYMARKET_SMART_CASH_FLOOR_PCT", "float"),
        "min_open_positions": ("POLYMARKET_MIN_OPEN_POSITIONS", "int"),
        "starter_trade_usd": ("POLYMARKET_STARTER_TRADE_USD", "float"),
        "assumed_live_balance_usd": ("POLYMARKET_ASSUME_LIVE_BALANCE_USD", "float"),
    },
    "trader_cohort": {
        "leaderboard_window": ("POLYMARKET_SMART_TIME_PERIOD", "str"),
        "top_n": ("POLYMARKET_SMART_LEADERBOARD_LIMIT", "int"),
        "min_trader_pnl": ("POLYMARKET_SMART_MIN_TRADER_PNL", "float"),
        "min_trader_volume": ("POLYMARKET_SMART_MIN_TRADER_VOLUME", "float"),
        "min_trader_roi": ("POLYMARKET_SMART_MIN_TRADER_ROI", "float"),
        "trade_fetch_concurrency": ("POLYMARKET_SMART_TRADE_FETCH_CONCURRENCY", "int"),
        "trade_lookback_minutes": ("POLYMARKET_SMART_TRADE_LOOKBACK_MINUTES", "int"),
    },
    "filters": {
        "min_consensus": ("POLYMARKET_SMART_MIN_CONSENSUS", "int"),
        "min_copied_usdc": ("POLYMARKET_SMART_MIN_COPIED_USDC", "float"),
        "max_chase_premium": ("POLYMARKET_SMART_MAX_CHASE_PREMIUM", "float"),
        "price_min": ("POLYMARKET_SMART_MIN_BUY_PRICE", "float"),
        "price_max": ("POLYMARKET_SMART_MAX_BUY_PRICE", "float"),
        "max_absolute_spread": ("POLYMARKET_SMART_MAX_SPREAD", "float"),
        "max_relative_spread": ("POLYMARKET_SMART_MAX_RELATIVE_SPREAD", "float"),
        "signal_staleness_seconds": ("POLYMARKET_SMART_MAX_SIGNAL_AGE_MINUTES", "int"),
    },
    "exits": {
        "take_profit_ladder": ("POLYMARKET_SMART_TAKE_PROFIT_TIERS", "str"),
        "trailing_stop_arm_pct": ("POLYMARKET_SMART_TRAILING_STOP_ARM_PCT", "float"),
        "trailing_stop_giveback": ("POLYMARKET_SMART_TRAILING_STOP_GIVEBACK_PCT", "float"),
        "peak_protect_arm_pct": ("POLYMARKET_SMART_PEAK_PROTECT_TRIGGER", "float"),
        "peak_protect_exit_pct": ("POLYMARKET_SMART_PEAK_PROTECT_FLOOR", "float"),
        "stop_loss_pct": ("POLYMARKET_SMART_STOP_LOSS_PCT", "float"),
        "stop_loss_min_age_minutes": ("POLYMARKET_SMART_STOP_LOSS_MIN_AGE_MINUTES", "int"),
        "max_hold_hours": ("POLYMARKET_SMART_MAX_HOLD_HOURS", "float"),
    },
    "btc_edge": {
        "enabled": ("POLYMARKET_BTC_EDGE_INTEGRATED", "bool"),
        "per_trade_cap_usd": ("POLYMARKET_BTC_MAX_TRADE_USD", "float"),
        "min_edge_over_market": ("POLYMARKET_BTC_MIN_EDGE", "float"),
    },
    "noise_fallback": {
        "enabled": ("POLYMARKET_SMART_NOISE_FALLBACK_ENABLED", "bool"),
        "max_trades_per_tick": ("POLYMARKET_SMART_NOISE_FALLBACK_MAX_TRADES_PER_TICK", "int"),
        "stake_usd": ("POLYMARKET_SMART_NOISE_FALLBACK_MAX_TRADE_USD", "float"),
        "cash_pressure_threshold": ("POLYMARKET_SMART_NOISE_FALLBACK_CASH_PRESSURE_PCT", "float"),
    },
    "auto_tune": {
        "enabled": ("POLYMARKET_SMART_AUTO_TUNE_ENABLED", "bool"),
        "min_closed_trades": ("POLYMARKET_SMART_AUTO_TUNE_MIN_TRADES", "int"),
    },
    "persistence": {
        "enabled": ("POLYMARKET_PERSISTENCE_ENABLED", "bool"),
        "window_days": ("POLYMARKET_PERSISTENCE_WINDOW_DAYS", "int"),
        "cache_threshold": ("POLYMARKET_PERSISTENCE_CACHE_THRESHOLD", "float"),
        "intersect_periods": ("POLYMARKET_PERSISTENCE_INTERSECT_PERIODS", "str"),
        "intersect_min": ("POLYMARKET_PERSISTENCE_INTERSECT_MIN", "int"),
    },
    "telemetry": {
        "quiet": ("POLYMARKET_QUIET", "bool"),
        "auto_interval_seconds": ("POLYMARKET_AUTO_INTERVAL_SECONDS", "int"),
    },
}


DEFAULT_STARTING_CASH = 100.0


@dataclass(frozen=True)
class ProfileConfig:
    """Result of parsing a TOML profile.

    Attributes:
        source_path: file the profile was read from
        starting_cash: starting cash for dry-run runs (default 100.0)
        values: mapping ``<env_var>`` -> stringified value, ready to feed
            ``os.environ``. Keys include the ``POLYMARKET_`` prefix.
    """

    source_path: Path
    starting_cash: float = DEFAULT_STARTING_CASH
    values: dict[str, str] = field(default_factory=dict)


def _coerce(value: Any, expected: str, location: str) -> str:
    """Validate the TOML value's Python type and return its string form."""
    if expected == "bool":
        if not isinstance(value, bool):
            raise ProfileValidationError(
                f"{location}: expected bool, got {type(value).__name__}"
            )
        return "1" if value else "0"
    if expected == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProfileValidationError(
                f"{location}: expected int, got {type(value).__name__}"
            )
        return str(value)
    if expected == "float":
        if isinstance(value, bool):
            raise ProfileValidationError(
                f"{location}: expected float, got bool"
            )
        if not isinstance(value, (int, float)):
            raise ProfileValidationError(
                f"{location}: expected float, got {type(value).__name__}"
            )
        return str(float(value))
    if expected == "str":
        if not isinstance(value, str):
            raise ProfileValidationError(
                f"{location}: expected str, got {type(value).__name__}"
            )
        return value
    raise ProfileValidationError(f"{location}: unknown expected type {expected!r}")


def load_profile(path: Path) -> ProfileConfig:
    """Read ``path``, validate against the schema, return a :class:`ProfileConfig`.

    Raises :class:`ProfileValidationError` on any failure: file missing,
    invalid TOML, unknown section, unknown key, or value with the wrong
    Python type.
    """
    if not path.is_file():
        raise ProfileValidationError(f"profile not found: {path}")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ProfileValidationError(f"{path}: invalid TOML — {exc}") from exc

    values: dict[str, str] = {}
    starting_cash = DEFAULT_STARTING_CASH

    for section, body in raw.items():
        if section not in _SCHEMA:
            raise ProfileValidationError(
                f"{path}: unknown section [{section}]. "
                f"Supported: {sorted(_SCHEMA)}"
            )
        if not isinstance(body, dict):
            raise ProfileValidationError(
                f"{path}: section [{section}] must be a table"
            )
        section_schema = _SCHEMA[section]
        for key, value in body.items():
            if key not in section_schema:
                raise ProfileValidationError(
                    f"{path}: unknown key [{section}].{key}. "
                    f"Supported in [{section}]: {sorted(section_schema)}"
                )
            env_var, expected = section_schema[key]
            location = f"{path}:[{section}].{key}"
            stringified = _coerce(value, expected, location)
            values[env_var] = stringified
            if section == "run" and key == "starting_cash":
                starting_cash = float(value)

    return ProfileConfig(source_path=path, starting_cash=starting_cash, values=values)


def apply_profile_to_env(profile: ProfileConfig, *, override: bool = False) -> None:
    """Push profile values into ``os.environ``.

    By default, only sets a variable if it is missing or empty in
    ``os.environ`` — this preserves explicit CLI env overrides
    (``POLYMARKET_SMART_POSITION_PCT=0.25 pmbot auto-loop ...``).

    Pass ``override=True`` to force-overwrite (used by ``--reset`` flows
    in later plans).
    """
    for key, value in profile.values.items():
        if override or not os.environ.get(key):
            os.environ[key] = value


def snapshot_effective_env() -> dict[str, str]:
    """Return all ``POLYMARKET_*`` env vars currently set."""
    return {k: v for k, v in os.environ.items() if k.startswith("POLYMARKET_")}


def _reverse_schema() -> dict[str, tuple[str, str, str]]:
    """env_var -> (section, toml_key, value_type)."""
    reverse: dict[str, tuple[str, str, str]] = {}
    for section, body in _SCHEMA.items():
        for toml_key, (env_var, value_type) in body.items():
            reverse[env_var] = (section, toml_key, value_type)
    return reverse


def _format_toml_value(raw: str, expected: str) -> str:
    """Format a stringified env value back into TOML literal syntax."""
    if expected == "bool":
        return "true" if raw in ("1", "true", "True", "yes") else "false"
    if expected == "int":
        return str(int(float(raw)))
    if expected == "float":
        return str(float(raw))
    if expected == "str":
        escaped = raw.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return f'"{raw}"'


def write_snapshot_toml(path: Path, *, source_label: str) -> None:
    """Write a TOML snapshot of the current POLYMARKET_* environment.

    Grouped by schema section. Unknown keys (not in ``_SCHEMA``) are
    dumped into an ``[extras]`` section as raw strings so the snapshot
    remains lossless without breaking ``load_profile``.

    The output is a valid input to :func:`load_profile` provided no
    ``[extras]`` section is present (extras are for audit, not replay).
    """
    reverse = _reverse_schema()
    current = snapshot_effective_env()

    grouped: dict[str, list[tuple[str, str]]] = {}
    extras: list[tuple[str, str]] = []

    for env_var, value in sorted(current.items()):
        if env_var == "POLYMARKET_SKIP_DOTENV":
            continue
        match = reverse.get(env_var)
        if match is None:
            extras.append((env_var, value))
            continue
        section, toml_key, value_type = match
        grouped.setdefault(section, []).append(
            (toml_key, _format_toml_value(value, value_type))
        )

    lines: list[str] = [
        f"# source: {source_label}",
        "# Auto-generated snapshot — do not edit by hand.",
        "",
    ]
    for section in _SCHEMA.keys():
        rows = grouped.get(section)
        if not rows:
            continue
        lines.append(f"[{section}]")
        for toml_key, formatted in rows:
            lines.append(f"{toml_key} = {formatted}")
        lines.append("")
    if extras:
        lines.append("[extras]")
        lines.append("# These env vars are not part of the profile schema; preserved for audit.")
        for env_var, value in extras:
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{env_var} = "{escaped}"')
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
