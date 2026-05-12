#!/usr/bin/env bash
# Lance le bot en LIVE avec le profil live-90.
# La config "schéma" (sizing, filtres, exits, BTC, noise_fallback, auto_tune)
# vit dans configs/profiles/live-90.toml. Ce script complète avec les env vars
# hors schéma (discovery, reverse-lookup, crypto cohort, cohort exit, sport
# penalty, etc.) — TODO: les migrer progressivement dans le profil.
#
# Ce script passe --yes : la confirmation interactive est skipée, donc aucun
# besoin de TTY. Pour une exécution sans --yes (auto-loop --live tout court),
# l'opérateur DOIT être attaché à un TTY ; sans cela, prompt_live_confirmation
# refuse et abort proprement (cf. live_confirm.py:48).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# === Env vars hors schéma du profil ===
# (Toutes les variables ici devraient finir dans configs/profiles/live-90.toml
# à terme via une extension du _SCHEMA.)

# Sync live positions (toggle hors schéma).
export POLYMARKET_SYNC_LIVE_POSITIONS=1

# Discovery (catégories + mots-clés + fenêtres temporelles).
export POLYMARKET_SMART_CATEGORIES=OVERALL,FINANCE,ECONOMICS,TECH,POLITICS,SPORTS,CULTURE,WEATHER
export POLYMARKET_SMART_DISCOVERY_KEYWORDS='election,trump,senate,congress,fed,inflation,cpi,unemployment,gdp,weather,rain,snow,hurricane,temperature,box office,movie,earnings,stock,nasdaq'
export POLYMARKET_SMART_TIME_PERIODS=MONTH,ALL

# Reverse-lookup des tokens à fort flux smart-money.
export POLYMARKET_SMART_REVERSE_LOOKUP_ENABLED=1
export POLYMARKET_SMART_REVERSE_LOOKUP_MAX_TOKENS=100
export POLYMARKET_SMART_REVERSE_LOOKUP_MIN_COPIED_USDC=50
export POLYMARKET_SMART_REVERSE_LOOKUP_MIN_LIQUIDITY_USD=200
export POLYMARKET_SMART_REVERSE_LOOKUP_MIN_VOLUME_USD=500

# Cohorte crypto (filtres dédiés).
export POLYMARKET_SMART_ALLOW_CRYPTO=1
export POLYMARKET_SMART_CRYPTO_MIN_BUY_PRICE=0.70
export POLYMARKET_SMART_CRYPTO_MIN_HOURS_TO_CLOSE=2
export POLYMARKET_SMART_CRYPTO_MAX_HOURS_TO_CLOSE=48
export POLYMARKET_SMART_CRYPTO_MIN_COPIED_USDC=1500

# Sizing complémentaire hors schéma.
export POLYMARKET_MAX_POSITION_USD=7
export POLYMARKET_SMART_MAX_TRADE_USD=7
export POLYMARKET_SMART_HIGH_CONVICTION_BALANCE_FRACTION=0.15

# BTC edge — détails fins hors schéma.
export POLYMARKET_BTC_MIN_TRADE_USD=1
export POLYMARKET_BTC_MIN_BUY_PRICE=0.05
export POLYMARKET_BTC_MAX_BUY_PRICE=0.95
export POLYMARKET_BTC_MAX_SPREAD=0.04
export POLYMARKET_BTC_MIN_MODEL_PROBABILITY=0.90
export POLYMARKET_BTC_MIN_HOURS_TO_CLOSE=1.0

# Noise fallback — bornes de prix et spread (hors schéma).
export POLYMARKET_SMART_NOISE_FALLBACK_MIN_BUY_PRICE=0.15
export POLYMARKET_SMART_NOISE_FALLBACK_MAX_BUY_PRICE=0.85
export POLYMARKET_SMART_NOISE_FALLBACK_MAX_SPREAD=0.06

# Passes fallback (consensus relâché + deep fallback).
export POLYMARKET_SMART_FALLBACK_CONSENSUS=2
export POLYMARKET_SMART_DEEP_FALLBACK_ENABLED=1
export POLYMARKET_SMART_DEEP_FALLBACK_MIN_COPIED_USDC=25

# Entrées — bornes additionnelles.
export POLYMARKET_SMART_MIN_TRADE_USD=1
export POLYMARKET_SMART_MAX_ENTRY_SLIPPAGE=0.12

# Pondération catégories et cap sport.
export POLYMARKET_SMART_PRIORITY_CATEGORY_BONUS=8
export POLYMARKET_SMART_SPORTS_SCORE_PENALTY=12
export POLYMARKET_SMART_MAX_SPORTS_POSITIONS=3

# Fenêtres horaires d'éligibilité.
export POLYMARKET_SMART_SOON_HOURS=168
export POLYMARKET_SMART_MIN_HOURS_TO_CLOSE=1
export POLYMARKET_SMART_MAX_HOURS_TO_CLOSE=72

# Cohort exit (sortie active si la cohorte vend).
export POLYMARKET_SMART_COHORT_EXIT_ENABLED=1
export POLYMARKET_SMART_COHORT_EXIT_LOOKBACK_MINUTES=120
export POLYMARKET_SMART_COHORT_EXIT_MIN_AGE_MINUTES=20
export POLYMARKET_SMART_COHORT_EXIT_MIN_WALLETS=2

# Exits supplémentaires (near-expiry positif, résolution).
export POLYMARKET_SMART_EXIT_MINUTES_TO_CLOSE=20
export POLYMARKET_SMART_EXIT_MIN_PROFIT=0.05
export POLYMARKET_SMART_RESOLVED_EXIT_THRESHOLD=0.97

exec uv run pmbot auto-loop --live --profile live-90 --yes
