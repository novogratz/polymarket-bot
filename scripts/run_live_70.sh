#!/usr/bin/env bash
# Lance le bot en LIVE avec le profil orderbook_imbalance.
# Filtre : tight spread (≤3¢) + mid-price (0.25-0.75) + momentum ≥2% + ≤4h.
# Sizing : $5/trade, 3 trades max/tick. TP +8% / SL -15% / cash floor 10%.
# Toute la config vit dans configs/profiles/orderbook_imbalance.toml.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Sync live positions (toggle hors schéma).
export POLYMARKET_SYNC_LIVE_POSITIONS=1

exec uv run pmbot auto-loop --live --profile orderbook_imbalance --yes
