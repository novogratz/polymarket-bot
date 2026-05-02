from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return float(value)


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


@dataclass(frozen=True)
class Settings:
    gamma_base_url: str = os.getenv("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com")
    state_path: Path = Path(os.getenv("POLYMARKET_STATE_PATH", "data/paper_state.json"))
    scan_limit: int = _int_env("POLYMARKET_SCAN_LIMIT", 200)
    soon_hours: int = _int_env("POLYMARKET_SOON_HOURS", 72)
    paper_balance_usd: float = _float_env("POLYMARKET_PAPER_BALANCE_USD", 20.0)
    max_position_usd: float = _float_env("POLYMARKET_MAX_POSITION_USD", 5.0)
    min_liquidity_usd: float = _float_env("POLYMARKET_MIN_LIQUIDITY_USD", 500.0)
    min_volume_usd: float = _float_env("POLYMARKET_MIN_VOLUME_USD", 1000.0)
    dashboard_host: str = os.getenv("POLYMARKET_DASHBOARD_HOST", "127.0.0.1")
    dashboard_port: int = _int_env("POLYMARKET_DASHBOARD_PORT", 8765)
