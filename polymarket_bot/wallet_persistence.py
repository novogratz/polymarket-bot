"""Filtre de persistance d'edge sur les wallets smart-money.

Stocke un snapshot quotidien du top MONTH du leaderboard et compute
un score de persistance par croisement (cache + intersection multi-période).
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


_SUPPORTED_VERSION = 1


@dataclass(frozen=True)
class PersistenceSignal:
    """Score de persistance pour un wallet à un instant T."""
    wallet: str
    intersect_score: float
    cache_score: float
    persistance_score: float
    qualified: bool


class WalletHistoryStore:
    """Cache JSON append-only des snapshots quotidiens du leaderboard MONTH.

    Format v1 :
        {"version": 1, "snapshots": [{"date": "YYYY-MM-DD", "wallets": [...]}]}

    - record_snapshot(date, wallets) : no-op si date déjà présente
    - presence_count(wallet, n) : nb de jours dans les n derniers snapshots
    - Purge automatique au-delà de 2 × window_days (garde-fou taille)
    - Écriture atomique via tempfile + os.replace
    """

    def __init__(self, path: Path, window_days: int = 30) -> None:
        self.path = Path(path)
        self.window_days = int(window_days)
        self._max_kept = self.window_days * 2

    def record_snapshot(self, snapshot_date: date, wallets: list[str]) -> bool:
        """Enregistre un snapshot. Retourne True si ajouté, False si idempotent."""
        data = self._load()
        iso = snapshot_date.isoformat()
        snapshots: list[dict[str, Any]] = data.get("snapshots", [])
        if any(s.get("date") == iso for s in snapshots):
            return False
        normalized = sorted({w.lower() for w in wallets if w})
        snapshots.append({"date": iso, "wallets": normalized})
        snapshots.sort(key=lambda s: s["date"])
        if len(snapshots) > self._max_kept:
            snapshots = snapshots[-self._max_kept :]
        self._save({"version": _SUPPORTED_VERSION, "snapshots": snapshots})
        return True

    def presence_count(self, wallet: str, window_days: int) -> int:
        """Nb de snapshots des `window_days` plus récents contenant `wallet`."""
        if window_days <= 0:
            return 0
        target = wallet.lower()
        data = self._load()
        snapshots = data.get("snapshots", [])
        snapshots_sorted = sorted(snapshots, key=lambda s: s["date"])
        window = snapshots_sorted[-window_days:]
        return sum(1 for s in window if target in set(s.get("wallets", [])))

    def snapshot_count(self) -> int:
        return len(self._load().get("snapshots", []))

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": _SUPPORTED_VERSION, "snapshots": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"version": _SUPPORTED_VERSION, "snapshots": []}
        if not isinstance(data, dict) or "snapshots" not in data:
            return {"version": _SUPPORTED_VERSION, "snapshots": []}
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=self.path.name + ".",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
