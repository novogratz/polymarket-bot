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
        "mode": ("POLYMARKET_RUN_MODE", "str"),
    },
    "mirror": {
        "target": ("POLYMARKET_MIRROR_TARGET", "str"),
        "size_usd": ("POLYMARKET_MIRROR_SIZE_USD", "float"),
        "mirror_sells": ("POLYMARKET_MIRROR_MIRROR_SELLS", "bool"),
        "min_target_stake_usd": ("POLYMARKET_MIRROR_MIN_TARGET_STAKE_USD", "float"),
        "max_chase_premium": ("POLYMARKET_MIRROR_MAX_CHASE_PREMIUM", "float"),
        "min_buy_price": ("POLYMARKET_MIRROR_MIN_BUY_PRICE", "float"),
        "max_buy_price": ("POLYMARKET_MIRROR_MAX_BUY_PRICE", "float"),
        "max_trade_age_seconds": ("POLYMARKET_MIRROR_MAX_TRADE_AGE_SECONDS", "int"),
    },
    "sizing": {
        "position_pct": ("POLYMARKET_SMART_POSITION_PCT", "float"),
        "max_position_ceiling_usd": ("POLYMARKET_SMART_MAX_POSITION_CEILING_USD", "float"),
        "max_position_ceiling_pct": ("POLYMARKET_SMART_MAX_POSITION_CEILING_PCT", "float"),
        "max_trade_usd": ("POLYMARKET_SMART_MAX_TRADE_USD", "float"),
        "high_conviction_balance_fraction": ("POLYMARKET_SMART_HIGH_CONVICTION_BALANCE_FRACTION", "float"),
        "cash_floor_pct": ("POLYMARKET_SMART_CASH_FLOOR_PCT", "float"),
        "min_open_positions": ("POLYMARKET_MIN_OPEN_POSITIONS", "int"),
        "starter_trade_usd": ("POLYMARKET_STARTER_TRADE_USD", "float"),
        "assumed_live_balance_usd": ("POLYMARKET_ASSUME_LIVE_BALANCE_USD", "float"),
    },
    "trader_cohort": {
        "leaderboard_window": ("POLYMARKET_SMART_TIME_PERIOD", "str"),
        "leaderboard_windows": ("POLYMARKET_SMART_TIME_PERIODS", "str"),
        "top_n": ("POLYMARKET_SMART_LEADERBOARD_LIMIT", "int"),
        "min_trader_pnl": ("POLYMARKET_SMART_MIN_TRADER_PNL", "float"),
        "min_trader_volume": ("POLYMARKET_SMART_MIN_TRADER_VOLUME", "float"),
        "min_trader_roi": ("POLYMARKET_SMART_MIN_TRADER_ROI", "float"),
        "trade_fetch_concurrency": ("POLYMARKET_SMART_TRADE_FETCH_CONCURRENCY", "int"),
        "trade_lookback_minutes": ("POLYMARKET_SMART_TRADE_LOOKBACK_MINUTES", "int"),
    },
    "discovery": {
        "scan_limit": ("POLYMARKET_SMART_SCAN_LIMIT", "int"),
        "soon_hours": ("POLYMARKET_SMART_SOON_HOURS", "int"),
        "fresh_signal_bonus": ("POLYMARKET_SMART_FRESH_SIGNAL_BONUS", "float"),
        "priority_category_bonus": ("POLYMARKET_SMART_PRIORITY_CATEGORY_BONUS", "float"),
    },
    "market_filters": {
        "min_liquidity_usd": ("POLYMARKET_MIN_LIQUIDITY_USD", "float"),
        "min_volume_usd": ("POLYMARKET_MIN_VOLUME_USD", "float"),
    },
    "filters": {
        "min_consensus": ("POLYMARKET_SMART_MIN_CONSENSUS", "int"),
        "fallback_consensus": ("POLYMARKET_SMART_FALLBACK_CONSENSUS", "int"),
        "min_copied_usdc": ("POLYMARKET_SMART_MIN_COPIED_USDC", "float"),
        "min_wallet_flow_usdc": ("POLYMARKET_SMART_MIN_WALLET_FLOW_USDC", "float"),
        "max_wallet_flow_share": ("POLYMARKET_SMART_MAX_WALLET_FLOW_SHARE", "float"),
        "min_fresh_wallets": ("POLYMARKET_SMART_MIN_FRESH_WALLETS", "int"),
        "fresh_wallet_minutes": ("POLYMARKET_SMART_FRESH_WALLET_MINUTES", "int"),
        "min_trade_usd": ("POLYMARKET_SMART_MIN_TRADE_USD", "float"),
        "max_chase_premium": ("POLYMARKET_SMART_MAX_CHASE_PREMIUM", "float"),
        "price_min": ("POLYMARKET_SMART_MIN_BUY_PRICE", "float"),
        "price_max": ("POLYMARKET_SMART_MAX_BUY_PRICE", "float"),
        "max_absolute_spread": ("POLYMARKET_SMART_MAX_SPREAD", "float"),
        "max_relative_spread": ("POLYMARKET_SMART_MAX_RELATIVE_SPREAD", "float"),
        "signal_staleness_seconds": ("POLYMARKET_SMART_MAX_SIGNAL_AGE_MINUTES", "int"),
        "min_hours_to_close": ("POLYMARKET_SMART_MIN_HOURS_TO_CLOSE", "float"),
        "max_hours_to_close": ("POLYMARKET_SMART_MAX_HOURS_TO_CLOSE", "float"),
        "max_orders_per_tick": ("POLYMARKET_SMART_MAX_ORDERS_PER_TICK", "int"),
        "max_sports_positions": ("POLYMARKET_SMART_MAX_SPORTS_POSITIONS", "int"),
        "sports_score_penalty": ("POLYMARKET_SMART_SPORTS_SCORE_PENALTY", "float"),
    },
    "crypto": {
        "min_buy_price": ("POLYMARKET_SMART_CRYPTO_MIN_BUY_PRICE", "float"),
        "min_hours_to_close": ("POLYMARKET_SMART_CRYPTO_MIN_HOURS_TO_CLOSE", "float"),
        "max_hours_to_close": ("POLYMARKET_SMART_CRYPTO_MAX_HOURS_TO_CLOSE", "float"),
        "min_copied_usdc": ("POLYMARKET_SMART_CRYPTO_MIN_COPIED_USDC", "float"),
        "min_consensus": ("POLYMARKET_SMART_CRYPTO_MIN_CONSENSUS", "int"),
        "micro_min_consensus": ("POLYMARKET_SMART_CRYPTO_MICRO_MIN_CONSENSUS", "int"),
        "micro_max_entry_slippage": ("POLYMARKET_SMART_CRYPTO_MICRO_MAX_ENTRY_SLIPPAGE", "float"),
        "micro_max_trade_usd": ("POLYMARKET_SMART_CRYPTO_MICRO_MAX_TRADE_USD", "float"),
    },
    "execution": {
        "max_entry_slippage": ("POLYMARKET_SMART_MAX_ENTRY_SLIPPAGE", "float"),
        "pending_order_ttl_seconds": ("POLYMARKET_SMART_PENDING_ORDER_TTL_SECONDS", "int"),
        "min_sell_usd": ("POLYMARKET_SMART_MIN_SELL_USD", "float"),
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
        "near_expiry_min_profit": ("POLYMARKET_SMART_EXIT_MIN_PROFIT", "float"),
        "near_expiry_minutes_to_close": ("POLYMARKET_SMART_EXIT_MINUTES_TO_CLOSE", "int"),
        "near_expiry_exit_losers": ("POLYMARKET_SMART_NEAR_EXPIRY_EXIT_LOSERS", "bool"),
        "near_expiry_loser_minutes": ("POLYMARKET_SMART_NEAR_EXPIRY_LOSER_MINUTES", "int"),
        "entry_cooldown_after_loss_minutes": ("POLYMARKET_SMART_ENTRY_COOLDOWN_AFTER_LOSS_MINUTES", "int"),
        "resolved_market_threshold": ("POLYMARKET_SMART_RESOLVED_EXIT_THRESHOLD", "float"),
    },
    "cohort_exit": {
        "enabled": ("POLYMARKET_SMART_COHORT_EXIT_ENABLED", "bool"),
        "lookback_minutes": ("POLYMARKET_SMART_COHORT_EXIT_LOOKBACK_MINUTES", "int"),
        "min_age_minutes": ("POLYMARKET_SMART_COHORT_EXIT_MIN_AGE_MINUTES", "int"),
        "min_wallets": ("POLYMARKET_SMART_COHORT_EXIT_MIN_WALLETS", "int"),
    },
    "deep_fallback": {
        "enabled": ("POLYMARKET_SMART_DEEP_FALLBACK_ENABLED", "bool"),
        "min_copied_usdc": ("POLYMARKET_SMART_DEEP_FALLBACK_MIN_COPIED_USDC", "float"),
        "min_consensus": ("POLYMARKET_SMART_DEEP_FALLBACK_MIN_CONSENSUS", "int"),
    },
    "whale_copy": {
        "enabled": ("POLYMARKET_SMART_WHALE_COPY_ENABLED", "bool"),
        "min_usdc": ("POLYMARKET_SMART_WHALE_MIN_USDC", "float"),
        "lookback_minutes": ("POLYMARKET_SMART_WHALE_LOOKBACK_MINUTES", "int"),
        "max_orders_per_tick": ("POLYMARKET_SMART_WHALE_MAX_ORDERS_PER_TICK", "int"),
        "fetch_limit": ("POLYMARKET_SMART_WHALE_FETCH_LIMIT", "int"),
        "size_mult": ("POLYMARKET_SMART_WHALE_SIZE_MULT", "float"),
    },
    "favorite_dip": {
        "enabled": ("POLYMARKET_SMART_DIP_BUY_ENABLED", "bool"),
        "min_price": ("POLYMARKET_SMART_DIP_MIN_PRICE", "float"),
        "max_price": ("POLYMARKET_SMART_DIP_MAX_PRICE", "float"),
        "reference_min": ("POLYMARKET_SMART_DIP_REFERENCE_MIN", "float"),
        "use_day_change": ("POLYMARKET_SMART_DIP_USE_DAY_CHANGE", "bool"),
        "max_orders_per_tick": ("POLYMARKET_SMART_DIP_MAX_ORDERS_PER_TICK", "int"),
        "size_mult": ("POLYMARKET_SMART_DIP_SIZE_MULT", "float"),
    },
    "window": {
        "expiring_today_only": ("POLYMARKET_SMART_EXPIRING_TODAY_ONLY", "bool"),
        "one_bet_per_game": ("POLYMARKET_SMART_ONE_BET_PER_GAME", "bool"),
        "copy_light_exclusions": ("POLYMARKET_SMART_COPY_LIGHT_EXCLUSIONS", "bool"),
    },
    "reverse_lookup": {
        "enabled": ("POLYMARKET_SMART_REVERSE_LOOKUP_ENABLED", "bool"),
        "max_tokens": ("POLYMARKET_SMART_REVERSE_LOOKUP_MAX_TOKENS", "int"),
        "min_copied_usdc": ("POLYMARKET_SMART_REVERSE_LOOKUP_MIN_COPIED_USDC", "float"),
        "min_liquidity_usd": ("POLYMARKET_SMART_REVERSE_LOOKUP_MIN_LIQUIDITY_USD", "float"),
        "min_volume_usd": ("POLYMARKET_SMART_REVERSE_LOOKUP_MIN_VOLUME_USD", "float"),
    },
    "btc_edge": {
        "enabled": ("POLYMARKET_BTC_EDGE_INTEGRATED", "bool"),
        "per_trade_cap_usd": ("POLYMARKET_BTC_MAX_TRADE_USD", "float"),
        "min_edge_over_market": ("POLYMARKET_BTC_MIN_EDGE", "float"),
        "min_buy_price": ("POLYMARKET_BTC_MIN_BUY_PRICE", "float"),
        "max_buy_price": ("POLYMARKET_BTC_MAX_BUY_PRICE", "float"),
        "max_spread": ("POLYMARKET_BTC_MAX_SPREAD", "float"),
        "min_trade_usd": ("POLYMARKET_BTC_MIN_TRADE_USD", "float"),
        "min_model_probability": ("POLYMARKET_BTC_MIN_MODEL_PROBABILITY", "float"),
        "min_hours_to_close": ("POLYMARKET_BTC_MIN_HOURS_TO_CLOSE", "float"),
        "volatility_days": ("POLYMARKET_BTC_VOLATILITY_DAYS", "int"),
    },
    "noise_fallback": {
        "enabled": ("POLYMARKET_SMART_NOISE_FALLBACK_ENABLED", "bool"),
        "max_trades_per_tick": ("POLYMARKET_SMART_NOISE_FALLBACK_MAX_TRADES_PER_TICK", "int"),
        "stake_usd": ("POLYMARKET_SMART_NOISE_FALLBACK_MAX_TRADE_USD", "float"),
        "cash_pressure_threshold": ("POLYMARKET_SMART_NOISE_FALLBACK_CASH_PRESSURE_PCT", "float"),
        "min_buy_price": ("POLYMARKET_SMART_NOISE_FALLBACK_MIN_BUY_PRICE", "float"),
        "max_buy_price": ("POLYMARKET_SMART_NOISE_FALLBACK_MAX_BUY_PRICE", "float"),
        "max_spread": ("POLYMARKET_SMART_NOISE_FALLBACK_MAX_SPREAD", "float"),
    },
    "auto_tune": {
        "enabled": ("POLYMARKET_SMART_AUTO_TUNE_ENABLED", "bool"),
        "min_closed_trades": ("POLYMARKET_SMART_AUTO_TUNE_MIN_TRADES", "int"),
    },
    "news": {
        "max_hours": ("POLYMARKET_NEWS_MAX_HOURS", "float"),
        "min_hours": ("POLYMARKET_NEWS_MIN_HOURS", "float"),
        "min_price": ("POLYMARKET_NEWS_MIN_PRICE", "float"),
        "max_price": ("POLYMARKET_NEWS_MAX_PRICE", "float"),
        "max_spread": ("POLYMARKET_NEWS_MAX_SPREAD", "float"),
        "max_relative_spread": ("POLYMARKET_NEWS_MAX_RELATIVE_SPREAD", "float"),
        "min_liquidity_usd": ("POLYMARKET_NEWS_MIN_LIQUIDITY_USD", "float"),
        "min_volume_24h_usd": ("POLYMARKET_NEWS_MIN_VOLUME_24H_USD", "float"),
        "require_positive_momentum": ("POLYMARKET_NEWS_REQUIRE_POSITIVE_MOMENTUM", "bool"),
        "min_abs_momentum": ("POLYMARKET_NEWS_MIN_ABS_MOMENTUM", "float"),
        "take_profit_pct": ("POLYMARKET_NEWS_TAKE_PROFIT_PCT", "float"),
        "stop_loss_pct": ("POLYMARKET_NEWS_STOP_LOSS_PCT", "float"),
        "stop_loss_min_age_minutes": ("POLYMARKET_NEWS_STOP_LOSS_MIN_AGE_MINUTES", "int"),
        "near_expiry_min_profit": ("POLYMARKET_NEWS_NEAR_EXPIRY_MIN_PROFIT", "float"),
        "near_expiry_minutes": ("POLYMARKET_NEWS_NEAR_EXPIRY_MINUTES", "int"),
        "resolved_exit_threshold": ("POLYMARKET_NEWS_RESOLVED_EXIT_THRESHOLD", "float"),
        "max_orders_per_tick": ("POLYMARKET_NEWS_MAX_ORDERS_PER_TICK", "int"),
        "stake_usd": ("POLYMARKET_NEWS_STAKE_USD", "float"),
        "scan_limit": ("POLYMARKET_NEWS_SCAN_LIMIT", "int"),
        "cash_floor_pct": ("POLYMARKET_NEWS_CASH_FLOOR_PCT", "float"),
        "partial_tp_fraction": ("POLYMARKET_NEWS_PARTIAL_TP_FRACTION", "float"),
        "trailing_arm_pct": ("POLYMARKET_NEWS_TRAILING_ARM_PCT", "float"),
        "trailing_giveback_pct": ("POLYMARKET_NEWS_TRAILING_GIVEBACK_PCT", "float"),
        "max_stake_usd": ("POLYMARKET_NEWS_MAX_STAKE_USD", "float"),
        "min_stake_usd": ("POLYMARKET_NEWS_MIN_STAKE_USD", "float"),
        "smart_money_boost_enabled": ("POLYMARKET_NEWS_SMART_MONEY_BOOST_ENABLED", "bool"),
        "smart_money_min_flow_usd": ("POLYMARKET_NEWS_SMART_MONEY_MIN_FLOW_USD", "float"),
        "tight_stop_hours": ("POLYMARKET_NEWS_TIGHT_STOP_HOURS", "float"),
        "tight_stop_pct": ("POLYMARKET_NEWS_TIGHT_STOP_PCT", "float"),
        "very_tight_stop_hours": ("POLYMARKET_NEWS_VERY_TIGHT_STOP_HOURS", "float"),
        "very_tight_stop_pct": ("POLYMARKET_NEWS_VERY_TIGHT_STOP_PCT", "float"),
    },
    "edge": {
        "max_hours": ("POLYMARKET_EDGE_MAX_HOURS", "float"),
        "scan_limit": ("POLYMARKET_EDGE_SCAN_LIMIT", "int"),
        "min_liquidity_usd": ("POLYMARKET_EDGE_MIN_LIQUIDITY_USD", "float"),
        "min_volume_24h_usd": ("POLYMARKET_EDGE_MIN_VOLUME_24H_USD", "float"),
        "min_price": ("POLYMARKET_EDGE_MIN_PRICE", "float"),
        "max_price": ("POLYMARKET_EDGE_MAX_PRICE", "float"),
        "max_spread": ("POLYMARKET_EDGE_MAX_SPREAD", "float"),
        "fee_pct": ("POLYMARKET_EDGE_FEE_PCT", "float"),
        "min_edge_pct": ("POLYMARKET_EDGE_MIN_EDGE_PCT", "float"),
        "kelly_fraction": ("POLYMARKET_EDGE_KELLY_FRACTION", "float"),
        "max_position_pct": ("POLYMARKET_EDGE_MAX_POSITION_PCT", "float"),
        "max_orders_per_tick": ("POLYMARKET_EDGE_MAX_ORDERS_PER_TICK", "int"),
        "min_stake_usd": ("POLYMARKET_EDGE_MIN_STAKE_USD", "float"),
        "cash_floor_pct": ("POLYMARKET_EDGE_CASH_FLOOR_PCT", "float"),
        "daily_drawdown_pct": ("POLYMARKET_EDGE_DAILY_DRAWDOWN_PCT", "float"),
        "take_profit_pct": ("POLYMARKET_EDGE_TAKE_PROFIT_PCT", "float"),
        "stop_loss_pct": ("POLYMARKET_EDGE_STOP_LOSS_PCT", "float"),
        "stop_loss_min_age_minutes": ("POLYMARKET_EDGE_STOP_LOSS_MIN_AGE_MINUTES", "int"),
        "tight_stop_hours": ("POLYMARKET_EDGE_TIGHT_STOP_HOURS", "float"),
        "tight_stop_pct": ("POLYMARKET_EDGE_TIGHT_STOP_PCT", "float"),
        "very_tight_stop_hours": ("POLYMARKET_EDGE_VERY_TIGHT_STOP_HOURS", "float"),
        "very_tight_stop_pct": ("POLYMARKET_EDGE_VERY_TIGHT_STOP_PCT", "float"),
        "near_expiry_minutes": ("POLYMARKET_EDGE_NEAR_EXPIRY_MINUTES", "int"),
        "resolved_exit_threshold": ("POLYMARKET_EDGE_RESOLVED_EXIT_THRESHOLD", "float"),
        "arb_fee_buffer": ("POLYMARKET_EDGE_ARB_FEE_BUFFER", "float"),
        "arb_max_position_pct": ("POLYMARKET_EDGE_ARB_MAX_POSITION_PCT", "float"),
        "crypto_enabled": ("POLYMARKET_EDGE_CRYPTO_ENABLED", "bool"),
        "crypto_direction_enabled": ("POLYMARKET_EDGE_CRYPTO_DIRECTION_ENABLED", "bool"),
        "crypto_annual_vol": ("POLYMARKET_EDGE_CRYPTO_ANNUAL_VOL", "float"),
        "crypto_momentum_alpha": ("POLYMARKET_EDGE_CRYPTO_MOMENTUM_ALPHA", "float"),
        "near_cert_enabled": ("POLYMARKET_EDGE_NEAR_CERT_ENABLED", "bool"),
        "near_cert_max_hours": ("POLYMARKET_EDGE_NEAR_CERT_MAX_HOURS", "float"),
        "near_cert_min_bid": ("POLYMARKET_EDGE_NEAR_CERT_MIN_BID", "float"),
        "near_cert_max_ask": ("POLYMARKET_EDGE_NEAR_CERT_MAX_ASK", "float"),
        "near_cert_bias_multiplier": ("POLYMARKET_EDGE_NEAR_CERT_BIAS_MULTIPLIER", "float"),
        "scalp_enabled": ("POLYMARKET_EDGE_SCALP_ENABLED", "bool"),
        "scalp_min_volume_24h": ("POLYMARKET_EDGE_SCALP_MIN_VOLUME_24H", "float"),
        "scalp_max_position_pct": ("POLYMARKET_EDGE_SCALP_MAX_POSITION_PCT", "float"),
        "scalp_tp_pct": ("POLYMARKET_EDGE_SCALP_TP_PCT", "float"),
        "scalp_sl_pct": ("POLYMARKET_EDGE_SCALP_SL_PCT", "float"),
        "scalp_max_age_minutes": ("POLYMARKET_EDGE_SCALP_MAX_AGE_MINUTES", "int"),
    },
    "race": {
        "max_hours": ("POLYMARKET_RACE_MAX_HOURS", "float"),
        "max_hours_cap": ("POLYMARKET_RACE_MAX_HOURS_CAP", "float"),
        "daily_expiry_fallback": ("POLYMARKET_RACE_DAILY_EXPIRY_FALLBACK", "bool"),
        "scan_limit": ("POLYMARKET_RACE_SCAN_LIMIT", "int"),
        "min_liquidity_usd": ("POLYMARKET_RACE_MIN_LIQUIDITY_USD", "float"),
        "min_volume_24h_usd": ("POLYMARKET_RACE_MIN_VOLUME_24H_USD", "float"),
        "min_price": ("POLYMARKET_RACE_MIN_PRICE", "float"),
        "max_price": ("POLYMARKET_RACE_MAX_PRICE", "float"),
        "max_spread": ("POLYMARKET_RACE_MAX_SPREAD", "float"),
        "stake_usd": ("POLYMARKET_RACE_STAKE_USD", "float"),
        "stake_pct": ("POLYMARKET_RACE_STAKE_PCT", "float"),
        "fixed_stake_usd": ("POLYMARKET_RACE_FIXED_STAKE_USD", "float"),
        "full_deploy": ("POLYMARKET_RACE_FULL_DEPLOY", "bool"),
        "full_deploy_max_position_pct": ("POLYMARKET_RACE_FULL_DEPLOY_MAX_POSITION_PCT", "float"),
        "full_deploy_redistribute_max_position_pct": ("POLYMARKET_RACE_FULL_DEPLOY_REDISTRIBUTE_MAX_POSITION_PCT", "float"),
        "full_deploy_redistribute_min_lines": ("POLYMARKET_RACE_FULL_DEPLOY_REDISTRIBUTE_MIN_LINES", "int"),
        "max_price_hard_cap": ("POLYMARKET_RACE_MAX_PRICE_HARD_CAP", "float"),
        "crypto_min_price": ("POLYMARKET_RACE_CRYPTO_MIN_PRICE", "float"),
        "unban_all_markets": ("POLYMARKET_UNBAN_ALL_MARKETS", "bool"),
        "weather_only": ("POLYMARKET_RACE_WEATHER_ONLY", "bool"),
        "weather_forecast_min_edge": ("POLYMARKET_RACE_WEATHER_FORECAST_MIN_EDGE", "float"),
        "weather_min_bracket_margin_c": ("POLYMARKET_RACE_WEATHER_MIN_BRACKET_MARGIN_C", "float"),
        "category_min_samples": ("POLYMARKET_RACE_CATEGORY_MIN_SAMPLES", "int"),
        "category_disable_roi": ("POLYMARKET_RACE_CATEGORY_DISABLE_ROI", "float"),
        "min_edge": ("POLYMARKET_RACE_MIN_EDGE", "float"),
        "min_quality_score": ("POLYMARKET_RACE_MIN_QUALITY_SCORE", "float"),
        "min_resolution_clarity": ("POLYMARKET_RACE_MIN_RESOLUTION_CLARITY", "float"),
        "forecast_prior": ("POLYMARKET_RACE_FORECAST_PRIOR", "float"),
        "forecast_pseudo_count": ("POLYMARKET_RACE_FORECAST_PSEUDO_COUNT", "float"),
        "preferred_volume_usd": ("POLYMARKET_RACE_PREFERRED_VOLUME_USD", "float"),
        "promotion_min_trades": ("POLYMARKET_RACE_PROMOTION_MIN_TRADES", "int"),
        "promotion_min_roi": ("POLYMARKET_RACE_PROMOTION_MIN_ROI", "float"),
        "initial_stake_pct": ("POLYMARKET_RACE_INITIAL_STAKE_PCT", "float"),
        "double_down_enabled": ("POLYMARKET_RACE_DOUBLE_DOWN_ENABLED", "bool"),
        "double_down_min_dip": ("POLYMARKET_RACE_DOUBLE_DOWN_MIN_DIP", "float"),
        "double_down_max_dip": ("POLYMARKET_RACE_DOUBLE_DOWN_MAX_DIP", "float"),
        "double_down_min_price": ("POLYMARKET_RACE_DOUBLE_DOWN_MIN_PRICE", "float"),
        "max_orders_per_tick": ("POLYMARKET_RACE_MAX_ORDERS_PER_TICK", "int"),
        "cash_floor_pct": ("POLYMARKET_RACE_CASH_FLOOR_PCT", "float"),
        "tp_pct": ("POLYMARKET_RACE_TP_PCT", "float"),
        "sl_pct": ("POLYMARKET_RACE_SL_PCT", "float"),
        "sl_confirm_ticks": ("POLYMARKET_RACE_SL_CONFIRM_TICKS", "int"),
        "sl_min_age_minutes": ("POLYMARKET_RACE_SL_MIN_AGE_MINUTES", "int"),
        "sl_min_exit_price": ("POLYMARKET_RACE_SL_MIN_EXIT_PRICE", "float"),
        "near_expiry_minutes": ("POLYMARKET_RACE_NEAR_EXPIRY_MINUTES", "int"),
        "resolved_exit_threshold": ("POLYMARKET_RACE_RESOLVED_EXIT_THRESHOLD", "float"),
        "min_profit_margin": ("POLYMARKET_RACE_MIN_PROFIT_MARGIN", "float"),
        "contrarian_min_momentum": ("POLYMARKET_RACE_CONTRARIAN_MIN_MOMENTUM", "float"),
        "favorite_min_bid": ("POLYMARKET_RACE_FAVORITE_MIN_BID", "float"),
        "breakout_min_momentum": ("POLYMARKET_RACE_BREAKOUT_MIN_MOMENTUM", "float"),
        "breakout_min_volume": ("POLYMARKET_RACE_BREAKOUT_MIN_VOLUME", "float"),
        "late_favorite_min_bid": ("POLYMARKET_RACE_LATE_FAVORITE_MIN_BID", "float"),
        "late_favorite_max_hours": ("POLYMARKET_RACE_LATE_FAVORITE_MAX_HOURS", "float"),
        "panic_fade_min_move": ("POLYMARKET_RACE_PANIC_FADE_MIN_MOVE", "float"),
        "panic_fade_min_volume": ("POLYMARKET_RACE_PANIC_FADE_MIN_VOLUME", "float"),
        "underdog_max_ask": ("POLYMARKET_RACE_UNDERDOG_MAX_ASK", "float"),
        "underdog_min_momentum": ("POLYMARKET_RACE_UNDERDOG_MIN_MOMENTUM", "float"),
        "underdog_min_volume": ("POLYMARKET_RACE_UNDERDOG_MIN_VOLUME", "float"),
        "arb_threshold": ("POLYMARKET_RACE_ARB_THRESHOLD", "float"),
        "arb_max_stake_usd": ("POLYMARKET_RACE_ARB_MAX_STAKE_USD", "float"),
        "expiry_grace_min": ("POLYMARKET_RACE_EXPIRY_GRACE_MIN", "int"),
        "limit_sell_trigger": ("POLYMARKET_RACE_LIMIT_SELL_TRIGGER", "float"),
        "limit_sell_price": ("POLYMARKET_RACE_LIMIT_SELL_PRICE", "float"),
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
        "stdout_heartbeat_minutes": ("POLYMARKET_STDOUT_HEARTBEAT_MINUTES", "int"),
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
