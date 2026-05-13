#!/usr/bin/env bash
# Lance le bot en LIVE avec le profil late_favorite.
# Toute la config (sizing, filtres race, exits, telemetry) vit dans
# configs/profiles/late_favorite.toml.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Sync live positions (toggle hors schéma).
export POLYMARKET_SYNC_LIVE_POSITIONS=1

exec uv run pmbot auto-loop --live --profile late_favorite --yes
