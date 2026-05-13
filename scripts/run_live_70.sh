#!/usr/bin/env bash
# Lance le bot en LIVE avec le profil edge.
# Toute la config (4 lanes: arb / crypto / near-cert / scalp) vit dans
# configs/profiles/edge.toml.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Sync live positions (toggle hors schéma).
export POLYMARKET_SYNC_LIVE_POSITIONS=1

exec uv run pmbot auto-loop --live --profile edge --yes
