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
from dataclasses import dataclass, field
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
    gamma_base_url: str = field(default_factory=lambda: os.getenv("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com"))
    clob_base_url: str = field(default_factory=lambda: os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com"))
    state_path: Path = field(default_factory=lambda: Path(os.getenv("POLYMARKET_STATE_PATH", "data/paper_state.json")))
    trade_journal_path: Path = field(default_factory=lambda: Path(os.getenv("POLYMARKET_TRADE_JOURNAL_PATH", "data/trade_journal.jsonl")))
    strategy_overrides_path: Path = field(default_factory=lambda: Path(os.getenv("POLYMARKET_STRATEGY_OVERRIDES_PATH", "data/strategy_overrides.json")))
    tick_state_path: Path = field(default_factory=lambda: Path(os.getenv("POLYMARKET_TICK_STATE_PATH", "data/last_tick.json")))
    tick_history_path: Path = field(default_factory=lambda: Path(os.getenv("POLYMARKET_TICK_HISTORY_PATH", "data/tick_history.jsonl")))
    smart_auto_tune_enabled: bool = field(default_factory=lambda: _bool_env("POLYMARKET_SMART_AUTO_TUNE_ENABLED", True))
    smart_auto_tune_min_trades: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_AUTO_TUNE_MIN_TRADES", 30))
    scan_limit: int = field(default_factory=lambda: _int_env("POLYMARKET_SCAN_LIMIT", 200))
    soon_hours: int = field(default_factory=lambda: _int_env("POLYMARKET_SOON_HOURS", 72))
    paper_balance_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_PAPER_BALANCE_USD", 20.0))
    assumed_live_balance_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_ASSUME_LIVE_BALANCE_USD", 0.0))
    max_position_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_MAX_POSITION_USD", 5.0))
    trade_fraction: float = field(default_factory=lambda: _float_env("POLYMARKET_TRADE_FRACTION", 1.0))
    btc_edge_integrated: bool = field(default_factory=lambda: _bool_env("POLYMARKET_BTC_EDGE_INTEGRATED", False))
    btc_min_model_probability: float = field(default_factory=lambda: _float_env("POLYMARKET_BTC_MIN_MODEL_PROBABILITY", 0.90))
    btc_min_hours_to_close: float = field(default_factory=lambda: _float_env("POLYMARKET_BTC_MIN_HOURS_TO_CLOSE", 0.5))
    btc_min_buy_price: float = field(default_factory=lambda: _float_env("POLYMARKET_BTC_MIN_BUY_PRICE", 0.05))
    btc_max_buy_price: float = field(default_factory=lambda: _float_env("POLYMARKET_BTC_MAX_BUY_PRICE", 0.95))
    btc_min_edge: float = field(default_factory=lambda: _float_env("POLYMARKET_BTC_MIN_EDGE", 0.08))
    btc_max_spread: float = field(default_factory=lambda: _float_env("POLYMARKET_BTC_MAX_SPREAD", 0.04))
    btc_min_trade_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_BTC_MIN_TRADE_USD", 1.0))
    btc_max_trade_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_BTC_MAX_TRADE_USD", 5.0))
    btc_volatility_days: int = field(default_factory=lambda: _int_env("POLYMARKET_BTC_VOLATILITY_DAYS", 7))
    auto_interval_seconds: int = field(default_factory=lambda: _int_env("POLYMARKET_AUTO_INTERVAL_SECONDS", 10))
    auto_max_ticks: int = field(default_factory=lambda: _int_env("POLYMARKET_AUTO_MAX_TICKS", 0))
    data_api_base_url: str = field(default_factory=lambda: os.getenv("POLYMARKET_DATA_API_URL", "https://data-api.polymarket.com"))
    polygon_rpc_url: str = field(default_factory=lambda: os.getenv("POLYMARKET_POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com"))
    smart_categories: str = field(default_factory=lambda: os.getenv(
        "POLYMARKET_SMART_CATEGORIES",
        "OVERALL,FINANCE,ECONOMICS,TECH,POLITICS,SPORTS,CULTURE,WEATHER",
    ))
    smart_discovery_keywords: str = field(default_factory=lambda: os.getenv(
        "POLYMARKET_SMART_DISCOVERY_KEYWORDS",
        "election,trump,senate,congress,fed,inflation,cpi,unemployment,gdp,weather,rain,snow,"
        "hurricane,temperature,box office,movie,earnings,stock,nasdaq",
    ))
    smart_time_period: str = field(default_factory=lambda: os.getenv("POLYMARKET_SMART_TIME_PERIOD", "WEEK"))
    smart_time_periods: str = field(default_factory=lambda: os.getenv("POLYMARKET_SMART_TIME_PERIODS", ""))
    smart_leaderboard_limit: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_LEADERBOARD_LIMIT", 25))
    smart_scan_limit: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_SCAN_LIMIT", 1000))
    smart_soon_hours: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_SOON_HOURS", 72))
    smart_trade_lookback_minutes: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_TRADE_LOOKBACK_MINUTES", 240))
    smart_max_signal_age_minutes: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_MAX_SIGNAL_AGE_MINUTES", 0))
    smart_fresh_signal_bonus: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_FRESH_SIGNAL_BONUS", 8.0))
    smart_min_consensus: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_MIN_CONSENSUS", 2))
    smart_fallback_consensus: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_FALLBACK_CONSENSUS", 2))
    min_open_positions: int = field(default_factory=lambda: _int_env("POLYMARKET_MIN_OPEN_POSITIONS", 3))
    starter_trade_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_STARTER_TRADE_USD", 25.0))
    min_order_shares: float = field(default_factory=lambda: _float_env("POLYMARKET_MIN_ORDER_SHARES", 5.0))
    smart_min_trader_pnl: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_MIN_TRADER_PNL", 0.0))
    smart_min_trade_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_MIN_TRADE_USD", 1.0))
    smart_min_copied_usdc: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_MIN_COPIED_USDC", 50.0))
    smart_min_buy_price: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_MIN_BUY_PRICE", 0.02))
    smart_max_buy_price: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_MAX_BUY_PRICE", 0.98))
    smart_max_spread: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_MAX_SPREAD", 0.10))
    smart_min_hours_to_close: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_MIN_HOURS_TO_CLOSE", 0.25))
    smart_max_hours_to_close: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_MAX_HOURS_TO_CLOSE", 72.0))
    smart_max_chase_premium: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_MAX_CHASE_PREMIUM", 0.10))
    smart_priority_category_bonus: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_PRIORITY_CATEGORY_BONUS", 6.0))
    smart_sports_score_penalty: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_SPORTS_SCORE_PENALTY", 8.0))
    smart_max_sports_positions: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_MAX_SPORTS_POSITIONS", 3))
    smart_max_entry_slippage: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_MAX_ENTRY_SLIPPAGE", 0.10))
    smart_crypto_micro_min_consensus: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_CRYPTO_MICRO_MIN_CONSENSUS", 3))
    smart_crypto_micro_max_entry_slippage: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_CRYPTO_MICRO_MAX_ENTRY_SLIPPAGE", 0.05))
    smart_crypto_micro_max_trade_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_CRYPTO_MICRO_MAX_TRADE_USD", 5.0))
    smart_allow_crypto: bool = field(default_factory=lambda: _bool_env("POLYMARKET_SMART_ALLOW_CRYPTO", False))
    smart_crypto_min_hours_to_close: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_CRYPTO_MIN_HOURS_TO_CLOSE", 6.0))
    smart_crypto_max_hours_to_close: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_CRYPTO_MAX_HOURS_TO_CLOSE", 48.0))
    smart_crypto_min_copied_usdc: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_CRYPTO_MIN_COPIED_USDC", 1000.0))
    smart_crypto_min_consensus: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_CRYPTO_MIN_CONSENSUS", 3))
    smart_crypto_min_buy_price: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_CRYPTO_MIN_BUY_PRICE", 0.70))
    smart_max_trade_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_MAX_TRADE_USD", 5.0))
    smart_position_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_POSITION_PCT", 0.0))
    smart_max_position_ceiling_usd: float = field(default_factory=lambda: _float_env(
        "POLYMARKET_SMART_MAX_POSITION_CEILING_USD",
        50.0,
    ))
    smart_max_position_ceiling_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_MAX_POSITION_CEILING_PCT", 0.0))
    smart_max_hold_hours: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_MAX_HOLD_HOURS", 0.0))
    smart_resolved_exit_threshold: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_RESOLVED_EXIT_THRESHOLD", 0.97))
    smart_cash_floor_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_CASH_FLOOR_PCT", 0.0))
    smart_noise_fallback_enabled: bool = field(default_factory=lambda: _bool_env("POLYMARKET_SMART_NOISE_FALLBACK_ENABLED", False))
    smart_noise_fallback_max_trades_per_tick: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_NOISE_FALLBACK_MAX_TRADES_PER_TICK", 2))
    smart_noise_fallback_max_trade_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_NOISE_FALLBACK_MAX_TRADE_USD", 5.0))
    smart_noise_fallback_min_buy_price: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_NOISE_FALLBACK_MIN_BUY_PRICE", 0.20))
    smart_noise_fallback_max_buy_price: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_NOISE_FALLBACK_MAX_BUY_PRICE", 0.80))
    smart_noise_fallback_max_spread: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_NOISE_FALLBACK_MAX_SPREAD", 0.04))
    smart_noise_fallback_cash_pressure_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_NOISE_FALLBACK_CASH_PRESSURE_PCT", 0.35))
    smart_high_conviction_balance_fraction: float = field(default_factory=lambda: _float_env(
        "POLYMARKET_SMART_HIGH_CONVICTION_BALANCE_FRACTION",
        0.0,
    ))
    smart_max_orders_per_tick: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_MAX_ORDERS_PER_TICK", 0))
    smart_take_profit_tiers: str = field(default_factory=lambda: os.getenv(
        "POLYMARKET_SMART_TAKE_PROFIT_TIERS",
        "0.5:0.25,1.0:0.50,2.0:0.25,3.0:0.15",
    ))
    smart_peak_protect_trigger: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_PEAK_PROTECT_TRIGGER", 1.0))
    smart_peak_protect_floor: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_PEAK_PROTECT_FLOOR", 0.40))
    smart_stop_loss_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_STOP_LOSS_PCT", 0.40))
    smart_stop_loss_min_age_minutes: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_STOP_LOSS_MIN_AGE_MINUTES", 15))
    smart_cohort_exit_enabled: bool = field(default_factory=lambda: _bool_env("POLYMARKET_SMART_COHORT_EXIT_ENABLED", True))
    smart_cohort_exit_lookback_minutes: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_COHORT_EXIT_LOOKBACK_MINUTES", 120))
    smart_cohort_exit_min_age_minutes: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_COHORT_EXIT_MIN_AGE_MINUTES", 30))
    smart_cohort_exit_min_wallets: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_COHORT_EXIT_MIN_WALLETS", 2))
    smart_max_relative_spread: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_MAX_RELATIVE_SPREAD", 0.30))
    smart_deep_fallback_enabled: bool = field(default_factory=lambda: _bool_env("POLYMARKET_SMART_DEEP_FALLBACK_ENABLED", True))
    smart_deep_fallback_min_copied_usdc: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_DEEP_FALLBACK_MIN_COPIED_USDC", 250.0))
    smart_reverse_lookup_enabled: bool = field(default_factory=lambda: _bool_env("POLYMARKET_SMART_REVERSE_LOOKUP_ENABLED", True))
    smart_reverse_lookup_max_tokens: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_REVERSE_LOOKUP_MAX_TOKENS", 30))
    smart_reverse_lookup_min_copied_usdc: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_REVERSE_LOOKUP_MIN_COPIED_USDC", 100.0))
    smart_reverse_lookup_min_liquidity_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_REVERSE_LOOKUP_MIN_LIQUIDITY_USD", 200.0))
    smart_reverse_lookup_min_volume_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_REVERSE_LOOKUP_MIN_VOLUME_USD", 500.0))
    smart_trade_fetch_concurrency: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_TRADE_FETCH_CONCURRENCY", 16))
    smart_trailing_stop_arm_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_TRAILING_STOP_ARM_PCT", 0.25))
    smart_trailing_stop_giveback_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_TRAILING_STOP_GIVEBACK_PCT", 0.50))
    smart_min_trader_roi: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_MIN_TRADER_ROI", 0.0))
    smart_min_trader_volume: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_MIN_TRADER_VOLUME", 0.0))
    smart_min_sell_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_MIN_SELL_USD", 1.0))

    # Filtre de persistance d'edge sur la cohorte smart-money
    persistence_enabled: bool = field(default_factory=lambda: _bool_env("POLYMARKET_PERSISTENCE_ENABLED", True))
    persistence_cache_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("POLYMARKET_PERSISTENCE_CACHE_PATH", "data/wallet_history.json")
        )
    )
    persistence_window_days: int = field(default_factory=lambda: _int_env("POLYMARKET_PERSISTENCE_WINDOW_DAYS", 30))
    persistence_cache_threshold: float = field(
        default_factory=lambda: _float_env("POLYMARKET_PERSISTENCE_CACHE_THRESHOLD", 0.70)
    )
    persistence_intersect_periods: str = field(
        default_factory=lambda: os.environ.get(
            "POLYMARKET_PERSISTENCE_INTERSECT_PERIODS", "WEEK,MONTH,ALL"
        )
    )
    persistence_intersect_min: int = field(default_factory=lambda: _int_env("POLYMARKET_PERSISTENCE_INTERSECT_MIN", 2))
    smart_exit_minutes_to_close: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_EXIT_MINUTES_TO_CLOSE", 20))
    smart_exit_min_profit: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_EXIT_MIN_PROFIT", 0.05))
    smart_pending_order_ttl_seconds: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_PENDING_ORDER_TTL_SECONDS", 45))
    live_position_min_value_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_LIVE_POSITION_MIN_VALUE_USD", 1.0))
    sync_live_positions: bool = field(default_factory=lambda: os.getenv("POLYMARKET_SYNC_LIVE_POSITIONS", "1").lower() in {"1", "true", "yes"})
    min_liquidity_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_MIN_LIQUIDITY_USD", 500.0))
    min_volume_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_MIN_VOLUME_USD", 1000.0))
    dashboard_host: str = field(default_factory=lambda: os.getenv("POLYMARKET_DASHBOARD_HOST", "127.0.0.1"))
    dashboard_port: int = field(default_factory=lambda: _int_env("POLYMARKET_DASHBOARD_PORT", 8765))
    chain_id: int = field(default_factory=lambda: _int_env("POLYMARKET_CHAIN_ID", 137))
    signature_type: int = field(default_factory=lambda: _int_env("POLYMARKET_SIGNATURE_TYPE", 0))
    funder_address: str | None = field(default_factory=lambda: os.getenv("POLYMARKET_FUNDER_ADDRESS") or None)
    private_key: str | None = field(default_factory=lambda: os.getenv("POLYMARKET_PRIVATE_KEY") or None)
    api_key: str | None = field(default_factory=lambda: os.getenv("POLYMARKET_API_KEY") or None)
    api_secret: str | None = field(default_factory=lambda: os.getenv("POLYMARKET_API_SECRET") or None)
    api_passphrase: str | None = field(default_factory=lambda: os.getenv("POLYMARKET_API_PASSPHRASE") or None)
    relayer_api_key: str | None = field(default_factory=lambda: os.getenv("RELAYER_API_KEY") or os.getenv("POLYMARKET_RELAYER_API_KEY") or None)
    relayer_api_key_address: str | None = field(default_factory=lambda: (
        os.getenv("RELAYER_API_KEY_ADDRESS") or os.getenv("POLYMARKET_RELAYER_API_KEY_ADDRESS") or None
    ))
    live_trading_enabled: bool = field(default_factory=lambda: os.getenv("POLYMARKET_ENABLE_LIVE_TRADING", "").lower() in {"1", "true", "yes"})
    dry_run: bool = field(default_factory=lambda: _bool_env("POLYMARKET_DRY_RUN", False))
    quiet: bool = field(default_factory=lambda: _bool_env("POLYMARKET_QUIET", False))

    run_mode: str = field(default_factory=lambda: os.getenv("POLYMARKET_RUN_MODE", "smart_money"))
    mirror_target: str = field(default_factory=lambda: os.getenv("POLYMARKET_MIRROR_TARGET", ""))
    mirror_size_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_MIRROR_SIZE_USD", 5.0))
    mirror_copy_ratio: float = field(default_factory=lambda: _float_env("POLYMARKET_MIRROR_COPY_RATIO", 0.20))
    mirror_max_position_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_MIRROR_MAX_POSITION_PCT", 0.02))
    mirror_max_open_positions: int = field(default_factory=lambda: _int_env("POLYMARKET_MIRROR_MAX_OPEN_POSITIONS", 8))
    mirror_max_category_exposure_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_MIRROR_MAX_CATEGORY_EXPOSURE_PCT", 0.20))
    mirror_whale_exit_fraction: float = field(default_factory=lambda: _float_env("POLYMARKET_MIRROR_WHALE_EXIT_FRACTION", 0.50))
    mirror_daily_loss_limit_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_MIRROR_DAILY_LOSS_LIMIT_PCT", 0.05))
    mirror_stop_loss_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_MIRROR_STOP_LOSS_PCT", 0.35))
    mirror_stop_loss_min_age_minutes: int = field(default_factory=lambda: _int_env("POLYMARKET_MIRROR_STOP_LOSS_MIN_AGE_MINUTES", 15))
    mirror_resolved_exit_threshold: float = field(default_factory=lambda: _float_env("POLYMARKET_MIRROR_RESOLVED_EXIT_THRESHOLD", 0.99))
    mirror_mirror_sells: bool = field(default_factory=lambda: _bool_env("POLYMARKET_MIRROR_MIRROR_SELLS", True))
    mirror_min_target_stake_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_MIRROR_MIN_TARGET_STAKE_USD", 50.0))
    mirror_max_chase_premium: float = field(default_factory=lambda: _float_env("POLYMARKET_MIRROR_MAX_CHASE_PREMIUM", 0.05))
    mirror_min_buy_price: float = field(default_factory=lambda: _float_env("POLYMARKET_MIRROR_MIN_BUY_PRICE", 0.02))
    mirror_max_buy_price: float = field(default_factory=lambda: _float_env("POLYMARKET_MIRROR_MAX_BUY_PRICE", 0.98))
    mirror_max_trade_age_seconds: int = field(default_factory=lambda: int(_float_env("POLYMARKET_MIRROR_MAX_TRADE_AGE_SECONDS", 60)))
    mirror_state_path: Path = field(default_factory=lambda: Path(os.getenv("POLYMARKET_MIRROR_STATE_PATH", "data/mirror_state.json")))

    mirror_discovery_enabled: bool = field(default_factory=lambda: _bool_env("POLYMARKET_MIRROR_DISCOVERY_ENABLED", True))
    mirror_discovery_interval_hours: int = field(default_factory=lambda: _int_env("POLYMARKET_MIRROR_DISCOVERY_INTERVAL_HOURS", 6))
    mirror_min_whale_pnl: float = field(default_factory=lambda: _float_env("POLYMARKET_MIRROR_MIN_WHALE_PNL", 10000.0))
    mirror_tiered_copy_ratios: str = field(default_factory=lambda: os.getenv("POLYMARKET_MIRROR_TIERED_COPY_RATIOS", "0.0:0.15,25000:0.25,100000:0.35"))
    mirror_weekly_loss_limit_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_MIRROR_WEEKLY_LOSS_LIMIT_PCT", 0.10))
    mirror_min_liquidity_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_MIRROR_MIN_LIQUIDITY_USD", 20000.0))
    mirror_max_days_to_expiry: int = field(default_factory=lambda: _int_env("POLYMARKET_MIRROR_MAX_DAYS_TO_EXPIRY", 3))

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
            ("persistence_cache_path", "data/wallet_history.json", "data/dry_run_wallet_history.json"),
            ("mirror_state_path", "data/mirror_state.json", "data/dry_run_mirror_state.json"),
        )
        for attr, live_default, dry_run_value in swaps:
            if str(getattr(self, attr)) == live_default:
                object.__setattr__(self, attr, Path(dry_run_value))
