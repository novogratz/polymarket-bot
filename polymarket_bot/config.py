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


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class Settings:
    gamma_base_url: str = os.getenv("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com")
    clob_base_url: str = os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com")
    state_path: Path = Path(os.getenv("POLYMARKET_STATE_PATH", "data/paper_state.json"))
    trade_journal_path: Path = Path(os.getenv("POLYMARKET_TRADE_JOURNAL_PATH", "data/trade_journal.jsonl"))
    strategy_overrides_path: Path = Path(os.getenv("POLYMARKET_STRATEGY_OVERRIDES_PATH", "data/strategy_overrides.json"))
    smart_auto_tune_enabled: bool = _bool_env("POLYMARKET_SMART_AUTO_TUNE_ENABLED", True)
    smart_auto_tune_min_trades: int = _int_env("POLYMARKET_SMART_AUTO_TUNE_MIN_TRADES", 30)
    scan_limit: int = _int_env("POLYMARKET_SCAN_LIMIT", 200)
    soon_hours: int = _int_env("POLYMARKET_SOON_HOURS", 72)
    paper_balance_usd: float = _float_env("POLYMARKET_PAPER_BALANCE_USD", 20.0)
    assumed_live_balance_usd: float = _float_env("POLYMARKET_ASSUME_LIVE_BALANCE_USD", 0.0)
    max_position_usd: float = _float_env("POLYMARKET_MAX_POSITION_USD", 5.0)
    trade_fraction: float = _float_env("POLYMARKET_TRADE_FRACTION", 1.0)
    btc_min_model_probability: float = _float_env("POLYMARKET_BTC_MIN_MODEL_PROBABILITY", 0.90)
    btc_min_buy_price: float = _float_env("POLYMARKET_BTC_MIN_BUY_PRICE", 0.05)
    btc_max_buy_price: float = _float_env("POLYMARKET_BTC_MAX_BUY_PRICE", 0.95)
    btc_min_edge: float = _float_env("POLYMARKET_BTC_MIN_EDGE", 0.05)
    btc_max_spread: float = _float_env("POLYMARKET_BTC_MAX_SPREAD", 0.10)
    btc_min_trade_usd: float = _float_env("POLYMARKET_BTC_MIN_TRADE_USD", 1.0)
    btc_max_trade_usd: float = _float_env("POLYMARKET_BTC_MAX_TRADE_USD", 50.0)
    btc_volatility_days: int = _int_env("POLYMARKET_BTC_VOLATILITY_DAYS", 7)
    auto_interval_seconds: int = _int_env("POLYMARKET_AUTO_INTERVAL_SECONDS", 10)
    auto_max_ticks: int = _int_env("POLYMARKET_AUTO_MAX_TICKS", 0)
    data_api_base_url: str = os.getenv("POLYMARKET_DATA_API_URL", "https://data-api.polymarket.com")
    smart_categories: str = os.getenv(
        "POLYMARKET_SMART_CATEGORIES",
        "OVERALL,FINANCE,ECONOMICS,TECH,POLITICS,SPORTS,CULTURE,WEATHER",
    )
    smart_discovery_keywords: str = os.getenv(
        "POLYMARKET_SMART_DISCOVERY_KEYWORDS",
        "election,trump,senate,congress,fed,inflation,cpi,unemployment,gdp,weather,rain,snow,"
        "hurricane,temperature,box office,movie,earnings,stock,nasdaq",
    )
    smart_time_period: str = os.getenv("POLYMARKET_SMART_TIME_PERIOD", "WEEK")
    smart_leaderboard_limit: int = _int_env("POLYMARKET_SMART_LEADERBOARD_LIMIT", 25)
    smart_scan_limit: int = _int_env("POLYMARKET_SMART_SCAN_LIMIT", 1000)
    smart_soon_hours: int = _int_env("POLYMARKET_SMART_SOON_HOURS", 72)
    smart_trade_lookback_minutes: int = _int_env("POLYMARKET_SMART_TRADE_LOOKBACK_MINUTES", 240)
    smart_max_signal_age_minutes: int = _int_env("POLYMARKET_SMART_MAX_SIGNAL_AGE_MINUTES", 0)
    smart_fresh_signal_bonus: float = _float_env("POLYMARKET_SMART_FRESH_SIGNAL_BONUS", 8.0)
    smart_min_consensus: int = _int_env("POLYMARKET_SMART_MIN_CONSENSUS", 2)
    smart_fallback_consensus: int = _int_env("POLYMARKET_SMART_FALLBACK_CONSENSUS", 2)
    min_open_positions: int = _int_env("POLYMARKET_MIN_OPEN_POSITIONS", 3)
    starter_trade_usd: float = _float_env("POLYMARKET_STARTER_TRADE_USD", 25.0)
    min_order_shares: float = _float_env("POLYMARKET_MIN_ORDER_SHARES", 5.0)
    smart_min_trader_pnl: float = _float_env("POLYMARKET_SMART_MIN_TRADER_PNL", 0.0)
    smart_min_trade_usd: float = _float_env("POLYMARKET_SMART_MIN_TRADE_USD", 1.0)
    smart_min_copied_usdc: float = _float_env("POLYMARKET_SMART_MIN_COPIED_USDC", 50.0)
    smart_min_buy_price: float = _float_env("POLYMARKET_SMART_MIN_BUY_PRICE", 0.02)
    smart_max_buy_price: float = _float_env("POLYMARKET_SMART_MAX_BUY_PRICE", 0.98)
    smart_max_spread: float = _float_env("POLYMARKET_SMART_MAX_SPREAD", 0.10)
    smart_min_hours_to_close: float = _float_env("POLYMARKET_SMART_MIN_HOURS_TO_CLOSE", 0.25)
    smart_max_hours_to_close: float = _float_env("POLYMARKET_SMART_MAX_HOURS_TO_CLOSE", 72.0)
    smart_max_chase_premium: float = _float_env("POLYMARKET_SMART_MAX_CHASE_PREMIUM", 0.10)
    smart_priority_category_bonus: float = _float_env("POLYMARKET_SMART_PRIORITY_CATEGORY_BONUS", 6.0)
    smart_sports_score_penalty: float = _float_env("POLYMARKET_SMART_SPORTS_SCORE_PENALTY", 8.0)
    smart_max_sports_positions: int = _int_env("POLYMARKET_SMART_MAX_SPORTS_POSITIONS", 3)
    smart_max_entry_slippage: float = _float_env("POLYMARKET_SMART_MAX_ENTRY_SLIPPAGE", 0.10)
    smart_crypto_micro_min_consensus: int = _int_env("POLYMARKET_SMART_CRYPTO_MICRO_MIN_CONSENSUS", 3)
    smart_crypto_micro_max_entry_slippage: float = _float_env("POLYMARKET_SMART_CRYPTO_MICRO_MAX_ENTRY_SLIPPAGE", 0.05)
    smart_crypto_micro_max_trade_usd: float = _float_env("POLYMARKET_SMART_CRYPTO_MICRO_MAX_TRADE_USD", 5.0)
    smart_allow_crypto: bool = _bool_env("POLYMARKET_SMART_ALLOW_CRYPTO", False)
    smart_crypto_min_hours_to_close: float = _float_env("POLYMARKET_SMART_CRYPTO_MIN_HOURS_TO_CLOSE", 6.0)
    smart_crypto_max_hours_to_close: float = _float_env("POLYMARKET_SMART_CRYPTO_MAX_HOURS_TO_CLOSE", 48.0)
    smart_crypto_min_copied_usdc: float = _float_env("POLYMARKET_SMART_CRYPTO_MIN_COPIED_USDC", 1000.0)
    smart_crypto_min_consensus: int = _int_env("POLYMARKET_SMART_CRYPTO_MIN_CONSENSUS", 3)
    smart_crypto_min_buy_price: float = _float_env("POLYMARKET_SMART_CRYPTO_MIN_BUY_PRICE", 0.70)
    smart_max_trade_usd: float = _float_env("POLYMARKET_SMART_MAX_TRADE_USD", 5.0)
    smart_position_pct: float = _float_env("POLYMARKET_SMART_POSITION_PCT", 0.0)
    smart_max_position_ceiling_usd: float = _float_env(
        "POLYMARKET_SMART_MAX_POSITION_CEILING_USD",
        50.0,
    )
    smart_cash_floor_pct: float = _float_env("POLYMARKET_SMART_CASH_FLOOR_PCT", 0.0)
    smart_high_conviction_balance_fraction: float = _float_env(
        "POLYMARKET_SMART_HIGH_CONVICTION_BALANCE_FRACTION",
        0.0,
    )
    smart_max_orders_per_tick: int = _int_env("POLYMARKET_SMART_MAX_ORDERS_PER_TICK", 0)
    smart_take_profit_tiers: str = os.getenv(
        "POLYMARKET_SMART_TAKE_PROFIT_TIERS",
        "1.0:0.50,2.0:0.25,3.0:0.15",
    )
    smart_peak_protect_trigger: float = _float_env("POLYMARKET_SMART_PEAK_PROTECT_TRIGGER", 1.0)
    smart_peak_protect_floor: float = _float_env("POLYMARKET_SMART_PEAK_PROTECT_FLOOR", 0.40)
    smart_stop_loss_pct: float = _float_env("POLYMARKET_SMART_STOP_LOSS_PCT", 0.40)
    smart_stop_loss_min_age_minutes: int = _int_env("POLYMARKET_SMART_STOP_LOSS_MIN_AGE_MINUTES", 15)
    smart_cohort_exit_enabled: bool = _bool_env("POLYMARKET_SMART_COHORT_EXIT_ENABLED", True)
    smart_cohort_exit_lookback_minutes: int = _int_env("POLYMARKET_SMART_COHORT_EXIT_LOOKBACK_MINUTES", 120)
    smart_cohort_exit_min_age_minutes: int = _int_env("POLYMARKET_SMART_COHORT_EXIT_MIN_AGE_MINUTES", 30)
    smart_cohort_exit_min_wallets: int = _int_env("POLYMARKET_SMART_COHORT_EXIT_MIN_WALLETS", 2)
    smart_max_relative_spread: float = _float_env("POLYMARKET_SMART_MAX_RELATIVE_SPREAD", 0.30)
    smart_deep_fallback_enabled: bool = _bool_env("POLYMARKET_SMART_DEEP_FALLBACK_ENABLED", True)
    smart_deep_fallback_min_copied_usdc: float = _float_env("POLYMARKET_SMART_DEEP_FALLBACK_MIN_COPIED_USDC", 250.0)
    smart_reverse_lookup_enabled: bool = _bool_env("POLYMARKET_SMART_REVERSE_LOOKUP_ENABLED", True)
    smart_reverse_lookup_max_tokens: int = _int_env("POLYMARKET_SMART_REVERSE_LOOKUP_MAX_TOKENS", 30)
    smart_reverse_lookup_min_copied_usdc: float = _float_env("POLYMARKET_SMART_REVERSE_LOOKUP_MIN_COPIED_USDC", 100.0)
    smart_reverse_lookup_min_liquidity_usd: float = _float_env("POLYMARKET_SMART_REVERSE_LOOKUP_MIN_LIQUIDITY_USD", 200.0)
    smart_reverse_lookup_min_volume_usd: float = _float_env("POLYMARKET_SMART_REVERSE_LOOKUP_MIN_VOLUME_USD", 500.0)
    smart_trade_fetch_concurrency: int = _int_env("POLYMARKET_SMART_TRADE_FETCH_CONCURRENCY", 16)
    smart_trailing_stop_arm_pct: float = _float_env("POLYMARKET_SMART_TRAILING_STOP_ARM_PCT", 0.25)
    smart_trailing_stop_giveback_pct: float = _float_env("POLYMARKET_SMART_TRAILING_STOP_GIVEBACK_PCT", 0.50)
    smart_min_trader_roi: float = _float_env("POLYMARKET_SMART_MIN_TRADER_ROI", 0.0)
    smart_min_trader_volume: float = _float_env("POLYMARKET_SMART_MIN_TRADER_VOLUME", 0.0)
    smart_min_sell_usd: float = _float_env("POLYMARKET_SMART_MIN_SELL_USD", 1.0)
    smart_exit_minutes_to_close: int = _int_env("POLYMARKET_SMART_EXIT_MINUTES_TO_CLOSE", 20)
    smart_exit_min_profit: float = _float_env("POLYMARKET_SMART_EXIT_MIN_PROFIT", 0.05)
    smart_pending_order_ttl_seconds: int = _int_env("POLYMARKET_SMART_PENDING_ORDER_TTL_SECONDS", 45)
    live_position_min_value_usd: float = _float_env("POLYMARKET_LIVE_POSITION_MIN_VALUE_USD", 1.0)
    sync_live_positions: bool = os.getenv("POLYMARKET_SYNC_LIVE_POSITIONS", "1").lower() in {"1", "true", "yes"}
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
