"""Centralised configuration for the Polymarket bot.

All runtime tuning lives in a single immutable :class:`Settings` dataclass
populated from environment variables (loaded from ``.env`` at import time).
Modules read from a ``Settings`` instance rather than reading the environment
directly, so tests can construct a custom ``Settings`` to exercise specific
code paths and the auto-tuner can produce a modified copy via
``dataclasses.replace``.

Tests set ``POLYMARKET_SKIP_DOTENV=1`` before importing this module so the
user's ``.env`` values do not leak into ``Settings`` field defaults — those
defaults are evaluated at class-definition time and would otherwise carry the
user's runtime overrides into the test fixtures.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


if not os.getenv("POLYMARKET_SKIP_DOTENV"):
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
    tick_state_path: Path = Path(os.getenv("POLYMARKET_TICK_STATE_PATH", "data/last_tick.json"))
    tick_history_path: Path = Path(os.getenv("POLYMARKET_TICK_HISTORY_PATH", "data/tick_history.jsonl"))
    smart_auto_tune_enabled: bool = _bool_env("POLYMARKET_SMART_AUTO_TUNE_ENABLED", True)
    smart_auto_tune_min_trades: int = _int_env("POLYMARKET_SMART_AUTO_TUNE_MIN_TRADES", 30)
    scan_limit: int = _int_env("POLYMARKET_SCAN_LIMIT", 200)
    soon_hours: int = _int_env("POLYMARKET_SOON_HOURS", 72)
    paper_balance_usd: float = _float_env("POLYMARKET_PAPER_BALANCE_USD", 0.0)
    assumed_live_balance_usd: float = _float_env("POLYMARKET_ASSUME_LIVE_BALANCE_USD", 0.0)
    max_position_usd: float = _float_env("POLYMARKET_MAX_POSITION_USD", 12.0)
    trade_fraction: float = _float_env("POLYMARKET_TRADE_FRACTION", 1.0)
    btc_edge_integrated: bool = _bool_env("POLYMARKET_BTC_EDGE_INTEGRATED", False)
    btc_min_model_probability: float = _float_env("POLYMARKET_BTC_MIN_MODEL_PROBABILITY", 0.51)
    btc_min_hours_to_close: float = _float_env("POLYMARKET_BTC_MIN_HOURS_TO_CLOSE", 0.10)
    btc_min_buy_price: float = _float_env("POLYMARKET_BTC_MIN_BUY_PRICE", 0.02)
    btc_max_buy_price: float = _float_env("POLYMARKET_BTC_MAX_BUY_PRICE", 0.98)
    btc_min_edge: float = _float_env("POLYMARKET_BTC_MIN_EDGE", 0.01)
    btc_max_spread: float = _float_env("POLYMARKET_BTC_MAX_SPREAD", 0.12)
    btc_min_trade_usd: float = _float_env("POLYMARKET_BTC_MIN_TRADE_USD", 1.0)
    btc_max_trade_usd: float = _float_env("POLYMARKET_BTC_MAX_TRADE_USD", 50.0)
    btc_volatility_days: int = _int_env("POLYMARKET_BTC_VOLATILITY_DAYS", 7)
    auto_interval_seconds: int = _int_env("POLYMARKET_AUTO_INTERVAL_SECONDS", 10)
    auto_max_ticks: int = _int_env("POLYMARKET_AUTO_MAX_TICKS", 0)
    data_api_base_url: str = os.getenv("POLYMARKET_DATA_API_URL", "https://data-api.polymarket.com")
    polygon_rpc_url: str = os.getenv("POLYMARKET_POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com")
    smart_categories: str = os.getenv(
        "POLYMARKET_SMART_CATEGORIES",
        "OVERALL,FINANCE,ECONOMICS,TECH,POLITICS,SPORTS,CULTURE,WEATHER,CRYPTO",
    )
    smart_discovery_keywords: str = os.getenv(
        "POLYMARKET_SMART_DISCOVERY_KEYWORDS",
        "election,trump,senate,congress,fed,inflation,cpi,unemployment,gdp,weather,rain,snow,"
        "hurricane,temperature,box office,movie,earnings,stock,nasdaq,gaming,crypto,token,game,film,show",
    )
    smart_time_period: str = os.getenv("POLYMARKET_SMART_TIME_PERIOD", "WEEK")
    smart_time_periods: str = os.getenv("POLYMARKET_SMART_TIME_PERIODS", "")
    smart_leaderboard_limit: int = _int_env("POLYMARKET_SMART_LEADERBOARD_LIMIT", 25)
    smart_max_traders: int = _int_env("POLYMARKET_SMART_MAX_TRADERS", 250)
    smart_scan_limit: int = _int_env("POLYMARKET_SMART_SCAN_LIMIT", 1000)
    smart_soon_hours: int = _int_env("POLYMARKET_SMART_SOON_HOURS", 72)
    smart_trade_lookback_minutes: int = _int_env("POLYMARKET_SMART_TRADE_LOOKBACK_MINUTES", 1440)
    smart_max_signal_age_minutes: int = _int_env("POLYMARKET_SMART_MAX_SIGNAL_AGE_MINUTES", 0)
    smart_fresh_signal_bonus: float = _float_env("POLYMARKET_SMART_FRESH_SIGNAL_BONUS", 8.0)
    smart_min_consensus: int = _int_env("POLYMARKET_SMART_MIN_CONSENSUS", 2)
    smart_fallback_consensus: int = _int_env("POLYMARKET_SMART_FALLBACK_CONSENSUS", 2)
    min_open_positions: int = _int_env("POLYMARKET_MIN_OPEN_POSITIONS", 20)
    starter_trade_usd: float = _float_env("POLYMARKET_STARTER_TRADE_USD", 25.0)
    min_order_shares: float = _float_env("POLYMARKET_MIN_ORDER_SHARES", 5.0)
    smart_min_trader_pnl: float = _float_env("POLYMARKET_SMART_MIN_TRADER_PNL", 0.0)
    smart_min_trade_usd: float = _float_env("POLYMARKET_SMART_MIN_TRADE_USD", 1.0)
    smart_min_copied_usdc: float = _float_env("POLYMARKET_SMART_MIN_COPIED_USDC", 5.0)
    smart_min_buy_price: float = _float_env("POLYMARKET_SMART_MIN_BUY_PRICE", 0.02)
    smart_max_buy_price: float = _float_env("POLYMARKET_SMART_MAX_BUY_PRICE", 0.98)
    smart_max_spread: float = _float_env("POLYMARKET_SMART_MAX_SPREAD", 0.25)
    smart_min_hours_to_close: float = _float_env("POLYMARKET_SMART_MIN_HOURS_TO_CLOSE", 0.10)
    smart_max_hours_to_close: float = _float_env("POLYMARKET_SMART_MAX_HOURS_TO_CLOSE", 168.0)
    smart_entry_max_horizon_hours: float = _float_env("POLYMARKET_SMART_ENTRY_MAX_HORIZON_HOURS", 168.0)
    smart_max_chase_premium: float = _float_env("POLYMARKET_SMART_MAX_CHASE_PREMIUM", 0.20)
    smart_priority_category_bonus: float = _float_env("POLYMARKET_SMART_PRIORITY_CATEGORY_BONUS", 10.0)
    smart_sports_score_penalty: float = _float_env("POLYMARKET_SMART_SPORTS_SCORE_PENALTY", 4.0)
    smart_max_sports_positions: int = _int_env("POLYMARKET_SMART_MAX_SPORTS_POSITIONS", 8)
    smart_max_entry_slippage: float = _float_env("POLYMARKET_SMART_MAX_ENTRY_SLIPPAGE", 0.50)
    smart_crypto_micro_min_consensus: int = _int_env("POLYMARKET_SMART_CRYPTO_MICRO_MIN_CONSENSUS", 3)
    smart_crypto_micro_max_entry_slippage: float = _float_env("POLYMARKET_SMART_CRYPTO_MICRO_MAX_ENTRY_SLIPPAGE", 0.25)
    smart_crypto_micro_max_trade_usd: float = _float_env("POLYMARKET_SMART_CRYPTO_MICRO_MAX_TRADE_USD", 5.0)
    smart_allow_crypto: bool = _bool_env("POLYMARKET_SMART_ALLOW_CRYPTO", True)
    smart_crypto_min_hours_to_close: float = _float_env("POLYMARKET_SMART_CRYPTO_MIN_HOURS_TO_CLOSE", 0.5)
    smart_crypto_max_hours_to_close: float = _float_env("POLYMARKET_SMART_CRYPTO_MAX_HOURS_TO_CLOSE", 168.0)
    smart_crypto_min_copied_usdc: float = _float_env("POLYMARKET_SMART_CRYPTO_MIN_COPIED_USDC", 10.0)
    smart_crypto_min_consensus: int = _int_env("POLYMARKET_SMART_CRYPTO_MIN_CONSENSUS", 1)
    smart_crypto_min_buy_price: float = _float_env("POLYMARKET_SMART_CRYPTO_MIN_BUY_PRICE", 0.05)
    smart_max_trade_usd: float = _float_env("POLYMARKET_SMART_MAX_TRADE_USD", 25.0)
    smart_position_pct: float = _float_env("POLYMARKET_SMART_POSITION_PCT", 0.50)
    smart_max_position_ceiling_usd: float = _float_env(
        "POLYMARKET_SMART_MAX_POSITION_CEILING_USD",
        150.0,
    )
    smart_max_position_ceiling_pct: float = _float_env("POLYMARKET_SMART_MAX_POSITION_CEILING_PCT", 0.40)
    smart_max_hold_hours: float = _float_env("POLYMARKET_SMART_MAX_HOLD_HOURS", 0.0)
    smart_recycle_profit_pct: float = _float_env("POLYMARKET_SMART_RECYCLE_PROFIT_PCT", 0.0)
    smart_recycle_profit_min_age_minutes: int = _int_env("POLYMARKET_SMART_RECYCLE_PROFIT_MIN_AGE_MINUTES", 60)
    smart_lock_gain_price: float = _float_env("POLYMARKET_SMART_LOCK_GAIN_PRICE", 0.95)
    smart_lock_gain_min_pnl_pct: float = _float_env("POLYMARKET_SMART_LOCK_GAIN_MIN_PNL_PCT", 0.20)
    smart_resolved_exit_threshold: float = _float_env("POLYMARKET_SMART_RESOLVED_EXIT_THRESHOLD", 0.9899)
    smart_cash_floor_pct: float = _float_env("POLYMARKET_SMART_CASH_FLOOR_PCT", 0.02)
    smart_cash_pressure_enabled: bool = _bool_env("POLYMARKET_SMART_CASH_PRESSURE_ENABLED", True)
    smart_cash_pressure_min_cash_pct: float = _float_env("POLYMARKET_SMART_CASH_PRESSURE_MIN_CASH_PCT", 0.20)
    smart_cash_pressure_min_copied_usdc: float = _float_env("POLYMARKET_SMART_CASH_PRESSURE_MIN_COPIED_USDC", 3.0)
    smart_cash_pressure_max_signal_age_minutes: int = _int_env(
        "POLYMARKET_SMART_CASH_PRESSURE_MAX_SIGNAL_AGE_MINUTES",
        2880,
    )
    smart_cash_pressure_max_relative_spread: float = _float_env(
        "POLYMARKET_SMART_CASH_PRESSURE_MAX_RELATIVE_SPREAD",
        0.75,
    )
    smart_non_sports_event_cap_usd: float = _float_env("POLYMARKET_SMART_NON_SPORTS_EVENT_CAP_USD", 50.0)
    smart_reentry_cooldown_minutes: int = _int_env("POLYMARKET_SMART_REENTRY_COOLDOWN_MINUTES", 60)
    smart_profitable_reentry_cooldown_minutes: int = _int_env(
        "POLYMARKET_SMART_PROFITABLE_REENTRY_COOLDOWN_MINUTES",
        10,
    )
    smart_fast_market_max_hours: float = _float_env("POLYMARKET_SMART_FAST_MARKET_MAX_HOURS", 48.0)
    smart_fast_market_score_bonus: float = _float_env("POLYMARKET_SMART_FAST_MARKET_SCORE_BONUS", 4.0)
    smart_noise_fallback_enabled: bool = _bool_env("POLYMARKET_SMART_NOISE_FALLBACK_ENABLED", False)
    smart_noise_fallback_max_trades_per_tick: int = _int_env("POLYMARKET_SMART_NOISE_FALLBACK_MAX_TRADES_PER_TICK", 4)
    smart_noise_fallback_max_trade_usd: float = _float_env("POLYMARKET_SMART_NOISE_FALLBACK_MAX_TRADE_USD", 10.0)
    smart_noise_fallback_min_buy_price: float = _float_env("POLYMARKET_SMART_NOISE_FALLBACK_MIN_BUY_PRICE", 0.20)
    smart_noise_fallback_max_buy_price: float = _float_env("POLYMARKET_SMART_NOISE_FALLBACK_MAX_BUY_PRICE", 0.80)
    smart_noise_fallback_max_spread: float = _float_env("POLYMARKET_SMART_NOISE_FALLBACK_MAX_SPREAD", 0.04)
    smart_noise_fallback_cash_pressure_pct: float = _float_env("POLYMARKET_SMART_NOISE_FALLBACK_CASH_PRESSURE_PCT", 0.90)
    smart_leaderboard_position_enabled: bool = _bool_env("POLYMARKET_SMART_LEADERBOARD_POSITION_ENABLED", False)
    smart_leaderboard_position_top_n: int = _int_env("POLYMARKET_SMART_LEADERBOARD_POSITION_TOP_N", 50)
    smart_leaderboard_position_min_wallets: int = _int_env("POLYMARKET_SMART_LEADERBOARD_POSITION_MIN_WALLETS", 3)
    smart_leaderboard_position_cash_pct: float = _float_env("POLYMARKET_SMART_LEADERBOARD_POSITION_CASH_PCT", 0.30)
    smart_leaderboard_position_fetch_concurrency: int = _int_env(
        "POLYMARKET_SMART_LEADERBOARD_POSITION_FETCH_CONCURRENCY",
        12,
    )
    smart_high_conviction_balance_fraction: float = _float_env(
        "POLYMARKET_SMART_HIGH_CONVICTION_BALANCE_FRACTION",
        0.80,
    )
    smart_max_orders_per_tick: int = _int_env("POLYMARKET_SMART_MAX_ORDERS_PER_TICK", 0)
    smart_take_profit_tiers: str = os.getenv(
        "POLYMARKET_SMART_TAKE_PROFIT_TIERS",
        "0.5:0.25,1.0:0.50,2.0:0.25,3.0:0.15",
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
    smart_deep_fallback_min_copied_usdc: float = _float_env("POLYMARKET_SMART_DEEP_FALLBACK_MIN_COPIED_USDC", 10.0)
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
    dry_run: bool = _bool_env("POLYMARKET_DRY_RUN", False)
    quiet: bool = _bool_env("POLYMARKET_QUIET", False)

    def __post_init__(self) -> None:
        """Swap ledger, journal, overrides, and tick-state paths to dry-run files.

        When POLYMARKET_DRY_RUN=1 is set, the bot reads/writes simulated state
        to a parallel set of files so the live paper-trading ledger, journal,
        auto-tuner overrides, and tick-state stream are never polluted. Each
        swap fires only when the current value still matches the live default;
        users who set a custom path via env keep their explicit choice.
        """
        if not self.dry_run:
            return
        swaps = (
            ("state_path", "data/paper_state.json", "data/dry_run_state.json"),
            ("trade_journal_path", "data/trade_journal.jsonl", "data/dry_run_journal.jsonl"),
            ("strategy_overrides_path", "data/strategy_overrides.json", "data/dry_run_strategy_overrides.json"),
            ("tick_state_path", "data/last_tick.json", "data/dry_run_last_tick.json"),
            ("tick_history_path", "data/tick_history.jsonl", "data/dry_run_tick_history.jsonl"),
        )
        for attr, live_default, dry_run_value in swaps:
            if str(getattr(self, attr)) == live_default:
                object.__setattr__(self, attr, Path(dry_run_value))
