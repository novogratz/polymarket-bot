#!/usr/bin/env bash
# Lance le bot en LIVE avec le profil late_momentum_chase.
# Filtre : momentum ≥8% + volume ≥$2k + < 1.5h to close. Sizing : $4/trade,
# 2 trades max/tick. TP +15% / SL -20% / cash floor 10%.
# Toute la config vit dans configs/profiles/late_momentum_chase.toml.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Sync live positions (toggle hors schéma).
export POLYMARKET_SYNC_LIVE_POSITIONS=1

exec uv run pmbot auto-loop --live --profile late_momentum_chase --yes
