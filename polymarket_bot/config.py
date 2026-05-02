from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parent.parent / ".env")


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
    clob_base_url: str = os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com")
    state_path: Path = Path(os.getenv("POLYMARKET_STATE_PATH", "data/paper_state.json"))
    scan_limit: int = _int_env("POLYMARKET_SCAN_LIMIT", 200)
    soon_hours: int = _int_env("POLYMARKET_SOON_HOURS", 72)
    paper_balance_usd: float = _float_env("POLYMARKET_PAPER_BALANCE_USD", 20.0)
    max_position_usd: float = _float_env("POLYMARKET_MAX_POSITION_USD", 5.0)
    trade_fraction: float = _float_env("POLYMARKET_TRADE_FRACTION", 0.10)
    btc_min_model_probability: float = _float_env("POLYMARKET_BTC_MIN_MODEL_PROBABILITY", 0.90)
    btc_min_buy_price: float = _float_env("POLYMARKET_BTC_MIN_BUY_PRICE", 0.70)
    btc_max_buy_price: float = _float_env("POLYMARKET_BTC_MAX_BUY_PRICE", 0.82)
    btc_min_edge: float = _float_env("POLYMARKET_BTC_MIN_EDGE", 0.08)
    btc_max_spread: float = _float_env("POLYMARKET_BTC_MAX_SPREAD", 0.03)
    btc_min_trade_usd: float = _float_env("POLYMARKET_BTC_MIN_TRADE_USD", 1.0)
    btc_max_trade_usd: float = _float_env("POLYMARKET_BTC_MAX_TRADE_USD", 25.0)
    btc_volatility_days: int = _int_env("POLYMARKET_BTC_VOLATILITY_DAYS", 7)
    auto_interval_seconds: int = _int_env("POLYMARKET_AUTO_INTERVAL_SECONDS", 300)
    auto_max_ticks: int = _int_env("POLYMARKET_AUTO_MAX_TICKS", 0)
    data_api_base_url: str = os.getenv("POLYMARKET_DATA_API_URL", "https://data-api.polymarket.com")
    smart_categories: str = os.getenv("POLYMARKET_SMART_CATEGORIES", "OVERALL,CRYPTO,FINANCE,ECONOMICS,TECH,POLITICS")
    smart_time_period: str = os.getenv("POLYMARKET_SMART_TIME_PERIOD", "WEEK")
    smart_leaderboard_limit: int = _int_env("POLYMARKET_SMART_LEADERBOARD_LIMIT", 15)
    smart_trade_lookback_minutes: int = _int_env("POLYMARKET_SMART_TRADE_LOOKBACK_MINUTES", 20)
    smart_min_consensus: int = _int_env("POLYMARKET_SMART_MIN_CONSENSUS", 2)
    smart_min_trader_pnl: float = _float_env("POLYMARKET_SMART_MIN_TRADER_PNL", 0.0)
    smart_min_trade_usd: float = _float_env("POLYMARKET_SMART_MIN_TRADE_USD", 25.0)
    smart_min_buy_price: float = _float_env("POLYMARKET_SMART_MIN_BUY_PRICE", 0.08)
    smart_max_buy_price: float = _float_env("POLYMARKET_SMART_MAX_BUY_PRICE", 0.85)
    smart_max_spread: float = _float_env("POLYMARKET_SMART_MAX_SPREAD", 0.04)
    smart_max_trade_usd: float = _float_env("POLYMARKET_SMART_MAX_TRADE_USD", 25.0)
    min_liquidity_usd: float = _float_env("POLYMARKET_MIN_LIQUIDITY_USD", 500.0)
    min_volume_usd: float = _float_env("POLYMARKET_MIN_VOLUME_USD", 1000.0)
    dashboard_host: str = os.getenv("POLYMARKET_DASHBOARD_HOST", "127.0.0.1")
    dashboard_port: int = _int_env("POLYMARKET_DASHBOARD_PORT", 8765)
    chain_id: int = _int_env("POLYMARKET_CHAIN_ID", 137)
    signature_type: int = _int_env("POLYMARKET_SIGNATURE_TYPE", 0)
    funder_address: str | None = os.getenv("POLYMARKET_FUNDER_ADDRESS") or None
    private_key: str | None = os.getenv("POLYMARKET_PRIVATE_KEY") or None
    api_key: str | None = os.getenv("POLYMARKET_API_KEY") or None
    api_secret: str | None = os.getenv("POLYMARKET_API_SECRET") or None
    api_passphrase: str | None = os.getenv("POLYMARKET_API_PASSPHRASE") or None
    relayer_api_key: str | None = os.getenv("RELAYER_API_KEY") or os.getenv("POLYMARKET_RELAYER_API_KEY") or None
    relayer_api_key_address: str | None = (
        os.getenv("RELAYER_API_KEY_ADDRESS") or os.getenv("POLYMARKET_RELAYER_API_KEY_ADDRESS") or None
    )
    live_trading_enabled: bool = os.getenv("POLYMARKET_ENABLE_LIVE_TRADING", "").lower() in {"1", "true", "yes"}
