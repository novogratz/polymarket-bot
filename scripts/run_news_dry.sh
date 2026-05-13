#!/usr/bin/env bash
# Lance la stratégie "news" en DRY-RUN (aucun ordre réel envoyé).
#
# Règle dure : seuls les marchés expirant dans moins de 4h sont éligibles.
# Le reste est paramétré dans configs/profiles/news.toml.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Quiet logs by default — supprime pour debug.
export POLYMARKET_QUIET=${POLYMARKET_QUIET:-1}

exec uv run pmbot auto-loop --dry-run --profile news --run news
