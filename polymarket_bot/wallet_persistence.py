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
    persistence_score: float
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
        self._cache: dict[str, Any] | None = None

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
        if self._cache is not None:
            return self._cache
        if not self.path.exists():
            self._cache = {"version": _SUPPORTED_VERSION, "snapshots": []}
            return self._cache
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self._cache = {"version": _SUPPORTED_VERSION, "snapshots": []}
            return self._cache
        if not isinstance(data, dict) or "snapshots" not in data:
            self._cache = {"version": _SUPPORTED_VERSION, "snapshots": []}
            return self._cache
        self._cache = data
        return self._cache

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
            self._cache = data  # cache après écriture réussie
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def compute_persistence(
    *,
    wallet: str,
    in_week: bool,
    in_month: bool,
    in_all: bool,
    cache_presence_days: int,
    snapshot_count_in_store: int,
    window_days: int,
    cache_threshold: float = 0.70,
    intersect_min: int = 2,
) -> PersistenceSignal:
    """Calcule le PersistenceSignal pour un wallet.

    Règles :
    - intersect_count = nombre de listes (W,M,A) contenant le wallet (0..3)
    - intersect_score = intersect_count / 3
    - cache utilisable seulement si snapshot_count_in_store >= window_days/2
      (sinon cache_score = 0.0 → warmup)
    - cache_score = cache_presence_days / window_days
    - qualified = (intersect_count >= intersect_min) OR (cache_score >= cache_threshold)
    - persistence_score = max(intersect_score, cache_score)
    """
    intersect_count = int(in_week) + int(in_month) + int(in_all)
    intersect_score = intersect_count / 3.0
    if snapshot_count_in_store >= max(1, window_days // 2):
        cache_score = max(0.0, min(1.0, cache_presence_days / max(1, window_days)))
    else:
        cache_score = 0.0
    qualified = (intersect_count >= intersect_min) or (cache_score >= cache_threshold)
    persistence_score = max(intersect_score, cache_score)
    return PersistenceSignal(
        wallet=wallet,
        intersect_score=intersect_score,
        cache_score=cache_score,
        persistence_score=persistence_score,
        qualified=qualified,
    )


def filter_cohort_by_persistence(
    qualified_traders: list[Any],  # list[SmartTrader] — Any pour éviter import circulaire
    *,
    leaderboards: dict[str, set[str]],
    store: WalletHistoryStore,
    settings: Any,  # Settings
) -> tuple[list[Any], dict[str, PersistenceSignal]]:
    """Filtre une cohorte selon les règles de persistance.

    Args:
        qualified_traders : SmartTraders déjà passés par les seuils PnL/Vol/ROI.
        leaderboards : mapping {period: set[wallet]} pour le tick courant.
        store : cache historique des snapshots.
        settings : Settings (lit persistence_*).

    Returns:
        (cohort filtrée, dict {wallet: PersistenceSignal}).
        Si persistence_enabled=False : retourne (traders, {}).
    """
    if not getattr(settings, "persistence_enabled", True):
        return list(qualified_traders), {}

    periods_csv = getattr(settings, "persistence_intersect_periods", "WEEK,MONTH,ALL")
    intersect_min = int(getattr(settings, "persistence_intersect_min", 2))
    window_days = int(getattr(settings, "persistence_window_days", 30))
    cache_threshold = float(getattr(settings, "persistence_cache_threshold", 0.70))

    periods = [p.strip().upper() for p in periods_csv.split(",") if p.strip()]
    norm_lb: dict[str, set[str]] = {
        p: {w.lower() for w in leaderboards.get(p, set())} for p in periods
    }
    if not norm_lb:
        return list(qualified_traders), {}

    # Period canonique pour le snapshot du jour : MONTH si présent, sinon la première
    snapshot_period = "MONTH" if "MONTH" in norm_lb else periods[0]
    store.record_snapshot(date.today(), sorted(norm_lb.get(snapshot_period, set())))

    snapshot_count = store.snapshot_count()

    signals: dict[str, PersistenceSignal] = {}
    kept: list[Any] = []
    for trader in qualified_traders:
        wallet = trader.wallet.lower()
        in_flags = {p: (wallet in norm_lb[p]) for p in periods}
        cache_presence = store.presence_count(wallet, window_days)
        sig = compute_persistence(
            wallet=wallet,
            in_week=in_flags.get("WEEK", False),
            in_month=in_flags.get("MONTH", False),
            in_all=in_flags.get("ALL", False),
            cache_presence_days=cache_presence,
            snapshot_count_in_store=snapshot_count,
            window_days=window_days,
            cache_threshold=cache_threshold,
            intersect_min=intersect_min,
        )
        signals[wallet] = sig
        if sig.qualified:
            kept.append(trader)
    return kept, signals
