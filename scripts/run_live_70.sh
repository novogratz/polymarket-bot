#!/usr/bin/env bash
# Lance le bot en LIVE avec le profil multi_signal_consensus.
# Toute la config vit dans configs/profiles/multi_signal_consensus.toml.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Sync live positions (toggle hors schéma).
export POLYMARKET_SYNC_LIVE_POSITIONS=1

exec uv run pmbot auto-loop --live --profile multi_signal_consensus --yes
