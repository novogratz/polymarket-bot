#!/usr/bin/env bash
# Dry-run équivalent de scripts/run_live_70.sh, pour comparer aggressive
# vs baseline en simulation.
#
# Charge le profil aggressive-live.toml et exporte les env vars qui ne
# sont pas (encore) dans le schéma profile. Ces env vars surchargent
# tout ce que apply_profile_to_env() ferait (préservation par défaut).

set -euo pipefail

# Sizing extras (hors schéma)
export POLYMARKET_SMART_HIGH_CONVICTION_BALANCE_FRACTION=0.15
export POLYMARKET_MAX_POSITION_USD=7
export POLYMARKET_SMART_MAX_TRADE_USD=7

# Entry filters extras
export POLYMARKET_SMART_FALLBACK_CONSENSUS=2
export POLYMARKET_SMART_MAX_ENTRY_SLIPPAGE=0.12

# Discovery (reverse-lookup)
export POLYMARKET_SMART_REVERSE_LOOKUP_ENABLED=1
export POLYMARKET_SMART_REVERSE_LOOKUP_MAX_TOKENS=100
export POLYMARKET_SMART_REVERSE_LOOKUP_MIN_COPIED_USDC=50
export POLYMARKET_SMART_REVERSE_LOOKUP_MIN_LIQUIDITY_USD=200
export POLYMARKET_SMART_REVERSE_LOOKUP_MIN_VOLUME_USD=500

# Activity floors
export POLYMARKET_SMART_DEEP_FALLBACK_ENABLED=1
export POLYMARKET_SMART_DEEP_FALLBACK_MIN_COPIED_USDC=25

# Noise fallback prix/spread extras
export POLYMARKET_SMART_NOISE_FALLBACK_MIN_BUY_PRICE=0.20
export POLYMARKET_SMART_NOISE_FALLBACK_MAX_BUY_PRICE=0.80
export POLYMARKET_SMART_NOISE_FALLBACK_MAX_SPREAD=0.04

# Exits extras (cohort + near-expiry)
export POLYMARKET_SMART_COHORT_EXIT_ENABLED=1
export POLYMARKET_SMART_COHORT_EXIT_LOOKBACK_MINUTES=120
export POLYMARKET_SMART_COHORT_EXIT_MIN_AGE_MINUTES=20
export POLYMARKET_SMART_COHORT_EXIT_MIN_WALLETS=2
export POLYMARKET_SMART_EXIT_MINUTES_TO_CLOSE=20
export POLYMARKET_SMART_EXIT_MIN_PROFIT=0.05

# BTC edge extras
export POLYMARKET_BTC_MIN_TRADE_USD=1
export POLYMARKET_BTC_MIN_BUY_PRICE=0.05
export POLYMARKET_BTC_MAX_BUY_PRICE=0.95
export POLYMARKET_BTC_MAX_SPREAD=0.04
export POLYMARKET_BTC_MIN_MODEL_PROBABILITY=0.90
export POLYMARKET_BTC_VOLATILITY_DAYS=7

# Run name par défaut, override par premier argument
RUN_NAME="${1:-aggressive}"

exec uv run pmbot auto-loop --dry-run --run "$RUN_NAME" --profile aggressive-live
