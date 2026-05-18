#!/usr/bin/env bash
# Lance la stratégie "news" en LIVE (ordres réels envoyés à Polymarket).
#
# Règle dure : seuls les marchés expirant dans moins de 4h sont éligibles.
# Sizing : 5$ par trade, max 3 trades/tick, profil dans configs/profiles/news.toml.
#
# Le flag --yes est intentionnel pour automation; en interactif, retire-le
# pour avoir le prompt de confirmation live.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Sync live positions (toggle hors schéma) — récupère ce qui est déjà sur le wallet.
export POLYMARKET_SYNC_LIVE_POSITIONS=1

# Quiet logs par défaut — supprime pour debug.
export POLYMARKET_QUIET=${POLYMARKET_QUIET:-1}

exec uv run pmbot auto-loop --live --profile news --yes
