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
    realized_cache_path: Path = field(default_factory=lambda: Path(os.getenv("POLYMARKET_REALIZED_CACHE_PATH", "data/realized_trade_cache.jsonl")))
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
    smart_min_wallet_flow_usdc: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_MIN_WALLET_FLOW_USDC", 0.0))
    smart_max_wallet_flow_share: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_MAX_WALLET_FLOW_SHARE", 0.85))
    smart_min_fresh_wallets: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_MIN_FRESH_WALLETS", 0))
    smart_fresh_wallet_minutes: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_FRESH_WALLET_MINUTES", 60))
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
    smart_deep_fallback_min_consensus: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_DEEP_FALLBACK_MIN_CONSENSUS", 1))
    # ── Whale-copy pass (bot 2 copy lane) ──────────────────────────────────
    # Independent of the cohort/consensus path: copy ANY single wallet's buy on
    # a token when its flow in the lookback window reaches the USDC threshold,
    # regardless of leaderboard membership. Restricted to the already-vetted
    # eligible universe so exclusions/crypto-ban/spread/liquidity still apply.
    # OFF by default — only bot 2's smart_b.toml turns it on.
    smart_whale_copy_enabled: bool = field(default_factory=lambda: _bool_env("POLYMARKET_SMART_WHALE_COPY_ENABLED", False))
    smart_whale_min_usdc: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_WHALE_MIN_USDC", 50000.0))
    smart_whale_lookback_minutes: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_WHALE_LOOKBACK_MINUTES", 60))
    smart_whale_max_orders_per_tick: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_WHALE_MAX_ORDERS_PER_TICK", 2))
    smart_whale_fetch_limit: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_WHALE_FETCH_LIMIT", 500))
    # Per-lane size multipliers on the computed max-trade (1.0 = no change).
    # Riskier lanes (single-wallet whale, falling-knife dip) sized below the
    # multi-wallet consensus.
    smart_whale_size_mult: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_WHALE_SIZE_MULT", 1.0))
    # ── Favorite-dip lane (bot 2) ──────────────────────────────────────────
    # Buy a favorite that just dropped: current ask in [min, max] AND the price
    # one window ago (ask - recent change) was >= reference_min, i.e. it was a
    # strong favorite that fell. Reference window = 1h by default (catches a
    # halftime goal drop); set use_day_change to use the 24h move instead.
    # OFF by default — only bot 2's smart_b.toml turns it on.
    smart_dip_buy_enabled: bool = field(default_factory=lambda: _bool_env("POLYMARKET_SMART_DIP_BUY_ENABLED", False))
    smart_dip_min_price: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_DIP_MIN_PRICE", 0.60))
    smart_dip_max_price: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_DIP_MAX_PRICE", 0.85))
    smart_dip_reference_min: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_DIP_REFERENCE_MIN", 0.86))
    smart_dip_use_day_change: bool = field(default_factory=lambda: _bool_env("POLYMARKET_SMART_DIP_USE_DAY_CHANGE", False))
    smart_dip_max_orders_per_tick: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_DIP_MAX_ORDERS_PER_TICK", 2))
    smart_dip_size_mult: float = field(default_factory=lambda: _float_env("POLYMARKET_SMART_DIP_SIZE_MULT", 1.0))
    # Global window for ALL bot-2 lanes (consensus/whale/dip): only take bets
    # that RESOLVE TODAY (end_date before the next UTC midnight). Naturally
    # tightens through the day. OFF by default.
    smart_expiring_today_only: bool = field(default_factory=lambda: _bool_env("POLYMARKET_SMART_EXPIRING_TODAY_ONLY", False))
    # One bet per GAME (not just per event): Polymarket files one match under
    # several events (moneyline / first-to-score / totals), so the lane could
    # stack 3 correlated legs of one game. When set, collapse to one pick per
    # game (date-truncated event slug + team names from the question). OFF by
    # default; only bot 2's smart_b.toml turns it on.
    smart_one_bet_per_game: bool = field(default_factory=lambda: _bool_env("POLYMARKET_SMART_ONE_BET_PER_GAME", False))
    # Copy lane: use the LIGHT exclusion set (crypto + stocks only) instead of
    # the grinder's full ban-list, so the lane can follow smart money into
    # draws / exact scores / halftime / O-U / weather markets (~10x the
    # universe). OFF by default; the grinder keeps the full exclusions.
    smart_copy_light_exclusions: bool = field(default_factory=lambda: _bool_env("POLYMARKET_SMART_COPY_LIGHT_EXCLUSIONS", False))
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
    # When True, force-close losing positions within
    # smart_near_expiry_loser_minutes of resolution. Caps the
    # \"resolved_market_sweep_loss\" tail (positions that go to $0 when
    # the market settles against the bot's bet). Tuned on the baseline
    # profile's 65-trade sample: 12 sweep_losses averaging -$3.11 each
    # were the biggest single bleed source.
    smart_near_expiry_exit_losers: bool = field(default_factory=lambda: os.getenv("POLYMARKET_SMART_NEAR_EXPIRY_EXIT_LOSERS", "0").lower() in {"1", "true", "yes"})
    smart_near_expiry_loser_minutes: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_NEAR_EXPIRY_LOSER_MINUTES", 30))
    smart_entry_cooldown_after_loss_minutes: int = field(default_factory=lambda: _int_env("POLYMARKET_SMART_ENTRY_COOLDOWN_AFTER_LOSS_MINUTES", 0))
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
    stdout_heartbeat_minutes: int = field(default_factory=lambda: _int_env("POLYMARKET_STDOUT_HEARTBEAT_MINUTES", 0))
    suppress_buy_logs: bool = field(default_factory=lambda: _bool_env("POLYMARKET_SUPPRESS_BUY_LOGS", False))

    run_mode: str = field(default_factory=lambda: os.getenv("POLYMARKET_RUN_MODE", "smart_money"))

    # News strategy — momentum on markets expiring within a short window.
    news_max_hours: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_MAX_HOURS", 4.0))
    news_min_hours: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_MIN_HOURS", 0.083))
    news_min_price: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_MIN_PRICE", 0.10))
    news_max_price: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_MAX_PRICE", 0.85))
    news_max_spread: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_MAX_SPREAD", 0.04))
    news_max_relative_spread: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_MAX_RELATIVE_SPREAD", 0.30))
    news_min_liquidity_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_MIN_LIQUIDITY_USD", 300.0))
    news_min_volume_24h_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_MIN_VOLUME_24H_USD", 200.0))
    news_require_positive_momentum: bool = field(default_factory=lambda: _bool_env("POLYMARKET_NEWS_REQUIRE_POSITIVE_MOMENTUM", True))
    news_min_abs_momentum: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_MIN_ABS_MOMENTUM", 0.02))
    news_take_profit_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_TAKE_PROFIT_PCT", 0.25))
    news_stop_loss_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_STOP_LOSS_PCT", 0.50))
    news_stop_loss_min_age_minutes: int = field(default_factory=lambda: _int_env("POLYMARKET_NEWS_STOP_LOSS_MIN_AGE_MINUTES", 5))
    news_near_expiry_min_profit: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_NEAR_EXPIRY_MIN_PROFIT", 0.0))
    news_near_expiry_minutes: int = field(default_factory=lambda: _int_env("POLYMARKET_NEWS_NEAR_EXPIRY_MINUTES", 5))
    news_resolved_exit_threshold: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_RESOLVED_EXIT_THRESHOLD", 0.97))
    news_max_orders_per_tick: int = field(default_factory=lambda: _int_env("POLYMARKET_NEWS_MAX_ORDERS_PER_TICK", 3))
    news_stake_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_STAKE_USD", 5.0))
    news_scan_limit: int = field(default_factory=lambda: _int_env("POLYMARKET_NEWS_SCAN_LIMIT", 500))
    news_cash_floor_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_CASH_FLOOR_PCT", 0.10))
    news_partial_tp_fraction: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_PARTIAL_TP_FRACTION", 0.50))
    news_trailing_arm_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_TRAILING_ARM_PCT", 0.35))
    news_trailing_giveback_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_TRAILING_GIVEBACK_PCT", 0.50))
    news_max_stake_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_MAX_STAKE_USD", 12.0))
    news_min_stake_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_MIN_STAKE_USD", 3.0))
    news_smart_money_boost_enabled: bool = field(default_factory=lambda: _bool_env("POLYMARKET_NEWS_SMART_MONEY_BOOST_ENABLED", True))
    news_smart_money_min_flow_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_SMART_MONEY_MIN_FLOW_USD", 250.0))
    news_tight_stop_hours: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_TIGHT_STOP_HOURS", 1.0))
    news_tight_stop_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_TIGHT_STOP_PCT", 0.15))
    news_very_tight_stop_hours: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_VERY_TIGHT_STOP_HOURS", 0.5))
    news_very_tight_stop_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_NEWS_VERY_TIGHT_STOP_PCT", 0.10))

    # Edge strategy — multi-lane (arb, crypto directional, near-cert, scalp).
    edge_max_hours: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_MAX_HOURS", 4.0))
    edge_scan_limit: int = field(default_factory=lambda: _int_env("POLYMARKET_EDGE_SCAN_LIMIT", 500))
    edge_min_liquidity_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_MIN_LIQUIDITY_USD", 1000.0))
    edge_min_volume_24h_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_MIN_VOLUME_24H_USD", 300.0))
    edge_min_price: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_MIN_PRICE", 0.05))
    edge_max_price: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_MAX_PRICE", 0.95))
    edge_max_spread: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_MAX_SPREAD", 0.05))
    edge_fee_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_FEE_PCT", 0.02))
    edge_min_edge_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_MIN_EDGE_PCT", 0.04))
    edge_kelly_fraction: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_KELLY_FRACTION", 0.25))
    edge_max_position_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_MAX_POSITION_PCT", 0.15))
    edge_max_orders_per_tick: int = field(default_factory=lambda: _int_env("POLYMARKET_EDGE_MAX_ORDERS_PER_TICK", 4))
    edge_min_stake_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_MIN_STAKE_USD", 2.0))
    edge_cash_floor_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_CASH_FLOOR_PCT", 0.10))
    edge_daily_drawdown_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_DAILY_DRAWDOWN_PCT", 0.20))
    edge_take_profit_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_TAKE_PROFIT_PCT", 0.25))
    edge_stop_loss_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_STOP_LOSS_PCT", 0.25))
    edge_stop_loss_min_age_minutes: int = field(default_factory=lambda: _int_env("POLYMARKET_EDGE_STOP_LOSS_MIN_AGE_MINUTES", 3))
    edge_tight_stop_hours: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_TIGHT_STOP_HOURS", 1.0))
    edge_tight_stop_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_TIGHT_STOP_PCT", 0.15))
    edge_very_tight_stop_hours: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_VERY_TIGHT_STOP_HOURS", 0.5))
    edge_very_tight_stop_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_VERY_TIGHT_STOP_PCT", 0.10))
    edge_near_expiry_minutes: int = field(default_factory=lambda: _int_env("POLYMARKET_EDGE_NEAR_EXPIRY_MINUTES", 5))
    edge_resolved_exit_threshold: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_RESOLVED_EXIT_THRESHOLD", 0.97))
    # Arb lane.
    edge_arb_fee_buffer: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_ARB_FEE_BUFFER", 0.02))
    edge_arb_max_position_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_ARB_MAX_POSITION_PCT", 0.25))
    # Crypto lane.
    edge_crypto_enabled: bool = field(default_factory=lambda: _bool_env("POLYMARKET_EDGE_CRYPTO_ENABLED", True))
    edge_crypto_direction_enabled: bool = field(default_factory=lambda: _bool_env("POLYMARKET_EDGE_CRYPTO_DIRECTION_ENABLED", True))
    edge_crypto_annual_vol: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_CRYPTO_ANNUAL_VOL", 0.60))
    edge_crypto_momentum_alpha: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_CRYPTO_MOMENTUM_ALPHA", 4.0))
    # Near-cert lane.
    edge_near_cert_enabled: bool = field(default_factory=lambda: _bool_env("POLYMARKET_EDGE_NEAR_CERT_ENABLED", True))
    edge_near_cert_max_hours: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_NEAR_CERT_MAX_HOURS", 2.0))
    edge_near_cert_min_bid: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_NEAR_CERT_MIN_BID", 0.92))
    edge_near_cert_max_ask: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_NEAR_CERT_MAX_ASK", 0.96))
    edge_near_cert_bias_multiplier: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_NEAR_CERT_BIAS_MULTIPLIER", 1.05))
    # Scalp lane.
    edge_scalp_enabled: bool = field(default_factory=lambda: _bool_env("POLYMARKET_EDGE_SCALP_ENABLED", False))
    edge_scalp_min_volume_24h: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_SCALP_MIN_VOLUME_24H", 5000.0))
    edge_scalp_max_position_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_SCALP_MAX_POSITION_PCT", 0.05))
    edge_scalp_tp_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_SCALP_TP_PCT", 0.03))
    edge_scalp_sl_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_EDGE_SCALP_SL_PCT", 0.05))
    edge_scalp_max_age_minutes: int = field(default_factory=lambda: _int_env("POLYMARKET_EDGE_SCALP_MAX_AGE_MINUTES", 15))

    # Race strategies — shared knobs for random/contrarian/favorite control bots.
    race_max_hours: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_MAX_HOURS", 4.0))
    # Dynamic entry window (2026-06-11): when the base race_max_hours window
    # yields no actionable candidate, the scan widens in 2h steps to 12h,
    # then jumps straight to this cap (4 → 6 → 8 → 10 → 12 → 24). 0 or
    # ≤ race_max_hours disables the ladder.
    race_max_hours_cap: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_MAX_HOURS_CAP", 0.0))
    # Final ladder rung (2026-06-12): when even the cap window is empty,
    # extend to the end of TOMORROW (UTC) so daily markets ("Will X be Y on
    # <date>?", stamped midnight UTC like the Trump-approval one) stay
    # reachable. Off by default.
    race_daily_expiry_fallback: bool = field(default_factory=lambda: _bool_env("POLYMARKET_RACE_DAILY_EXPIRY_FALLBACK", False))
    race_scan_limit: int = field(default_factory=lambda: _int_env("POLYMARKET_RACE_SCAN_LIMIT", 500))
    race_min_liquidity_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_MIN_LIQUIDITY_USD", 500.0))
    race_min_volume_24h_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_MIN_VOLUME_24H_USD", 200.0))
    race_min_price: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_MIN_PRICE", 0.05))
    race_max_price: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_MAX_PRICE", 0.95))
    race_max_spread: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_MAX_SPREAD", 0.05))
    race_stake_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_STAKE_USD", 5.0))
    race_stake_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_STAKE_PCT", 0.15))
    # Initial-entry size as a fraction of equity (user 2026-06-14): a FRESH
    # entry (and any passive top-up) targets this, leaving headroom up to the
    # full race_stake_pct cap for the dip double-down to fill. 0 or
    # ≥ race_stake_pct disables the reservation (entries target the full cap,
    # old behavior). Example: initial 0.05 + cap 0.10 → open at 5%, double
    # down to 10% on a dip.
    race_initial_stake_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_INITIAL_STAKE_PCT", 0.0))
    # Dip double-down (user 2026-06-14): when an OPEN position's live ask
    # dips at least ``min_dip`` below its entry AND is still "alive" (ask ≥
    # ``min_price``, default 0.60), buy more (average down) toward the
    # per-position cap — once per position. The 0.60 floor is the
    # deterministic proxy for "the bet is still going well" (a low-scoring
    # Under stays priced high; the bot has no live-score feed). The 10%
    # per-bet cap is never breached. Off unless enabled in the profile.
    race_double_down_enabled: bool = field(default_factory=lambda: _bool_env("POLYMARKET_RACE_DOUBLE_DOWN_ENABLED", False))
    race_double_down_min_dip: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_DOUBLE_DOWN_MIN_DIP", 0.01))
    # Max dip below entry that still doubles down (0 = no upper bound). The
    # min_price floor (0.60) is the real "still a favorite" gate; this is a
    # secondary safety cap. 0.40 is effectively non-binding for valid entries.
    race_double_down_max_dip: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_DOUBLE_DOWN_MAX_DIP", 0.40))
    race_double_down_min_price: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_DOUBLE_DOWN_MIN_PRICE", 0.60))
    # Dynamic take-profit margin (user 2026-06-15): the resolved-exit must
    # clear the entry by at least this, capped at 0.99 — so a high-entry
    # favorite (e.g. 0.97) sells at 0.99, never at break-even. The effective
    # exit threshold = min(0.99, max(resolved_exit_threshold, entry + margin)).
    race_min_profit_margin: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_MIN_PROFIT_MARGIN", 0.02))
    race_max_orders_per_tick: int = field(default_factory=lambda: _int_env("POLYMARKET_RACE_MAX_ORDERS_PER_TICK", 3))
    # Max concurrent bets on the SAME game (default 1 = the standing one-bet-
    # per-game anti-stacking rule). Bot 2 raises this to 2 to surface more bets
    # when the board clusters on a few games; the per-bet cap still bounds total
    # game exposure. Honored by _dedup_same_game, the cross-tick open-game block,
    # and the in-loop event-exposure backstop.
    race_max_bets_per_game: int = field(default_factory=lambda: _int_env("POLYMARKET_RACE_MAX_BETS_PER_GAME", 1))
    race_cash_floor_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_CASH_FLOOR_PCT", 0.10))
    race_tp_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_TP_PCT", 0.25))
    race_sl_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_SL_PCT", 0.50))
    # Controlled stop-loss: number of CONSECUTIVE ticks the loss must persist
    # past race_sl_pct before the SL fires. >1 means a one-tick thin-book
    # phantom bid can't trigger a sale (the 2026-05-31 disaster). SL is disabled
    # entirely when race_sl_pct >= 1.0.
    race_sl_confirm_ticks: int = field(default_factory=lambda: _int_env("POLYMARKET_RACE_SL_CONFIRM_TICKS", 3))
    race_sl_min_age_minutes: int = field(default_factory=lambda: _int_env("POLYMARKET_RACE_SL_MIN_AGE_MINUTES", 5))
    race_near_expiry_minutes: int = field(default_factory=lambda: _int_env("POLYMARKET_RACE_NEAR_EXPIRY_MINUTES", 5))
    race_resolved_exit_threshold: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_RESOLVED_EXIT_THRESHOLD", 0.97))
    race_limit_sell_trigger: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_LIMIT_SELL_TRIGGER", 0.0))
    race_limit_sell_price: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_LIMIT_SELL_PRICE", 0.98))
    # Daily drawdown halt removed per user (2026-06-07) — 0 disables the gate.
    race_daily_drawdown_pct: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_DAILY_DRAWDOWN_PCT", 0.0))
    race_noise_fallback_enabled: bool = field(default_factory=lambda: _bool_env("POLYMARKET_RACE_NOISE_FALLBACK_ENABLED", False))
    # Binary intra-market arbitrage: buy both YES and NO when their combined
    # ask cost < threshold → guaranteed profit regardless of resolution.
    # 0.0 = disabled. Grinder sets to 0.97 (min 3% guaranteed profit).
    race_arb_threshold: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_ARB_THRESHOLD", 0.0))
    race_arb_max_stake_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_ARB_MAX_STAKE_USD", 5.0))
    # Minutes past a market's endDate before force-closing a stuck position.
    # Polymarket closes sports O/U betting at kickoff but the market resolves
    # 90+ min later after full time — keep this well above 90.
    race_expiry_grace_min: int = field(default_factory=lambda: int(_float_env("POLYMARKET_RACE_EXPIRY_GRACE_MIN", 150)))
    race_contrarian_min_momentum: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_CONTRARIAN_MIN_MOMENTUM", 0.03))
    race_favorite_min_bid: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_FAVORITE_MIN_BID", 0.65))
    race_breakout_min_momentum: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_BREAKOUT_MIN_MOMENTUM", 0.05))
    race_breakout_min_volume: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_BREAKOUT_MIN_VOLUME", 5000.0))
    race_late_favorite_min_bid: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_LATE_FAVORITE_MIN_BID", 0.75))
    race_late_favorite_max_hours: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_LATE_FAVORITE_MAX_HOURS", 0.5))
    race_panic_fade_min_move: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_PANIC_FADE_MIN_MOVE", 0.15))
    race_panic_fade_min_volume: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_PANIC_FADE_MIN_VOLUME", 3000.0))
    race_underdog_max_ask: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_UNDERDOG_MAX_ASK", 0.30))
    race_underdog_min_momentum: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_UNDERDOG_MIN_MOMENTUM", 0.03))
    race_underdog_min_volume: float = field(default_factory=lambda: _float_env("POLYMARKET_RACE_UNDERDOG_MIN_VOLUME", 2000.0))
    mirror_target: str = field(default_factory=lambda: os.getenv("POLYMARKET_MIRROR_TARGET", ""))
    mirror_size_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_MIRROR_SIZE_USD", 5.0))
    mirror_mirror_sells: bool = field(default_factory=lambda: _bool_env("POLYMARKET_MIRROR_MIRROR_SELLS", True))
    mirror_min_target_stake_usd: float = field(default_factory=lambda: _float_env("POLYMARKET_MIRROR_MIN_TARGET_STAKE_USD", 50.0))
    mirror_max_chase_premium: float = field(default_factory=lambda: _float_env("POLYMARKET_MIRROR_MAX_CHASE_PREMIUM", 0.05))
    mirror_min_buy_price: float = field(default_factory=lambda: _float_env("POLYMARKET_MIRROR_MIN_BUY_PRICE", 0.02))
    mirror_max_buy_price: float = field(default_factory=lambda: _float_env("POLYMARKET_MIRROR_MAX_BUY_PRICE", 0.98))
    mirror_max_trade_age_seconds: int = field(default_factory=lambda: int(_float_env("POLYMARKET_MIRROR_MAX_TRADE_AGE_SECONDS", 60)))
    mirror_state_path: Path = field(default_factory=lambda: Path(os.getenv("POLYMARKET_MIRROR_STATE_PATH", "data/mirror_state.json")))

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
            ("realized_cache_path", "data/realized_trade_cache.jsonl", "data/dry_run_realized_trade_cache.jsonl"),
            ("strategy_overrides_path", "data/strategy_overrides.json", "data/dry_run_strategy_overrides.json"),
            ("tick_state_path", "data/last_tick.json", "data/dry_run_last_tick.json"),
            ("tick_history_path", "data/tick_history.jsonl", "data/dry_run_tick_history.jsonl"),
            ("persistence_cache_path", "data/wallet_history.json", "data/dry_run_wallet_history.json"),
            ("mirror_state_path", "data/mirror_state.json", "data/dry_run_mirror_state.json"),
        )
        for attr, live_default, dry_run_value in swaps:
            if str(getattr(self, attr)) == live_default:
                object.__setattr__(self, attr, Path(dry_run_value))
