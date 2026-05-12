# Filtre de persistance d'edge des wallets — plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ajouter un filtre qui restreint la cohorte smart-money aux wallets démontrant une persistance d'edge — soit par intersection multi-période (WEEK ∩ MONTH ∩ ALL_TIME), soit par persistance dans un cache local de snapshots quotidiens du leaderboard.

**Architecture:** Module nouveau `wallet_persistence.py` avec une pure function `compute_persistence` + un I/O store `WalletHistoryStore`. Branché dans `smart_money.fetch_smart_money_data` entre le pré-filtre PnL/Vol/ROI et le fetch des trades. Bypass via env var ou flag CLI pour A/B test.

**Tech Stack:** Python 3.12, dataclasses, JSON (stdlib), `uv` pour gestion deps, `unittest` pour tests. Aucune nouvelle dépendance externe.

**Spec source :** `docs/superpowers/specs/2026-05-11-wallet-persistence-filter-design.md`

**Commande tests projet :** `uv run python -B -m unittest discover -s tests`
**Test d'un module précis :** `uv run python -B -m unittest tests.test_<module> -v`

---

## Task 1 : Module `wallet_persistence.py` — fondations (dataclass + store JSON)

**Files:**
- Create: `polymarket_bot/wallet_persistence.py`
- Create: `tests/test_wallet_persistence.py`

- [ ] **Step 1.1 : Écrire les tests `PersistenceSignal` et `WalletHistoryStore.record_snapshot`**

```python
# tests/test_wallet_persistence.py
"""Tests pour wallet_persistence : PersistenceSignal, WalletHistoryStore."""
from __future__ import annotations

import json
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from polymarket_bot.wallet_persistence import (
    PersistenceSignal,
    WalletHistoryStore,
)


class TestPersistenceSignal(unittest.TestCase):
    def test_dataclass_fields(self) -> None:
        sig = PersistenceSignal(
            wallet="0xabc",
            intersect_score=0.67,
            cache_score=0.80,
            persistence_score=0.80,
            qualified=True,
        )
        self.assertEqual(sig.wallet, "0xabc")
        self.assertAlmostEqual(sig.intersect_score, 0.67)
        self.assertAlmostEqual(sig.cache_score, 0.80)
        self.assertAlmostEqual(sig.persistence_score, 0.80)
        self.assertTrue(sig.qualified)


class TestWalletHistoryStoreRecord(unittest.TestCase):
    def _make_store(self, tmp: str) -> WalletHistoryStore:
        return WalletHistoryStore(Path(tmp) / "history.json", window_days=30)

    def test_record_creates_file(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self._make_store(tmp)
            added = store.record_snapshot(date(2026, 5, 11), ["0xa", "0xb"])
            self.assertTrue(added)
            self.assertTrue((Path(tmp) / "history.json").exists())

    def test_record_idempotent_same_date(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self._make_store(tmp)
            store.record_snapshot(date(2026, 5, 11), ["0xa"])
            added = store.record_snapshot(date(2026, 5, 11), ["0xb"])
            self.assertFalse(added)
            self.assertEqual(store.snapshot_count(), 1)

    def test_record_distinct_dates(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self._make_store(tmp)
            store.record_snapshot(date(2026, 5, 11), ["0xa"])
            store.record_snapshot(date(2026, 5, 12), ["0xa", "0xb"])
            self.assertEqual(store.snapshot_count(), 2)

    def test_record_persists_format_v1(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self._make_store(tmp)
            store.record_snapshot(date(2026, 5, 11), ["0xA"])
            data = json.loads((Path(tmp) / "history.json").read_text())
            self.assertEqual(data["version"], 1)
            self.assertEqual(len(data["snapshots"]), 1)
            self.assertEqual(data["snapshots"][0]["date"], "2026-05-11")
            self.assertEqual(data["snapshots"][0]["wallets"], ["0xa"])  # lowercased


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 1.2 : Lancer les tests pour vérifier qu'ils échouent**

Run: `uv run python -B -m unittest tests.test_wallet_persistence -v`
Expected: `ModuleNotFoundError: No module named 'polymarket_bot.wallet_persistence'`

- [ ] **Step 1.3 : Implémenter le module minimal**

```python
# polymarket_bot/wallet_persistence.py
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
    intersect_score: float    # n/3 où n = nb listes (W,M,A) contenant le wallet
    cache_score: float        # presence_days / window_days (0 en warmup)
    persistence_score: float  # max(intersect_score, cache_score)
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
        # purge ancien au-delà de 2× window
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
        # On prend les N derniers par date
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
```

- [ ] **Step 1.4 : Lancer les tests, ils doivent passer**

Run: `uv run python -B -m unittest tests.test_wallet_persistence -v`
Expected: 4 tests OK.

- [ ] **Step 1.5 : Commit**

```bash
git add polymarket_bot/wallet_persistence.py tests/test_wallet_persistence.py
git commit -m "feat(persistence): module wallet_persistence + WalletHistoryStore"
```

---

## Task 2 : `WalletHistoryStore` — presence_count + purge

**Files:**
- Modify: `tests/test_wallet_persistence.py` (ajout d'une classe de tests)
- Modify: `polymarket_bot/wallet_persistence.py` (déjà implémenté à Task 1, vérification via tests)

- [ ] **Step 2.1 : Ajouter les tests de presence_count et purge**

Ajouter à la fin de `tests/test_wallet_persistence.py` (avant le `if __name__`) :

```python
class TestWalletHistoryStoreCount(unittest.TestCase):
    def _populate(self, store: WalletHistoryStore, wallet: str, days: list[int]) -> None:
        """Enregistre `wallet` pour les jours (offsets négatifs depuis 2026-06-01)."""
        from datetime import timedelta
        anchor = date(2026, 6, 1)
        for offset in sorted(set(days)):
            store.record_snapshot(anchor + timedelta(days=offset), [wallet])

    def test_presence_count_full_window(self) -> None:
        with TemporaryDirectory() as tmp:
            store = WalletHistoryStore(Path(tmp) / "h.json", window_days=10)
            # wallet présent sur 7 jours sur les 10 derniers
            self._populate(store, "0xa", list(range(7)))
            # snapshots 1..7 (en partant de l'anchor)
            self.assertEqual(store.snapshot_count(), 7)
            self.assertEqual(store.presence_count("0xa", 10), 7)
            self.assertEqual(store.presence_count("0xa", 5), 5)  # 5 derniers
            self.assertEqual(store.presence_count("unknown", 10), 0)

    def test_presence_count_case_insensitive(self) -> None:
        with TemporaryDirectory() as tmp:
            store = WalletHistoryStore(Path(tmp) / "h.json", window_days=5)
            store.record_snapshot(date(2026, 5, 11), ["0xABC"])
            self.assertEqual(store.presence_count("0xabc", 5), 1)
            self.assertEqual(store.presence_count("0xABC", 5), 1)

    def test_purge_beyond_2x_window(self) -> None:
        with TemporaryDirectory() as tmp:
            store = WalletHistoryStore(Path(tmp) / "h.json", window_days=3)
            # 10 snapshots > 2*3=6 → seuls les 6 derniers sont gardés
            from datetime import timedelta
            anchor = date(2026, 5, 1)
            for offset in range(10):
                store.record_snapshot(anchor + timedelta(days=offset), ["0xa"])
            self.assertEqual(store.snapshot_count(), 6)

    def test_corrupted_file_safe(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.json"
            path.write_text("not json at all")
            store = WalletHistoryStore(path, window_days=5)
            # n'efface pas mais lit comme vide ; record écrit propre par-dessus
            self.assertEqual(store.snapshot_count(), 0)
            store.record_snapshot(date(2026, 5, 11), ["0xa"])
            self.assertEqual(store.snapshot_count(), 1)
```

- [ ] **Step 2.2 : Lancer les tests, ils doivent tous passer (l'impl est déjà faite)**

Run: `uv run python -B -m unittest tests.test_wallet_persistence -v`
Expected: 8 tests OK (4 ancien + 4 nouveaux).

- [ ] **Step 2.3 : Commit**

```bash
git add tests/test_wallet_persistence.py
git commit -m "test(persistence): presence_count, purge, corruption recovery"
```

---

## Task 3 : Pure function `compute_persistence`

**Files:**
- Modify: `tests/test_wallet_persistence.py`
- Modify: `polymarket_bot/wallet_persistence.py`

- [ ] **Step 3.1 : Ajouter les tests**

Ajouter à `tests/test_wallet_persistence.py` (avant `if __name__`) :

```python
from polymarket_bot.wallet_persistence import compute_persistence


class TestComputePersistence(unittest.TestCase):
    def _call(self, **kw: Any) -> PersistenceSignal:
        defaults = dict(
            wallet="0xa",
            in_week=False, in_month=False, in_all=False,
            cache_presence_days=0,
            snapshot_count_in_store=0,
            window_days=30,
            cache_threshold=0.70,
            intersect_min=2,
        )
        defaults.update(kw)
        return compute_persistence(**defaults)

    def test_intersect_3_of_3_qualified(self) -> None:
        sig = self._call(in_week=True, in_month=True, in_all=True)
        self.assertTrue(sig.qualified)
        self.assertAlmostEqual(sig.intersect_score, 1.0)

    def test_intersect_2_of_3_qualified(self) -> None:
        sig = self._call(in_week=False, in_month=True, in_all=True)
        self.assertTrue(sig.qualified)
        self.assertAlmostEqual(sig.intersect_score, 2 / 3, places=2)

    def test_intersect_1_of_3_not_qualified_no_cache(self) -> None:
        sig = self._call(in_week=False, in_month=True, in_all=False)
        self.assertFalse(sig.qualified)

    def test_cache_path_qualifies(self) -> None:
        sig = self._call(
            cache_presence_days=24, snapshot_count_in_store=30, window_days=30
        )
        # cache_score = 24/30 = 0.80 >= 0.70 → qualified
        self.assertTrue(sig.qualified)
        self.assertAlmostEqual(sig.cache_score, 0.80, places=2)

    def test_cache_boundary_at_threshold(self) -> None:
        # 21/30 = 0.70 exactement → qualifié
        sig = self._call(cache_presence_days=21, snapshot_count_in_store=30, window_days=30)
        self.assertTrue(sig.qualified)

    def test_cache_boundary_below_threshold(self) -> None:
        # 20/30 ≈ 0.667 < 0.70 → non qualifié si intersection nulle
        sig = self._call(cache_presence_days=20, snapshot_count_in_store=30, window_days=30)
        self.assertFalse(sig.qualified)

    def test_warmup_disables_cache(self) -> None:
        # store n'a que 10 snapshots < window_days/2=15 → cache_score forcé à 0
        sig = self._call(
            cache_presence_days=10, snapshot_count_in_store=10, window_days=30
        )
        self.assertAlmostEqual(sig.cache_score, 0.0)
        self.assertFalse(sig.qualified)

    def test_intersect_min_3_requires_all_three(self) -> None:
        sig = self._call(
            in_week=True, in_month=True, in_all=False, intersect_min=3
        )
        self.assertFalse(sig.qualified)
        sig2 = self._call(
            in_week=True, in_month=True, in_all=True, intersect_min=3
        )
        self.assertTrue(sig2.qualified)

    def test_persistence_score_is_max(self) -> None:
        sig = self._call(
            in_week=True, in_month=True, in_all=False,
            cache_presence_days=15, snapshot_count_in_store=30, window_days=30,
        )
        # intersect = 2/3 ≈ 0.667 ; cache = 15/30 = 0.50 → max = 0.667
        self.assertAlmostEqual(sig.persistence_score, 2 / 3, places=2)
```

- [ ] **Step 3.2 : Lancer les tests pour voir qu'ils échouent**

Run: `uv run python -B -m unittest tests.test_wallet_persistence.TestComputePersistence -v`
Expected: ImportError sur `compute_persistence`.

- [ ] **Step 3.3 : Implémenter `compute_persistence` dans `wallet_persistence.py`**

Ajouter à la fin de `polymarket_bot/wallet_persistence.py` :

```python
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
```

- [ ] **Step 3.4 : Lancer les tests, ils doivent passer**

Run: `uv run python -B -m unittest tests.test_wallet_persistence -v`
Expected: 17 tests OK (8 précédents + 9 nouveaux).

- [ ] **Step 3.5 : Commit**

```bash
git add polymarket_bot/wallet_persistence.py tests/test_wallet_persistence.py
git commit -m "feat(persistence): compute_persistence pure function + tests"
```

---

## Task 4 : Settings — nouveaux champs persistance

**Files:**
- Modify: `polymarket_bot/config.py` (ajouter 6 champs dans le dataclass `Settings`)
- Create: `tests/test_persistence_settings.py`

- [ ] **Step 4.1 : Lire le format de Settings pour situer le nouvel ajout**

Run: `grep -n "smart_min_trader_pnl\|smart_min_trader_volume\|smart_min_trader_roi" polymarket_bot/config.py`

Observer la convention : `<champ>: <type> = _<type>_env("POLYMARKET_<NAME>", default)`.

- [ ] **Step 4.2 : Écrire le test des nouvelles env vars**

```python
# tests/test_persistence_settings.py
"""Settings : nouveaux champs persistance lus depuis env vars."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from polymarket_bot.config import Settings


class TestPersistenceSettings(unittest.TestCase):
    def test_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            for key in (
                "POLYMARKET_PERSISTENCE_ENABLED",
                "POLYMARKET_PERSISTENCE_CACHE_PATH",
                "POLYMARKET_PERSISTENCE_WINDOW_DAYS",
                "POLYMARKET_PERSISTENCE_CACHE_THRESHOLD",
                "POLYMARKET_PERSISTENCE_INTERSECT_PERIODS",
                "POLYMARKET_PERSISTENCE_INTERSECT_MIN",
            ):
                os.environ.pop(key, None)
            s = Settings()
            self.assertTrue(s.persistence_enabled)
            self.assertEqual(s.persistence_cache_path, Path("data/wallet_history.json"))
            self.assertEqual(s.persistence_window_days, 30)
            self.assertAlmostEqual(s.persistence_cache_threshold, 0.70)
            self.assertEqual(s.persistence_intersect_periods, "WEEK,MONTH,ALL")
            self.assertEqual(s.persistence_intersect_min, 2)

    def test_env_override(self) -> None:
        with patch.dict(os.environ, {
            "POLYMARKET_PERSISTENCE_ENABLED": "false",
            "POLYMARKET_PERSISTENCE_WINDOW_DAYS": "14",
            "POLYMARKET_PERSISTENCE_CACHE_THRESHOLD": "0.60",
            "POLYMARKET_PERSISTENCE_INTERSECT_MIN": "3",
        }):
            s = Settings()
            self.assertFalse(s.persistence_enabled)
            self.assertEqual(s.persistence_window_days, 14)
            self.assertAlmostEqual(s.persistence_cache_threshold, 0.60)
            self.assertEqual(s.persistence_intersect_min, 3)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4.3 : Lancer les tests pour voir qu'ils échouent**

Run: `uv run python -B -m unittest tests.test_persistence_settings -v`
Expected: AttributeError sur `persistence_enabled`.

- [ ] **Step 4.4 : Ajouter les champs dans `polymarket_bot/config.py`**

**Note dry-run vs live :** explorer comment les autres paths (`data/paper_state.json` vs `data/dry_run_state.json`) sont re-routés selon le mode dans `config.py` ou `dry_run_cli.py`. Probablement via une fonction qui rewrite les paths selon `dry_run=True`. Appliquer la **même logique** au `persistence_cache_path` : si dry-run actif et l'env var `POLYMARKET_PERSISTENCE_CACHE_PATH` n'est pas explicitement définie, retourner `data/dry_run_wallet_history.json` au lieu de `data/wallet_history.json`. Si la mécanique n'existe pas en l'état pour les autres paths, simplement laisser `persistence_cache_path` comme env-var pure et documenter dans le run name handling de `dry_run_runs.py`.

Repérer un bloc cohérent à la fin du dataclass `Settings` (après les derniers `smart_*` ou avant les premiers `dry_run_*` selon convention). Ajouter :

```python
    # Filtre de persistance d'edge sur la cohorte smart-money
    persistence_enabled: bool = _bool_env("POLYMARKET_PERSISTENCE_ENABLED", True)
    persistence_cache_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("POLYMARKET_PERSISTENCE_CACHE_PATH", "data/wallet_history.json")
        )
    )
    persistence_window_days: int = _int_env("POLYMARKET_PERSISTENCE_WINDOW_DAYS", 30)
    persistence_cache_threshold: float = _float_env(
        "POLYMARKET_PERSISTENCE_CACHE_THRESHOLD", 0.70
    )
    persistence_intersect_periods: str = _str_env(
        "POLYMARKET_PERSISTENCE_INTERSECT_PERIODS", "WEEK,MONTH,ALL"
    )
    persistence_intersect_min: int = _int_env("POLYMARKET_PERSISTENCE_INTERSECT_MIN", 2)
```

Adapter les noms des helpers (`_bool_env`, `_int_env`, etc.) à ceux déjà utilisés dans `config.py` (`grep -n "^def _" polymarket_bot/config.py`). Pour `Path`, utiliser `field(default_factory=...)` car on lit `os.environ` au runtime, pas à la définition de la classe.

Si `_str_env` n'existe pas, utiliser `os.environ.get("POLYMARKET_PERSISTENCE_INTERSECT_PERIODS", "WEEK,MONTH,ALL")` directement.

S'assurer que `from pathlib import Path` et `from dataclasses import field` sont déjà importés en haut du fichier ; sinon les ajouter.

- [ ] **Step 4.5 : Lancer les tests, ils doivent passer**

Run: `uv run python -B -m unittest tests.test_persistence_settings -v`
Expected: 2 tests OK.

- [ ] **Step 4.6 : Lancer la suite complète pour vérifier qu'aucun test existant ne casse**

Run: `uv run python -B -m unittest discover -s tests`
Expected: 282 + 2 (nouveaux Task 4) + 17 (nouveaux Tasks 1-3) = 301 tests OK.

- [ ] **Step 4.7 : Commit**

```bash
git add polymarket_bot/config.py tests/test_persistence_settings.py
git commit -m "feat(persistence): Settings env vars (enabled, cache_path, window, threshold, intersect)"
```

---

## Task 5 : Profils TOML — section `persistence`

**Files:**
- Modify: `polymarket_bot/profiles.py` (ajouter section au `_SCHEMA`)
- Modify: `tests/test_profiles.py` (étendre les tests existants)

- [ ] **Step 5.1 : Ajouter un test de chargement de la section persistence**

Ajouter au fichier `tests/test_profiles.py` (à la fin, avant `if __name__`) :

```python
class TestProfilesPersistenceSection(unittest.TestCase):
    def test_persistence_section_recognized(self) -> None:
        toml_content = """
[persistence]
enabled = true
window_days = 14
cache_threshold = 0.65
intersect_periods = "MONTH,ALL"
intersect_min = 1
"""
        import os, tempfile
        from polymarket_bot.profiles import load_profile, apply_profile_to_env

        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write(toml_content)
            path = fh.name
        try:
            profile = load_profile(Path(path))
            # On vérifie que toutes les clés ont bien été parsées dans env vars
            self.assertEqual(profile.values["POLYMARKET_PERSISTENCE_ENABLED"], "true")
            self.assertEqual(profile.values["POLYMARKET_PERSISTENCE_WINDOW_DAYS"], "14")
            self.assertEqual(profile.values["POLYMARKET_PERSISTENCE_CACHE_THRESHOLD"], "0.65")
            self.assertEqual(profile.values["POLYMARKET_PERSISTENCE_INTERSECT_PERIODS"], "MONTH,ALL")
            self.assertEqual(profile.values["POLYMARKET_PERSISTENCE_INTERSECT_MIN"], "1")
        finally:
            os.unlink(path)

    def test_persistence_section_disabled(self) -> None:
        toml_content = """
[persistence]
enabled = false
"""
        import os, tempfile
        from polymarket_bot.profiles import load_profile

        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write(toml_content)
            path = fh.name
        try:
            profile = load_profile(Path(path))
            self.assertEqual(profile.values["POLYMARKET_PERSISTENCE_ENABLED"], "false")
        finally:
            os.unlink(path)
```

(L'import `from pathlib import Path` est probablement déjà en haut du fichier ; vérifier.)

- [ ] **Step 5.2 : Lancer les tests pour voir qu'ils échouent**

Run: `uv run python -B -m unittest tests.test_profiles.TestProfilesPersistenceSection -v`
Expected: échec — la section `persistence` est rejetée par `load_profile` (clé inconnue dans `_SCHEMA`).

- [ ] **Step 5.3 : Étendre `_SCHEMA` dans `polymarket_bot/profiles.py`**

Ajouter une section dans le dict `_SCHEMA` (juste avant `"telemetry"`) :

```python
    "persistence": {
        "enabled": ("POLYMARKET_PERSISTENCE_ENABLED", "bool"),
        "window_days": ("POLYMARKET_PERSISTENCE_WINDOW_DAYS", "int"),
        "cache_threshold": ("POLYMARKET_PERSISTENCE_CACHE_THRESHOLD", "float"),
        "intersect_periods": ("POLYMARKET_PERSISTENCE_INTERSECT_PERIODS", "str"),
        "intersect_min": ("POLYMARKET_PERSISTENCE_INTERSECT_MIN", "int"),
    },
```

- [ ] **Step 5.4 : Lancer les tests, ils doivent passer**

Run: `uv run python -B -m unittest tests.test_profiles -v`
Expected: tests existants + 2 nouveaux OK.

- [ ] **Step 5.5 : Commit**

```bash
git add polymarket_bot/profiles.py tests/test_profiles.py
git commit -m "feat(persistence): section TOML persistence dans _SCHEMA"
```

---

## Task 6 : Refactor `_top_traders` → dict[period, list[SmartTrader]]

**Files:**
- Modify: `polymarket_bot/smart_money.py` (fonction `_top_traders`, signature + appelants)
- Modify: `tests/test_strategy.py` (adapter les tests qui mockent `_top_traders`)

- [ ] **Step 6.1 : Identifier les appelants actuels de `_top_traders`**

Run: `grep -n "_top_traders" polymarket_bot/ tests/ -r`
Identifier où il est appelé et où son résultat est consommé.

- [ ] **Step 6.2 : Écrire un test pour le nouveau format**

Ajouter à `tests/test_smart_money.py` (créer le fichier s'il n'existe pas) :

```python
# tests/test_smart_money_top_traders.py
"""Test du refactor _top_traders → dict[period, list[SmartTrader]]."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from polymarket_bot.config import Settings
from polymarket_bot.smart_money import (
    SmartTrader,
    _top_traders,
)


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._responses: dict[tuple[str, str], list[SmartTrader]] = {}

    def add(self, period: str, category: str, traders: list[SmartTrader]) -> None:
        self._responses[(period, category)] = traders

    def leaderboard(self, *, category: str, time_period: str, limit: int) -> list[SmartTrader]:
        self.calls.append((time_period, category))
        return list(self._responses.get((time_period, category), []))


class TestTopTradersByPeriod(unittest.TestCase):
    def test_returns_dict_per_period(self) -> None:
        client = FakeClient()
        client.add("WEEK", "ALL", [SmartTrader(wallet="0xa", username="A", pnl=100, volume=200, category="ALL")])
        client.add("MONTH", "ALL", [SmartTrader(wallet="0xb", username="B", pnl=500, volume=1000, category="ALL")])
        client.add("ALL", "ALL", [SmartTrader(wallet="0xc", username="C", pnl=2000, volume=5000, category="ALL")])

        settings = Settings(
            smart_time_periods="WEEK,MONTH,ALL",
            smart_categories="ALL",
            smart_leaderboard_limit=10,
            quiet=True,
        )
        out = _top_traders(client, settings)
        self.assertIsInstance(out, dict)
        self.assertEqual(set(out.keys()), {"WEEK", "MONTH", "ALL"})
        self.assertEqual([t.wallet for t in out["WEEK"]], ["0xa"])
        self.assertEqual([t.wallet for t in out["MONTH"]], ["0xb"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 6.3 : Lancer le test, il doit échouer (signature change)**

Run: `uv run python -B -m unittest tests.test_smart_money_top_traders -v`
Expected: échec — `_top_traders` retourne actuellement `list[SmartTrader]`, pas un dict.

- [ ] **Step 6.4 : Refactor `_top_traders` dans `polymarket_bot/smart_money.py`**

Remplacer le corps de `_top_traders` par :

```python
def _top_traders(client: DataApiClient, settings: Settings) -> dict[str, list[SmartTrader]]:
    """Retourne un dict {period: traders} (au lieu d'une liste dédupée).

    La déduplication par wallet est désormais responsabilité du consommateur,
    qui peut ainsi calculer les croisements multi-période (filtre persistance).
    """
    result: dict[str, list[SmartTrader]] = {}
    categories = _categories(settings)
    periods = _time_periods(settings)
    combos = [(period, category) for period in periods for category in categories]
    for index, (period, category) in enumerate(combos, 1):
        if not settings.quiet:
            print(f"      leaderboard {index}/{len(combos)} {period}/{category}...", flush=True)
        try:
            category_traders = client.leaderboard(
                category=category,
                time_period=period,
                limit=settings.smart_leaderboard_limit,
            )
        except Exception as exc:
            print(
                f"⚠️  Smart-money leaderboard skipped: {period}/{category} {type(exc).__name__}: {exc}",
                flush=True,
            )
            continue
        bucket = result.setdefault(period, [])
        seen = {t.wallet.lower() for t in bucket}
        added = 0
        for trader in category_traders:
            key = trader.wallet.lower()
            if key in seen:
                continue
            seen.add(key)
            bucket.append(trader)
            added += 1
        if not settings.quiet:
            print(f"         +{added} new in {period} (period total {len(bucket)})", flush=True)
    return result
```

- [ ] **Step 6.5 : Adapter `fetch_smart_money_data` pour consommer le dict**

Dans la même fonction, remplacer :

```python
        traders = _top_traders(client, settings)
```

par :

```python
        traders_by_period = _top_traders(client, settings)
        # Liste plate dédupée pour le pipeline existant (compat ascendante)
        seen_wallets: set[str] = set()
        traders: list[SmartTrader] = []
        for period_traders in traders_by_period.values():
            for t in period_traders:
                key = t.wallet.lower()
                if key in seen_wallets:
                    continue
                seen_wallets.add(key)
                traders.append(t)
```

Stocker `traders_by_period` localement pour qu'il soit accessible plus tard (Task 7).

- [ ] **Step 6.6 : Lancer toute la suite pour vérifier qu'on n'a rien cassé**

Run: `uv run python -B -m unittest discover -s tests`
Expected: tous les tests OK (le comportement externe de `fetch_smart_money_data` est inchangé pour l'instant).

- [ ] **Step 6.7 : Commit**

```bash
git add polymarket_bot/smart_money.py tests/test_smart_money_top_traders.py
git commit -m "refactor(smart_money): _top_traders retourne dict[period, list]"
```

---

## Task 7 : `filter_cohort_by_persistence` + intégration dans `fetch_smart_money_data`

**Files:**
- Modify: `polymarket_bot/wallet_persistence.py` (ajouter `filter_cohort_by_persistence`)
- Modify: `polymarket_bot/smart_money.py` (brancher le filtre, propager les signals)
- Modify: `tests/test_wallet_persistence.py` (test intégration du filtre)

- [ ] **Step 7.1 : Test de `filter_cohort_by_persistence`**

Ajouter à `tests/test_wallet_persistence.py` :

```python
from polymarket_bot.config import Settings
from polymarket_bot.smart_money import SmartTrader
from polymarket_bot.wallet_persistence import filter_cohort_by_persistence


class TestFilterCohort(unittest.TestCase):
    def _trader(self, wallet: str) -> SmartTrader:
        return SmartTrader(wallet=wallet, username=wallet, pnl=1000, volume=5000, category="ALL")

    def _settings(self, **overrides: Any) -> Settings:
        kw: dict[str, Any] = dict(
            persistence_enabled=True,
            persistence_window_days=30,
            persistence_cache_threshold=0.70,
            persistence_intersect_periods="WEEK,MONTH,ALL",
            persistence_intersect_min=2,
            quiet=True,
        )
        kw.update(overrides)
        return Settings(**kw)

    def test_disabled_bypasses_filter(self) -> None:
        with TemporaryDirectory() as tmp:
            store = WalletHistoryStore(Path(tmp) / "h.json")
            traders = [self._trader("0xa"), self._trader("0xb")]
            leaderboards = {"WEEK": set(), "MONTH": {"0xa", "0xb"}, "ALL": set()}
            cohort, signals = filter_cohort_by_persistence(
                traders,
                leaderboards=leaderboards,
                store=store,
                settings=self._settings(persistence_enabled=False),
            )
            self.assertEqual([t.wallet for t in cohort], ["0xa", "0xb"])
            self.assertEqual(signals, {})

    def test_intersect_filters_correctly(self) -> None:
        with TemporaryDirectory() as tmp:
            store = WalletHistoryStore(Path(tmp) / "h.json")
            traders = [self._trader("0xa"), self._trader("0xb"), self._trader("0xc")]
            # 0xa dans 3/3, 0xb dans 1/3, 0xc dans 2/3
            leaderboards = {
                "WEEK": {"0xa", "0xc"},
                "MONTH": {"0xa", "0xb", "0xc"},
                "ALL": {"0xa"},
            }
            cohort, signals = filter_cohort_by_persistence(
                traders,
                leaderboards=leaderboards,
                store=store,
                settings=self._settings(),
            )
            kept = {t.wallet for t in cohort}
            self.assertEqual(kept, {"0xa", "0xc"})
            self.assertTrue(signals["0xa"].qualified)
            self.assertTrue(signals["0xc"].qualified)
            self.assertFalse(signals["0xb"].qualified)

    def test_cache_qualifies_alone(self) -> None:
        with TemporaryDirectory() as tmp:
            store = WalletHistoryStore(Path(tmp) / "h.json", window_days=10)
            # Remplir le cache : 0xa présent dans 8/10 jours, plus que le seuil 0.70
            from datetime import timedelta
            anchor = date(2026, 5, 1)
            for offset in range(10):
                wallets = ["0xa"] if offset < 8 else []
                store.record_snapshot(anchor + timedelta(days=offset), wallets)
            traders = [self._trader("0xa")]
            leaderboards = {"WEEK": set(), "MONTH": set(), "ALL": set()}
            cohort, _ = filter_cohort_by_persistence(
                traders,
                leaderboards=leaderboards,
                store=store,
                settings=self._settings(persistence_window_days=10),
            )
            self.assertEqual(len(cohort), 1)
```

- [ ] **Step 7.2 : Lancer le test pour voir qu'il échoue**

Run: `uv run python -B -m unittest tests.test_wallet_persistence.TestFilterCohort -v`
Expected: ImportError sur `filter_cohort_by_persistence`.

- [ ] **Step 7.3 : Implémenter `filter_cohort_by_persistence`**

Ajouter à la fin de `polymarket_bot/wallet_persistence.py` :

```python
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
    # Normalisation des sets
    norm_lb: dict[str, set[str]] = {
        p: {w.lower() for w in leaderboards.get(p, set())} for p in periods
    }
    if not norm_lb:
        # Mapping vide ou périodes mal configurées : laisser passer pour ne pas tout bloquer
        return list(qualified_traders), {}

    # Période canonique pour le snapshot du jour : on prend MONTH par convention si présent
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
```

- [ ] **Step 7.4 : Lancer les tests du filtre, ils doivent passer**

Run: `uv run python -B -m unittest tests.test_wallet_persistence -v`
Expected: 20+ tests OK.

- [ ] **Step 7.5 : Brancher le filtre dans `fetch_smart_money_data`**

Dans `polymarket_bot/smart_money.py`, modifier `fetch_smart_money_data` :

1. Ajouter en haut du fichier :
```python
from .wallet_persistence import (
    PersistenceSignal,
    WalletHistoryStore,
    filter_cohort_by_persistence,
)
```

2. Étendre le dataclass `SmartMoneyData` pour propager les signals :
```python
@dataclass(frozen=True)
class SmartMoneyData:
    traders: list[SmartTrader]
    trades: list[SmartTrade]
    pnl_by_wallet: dict[str, float]
    traders_used: int
    leaderboard_error: str | None = None
    persistence_signals: dict[str, PersistenceSignal] = field(default_factory=dict)
    cohort_before_persistence: int = 0
    cohort_after_persistence: int = 0
```

Vérifier que `from dataclasses import field` est importé en haut.

3. Dans le corps de `fetch_smart_money_data`, après le calcul de `qualified` (post-filtre PnL/Vol/ROI) et **avant** la boucle de fetch des trades :

```python
    # Filtre persistance d'edge — branché entre pré-filtre PnL/Vol/ROI et fetch trades
    cohort_before = len(qualified)
    persistence_signals: dict[str, PersistenceSignal] = {}
    if settings.persistence_enabled:
        leaderboards_sets: dict[str, set[str]] = {
            period: {t.wallet.lower() for t in period_traders}
            for period, period_traders in traders_by_period.items()
        }
        store = WalletHistoryStore(
            settings.persistence_cache_path,
            window_days=settings.persistence_window_days,
        )
        qualified, persistence_signals = filter_cohort_by_persistence(
            qualified,
            leaderboards=leaderboards_sets,
            store=store,
            settings=settings,
        )
        if not settings.quiet:
            n_cache = sum(1 for s in persistence_signals.values() if s.cache_score >= settings.persistence_cache_threshold)
            n_intersect = sum(1 for s in persistence_signals.values() if s.intersect_score * 3 >= settings.persistence_intersect_min)
            n_both = sum(
                1 for s in persistence_signals.values()
                if s.cache_score >= settings.persistence_cache_threshold
                and s.intersect_score * 3 >= settings.persistence_intersect_min
            )
            print(
                f"      cohort: {cohort_before} → {len(qualified)} "
                f"(persistence: {n_cache} cache, {n_intersect} intersect, {n_both} both)",
                flush=True,
            )
    cohort_after = len(qualified)
```

4. Compléter le `return` final pour passer les nouveaux champs :

```python
    return SmartMoneyData(
        traders=traders,
        trades=trades,
        pnl_by_wallet=pnl_by_wallet,
        traders_used=traders_used,
        persistence_signals=persistence_signals,
        cohort_before_persistence=cohort_before,
        cohort_after_persistence=cohort_after,
    )
```

- [ ] **Step 7.6 : Lancer toute la suite**

Run: `uv run python -B -m unittest discover -s tests`
Expected: tous OK. Si un test existant casse à cause du nouveau dataclass, le corriger en ajoutant les champs avec leurs defaults explicites dans le test (les defaults sont déjà fournis donc normalement pas requis).

- [ ] **Step 7.7 : Commit**

```bash
git add polymarket_bot/wallet_persistence.py polymarket_bot/smart_money.py tests/test_wallet_persistence.py
git commit -m "feat(persistence): filter_cohort_by_persistence + intégration fetch_smart_money_data"
```

---

## Task 8 : Journal des trades — champ `persistence_score`

**Files:**
- Modify: `polymarket_bot/main.py` (fonction `_append_trade_journal` ligne ~1760)
- Modify: la fonction qui crée l'entrée BUY dans le journal
- Modify: `tests/test_strategy.py` (ou créer un test dédié)

- [ ] **Step 8.1 : Identifier où le journal écrit l'entrée BUY**

Run: `grep -n "_append_trade_journal\|journal.*BUY\|trade.*persistence" polymarket_bot/main.py | head -20`
Lire le code aux alentours pour comprendre la structure de l'entrée écrite.

- [ ] **Step 8.2 : Test du champ persistence_score dans le journal**

Créer `tests/test_journal_persistence_score.py` :

```python
"""Trade journal : champ persistence_score propagé."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from polymarket_bot.main import _append_trade_journal
from polymarket_bot.config import Settings


class TestJournalPersistenceScore(unittest.TestCase):
    def test_journal_entry_includes_persistence_score(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.jsonl"
            settings = Settings(trade_journal_path=journal, quiet=True)
            position = {
                "id": "abc",
                "market_id": "m1",
                "side": "yes",
                "size": 10.0,
                "entry_price": 0.40,
                "exit_price": 0.50,
                "pnl_usd": 1.0,
                "persistence_score": 0.83,
                "consensus": 2,
                "copied_usdc": 200.0,
                "tag": "smart_money",
                "opened_at": 1700000000,
                "closed_at": 1700001000,
            }
            _append_trade_journal(settings, position, reason="tp")
            lines = journal.read_text().splitlines()
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[0])
            self.assertIn("persistence_score", entry)
            self.assertAlmostEqual(entry["persistence_score"], 0.83)
```

- [ ] **Step 8.3 : Lancer ce test pour voir s'il passe déjà (selon impl) ou échoue**

Run: `uv run python -B -m unittest tests.test_journal_persistence_score -v`
Si pass : continuer step 8.5. Si fail : voir step 8.4.

- [ ] **Step 8.4 : Étendre `_append_trade_journal` pour propager `persistence_score`**

Dans `polymarket_bot/main.py`, trouver la fonction `_append_trade_journal` (≈ ligne 1760). Dans la construction du dict `entry`, ajouter :

```python
        "persistence_score": float(position.get("persistence_score") or 0.0),
```

Et trouver où la position est créée au moment du BUY (probablement dans une fonction qui place les ordres smart-money). Avant l'ajout au portfolio, calculer le score depuis `SmartMoneyData.persistence_signals` :

```python
    # Récupère le persistence_score max parmi les wallets entry du signal
    entry_wallets = [w.lower() for w in signal.entry_wallets]
    persistence_signals = smart_money_data.persistence_signals
    persistence_score = max(
        (persistence_signals[w].persistence_score for w in entry_wallets if w in persistence_signals),
        default=0.0,
    )
    position["persistence_score"] = persistence_score
```

Adapter le nom de l'attribut `entry_wallets` selon le code réel — explorer `SmartMoneySignal` dans `smart_money.py` pour le bon attribut (probablement `wallets` ou `traders`).

- [ ] **Step 8.5 : Lancer toute la suite**

Run: `uv run python -B -m unittest discover -s tests`
Expected: tout OK.

- [ ] **Step 8.6 : Commit**

```bash
git add polymarket_bot/main.py tests/test_journal_persistence_score.py
git commit -m "feat(persistence): persistence_score propagé au trade journal"
```

---

## Task 9 : CLI flag `--no-persistence`

**Files:**
- Modify: `polymarket_bot/main.py` (commande `auto-loop`)
- Create: `tests/test_cli_no_persistence_flag.py`

- [ ] **Step 9.1 : Identifier la signature actuelle de `auto-loop`**

Run: `grep -n '@app.command\|def auto_loop' polymarket_bot/main.py | head -10`
Lire la signature complète pour situer où injecter le flag.

- [ ] **Step 9.2 : Test du flag**

```python
# tests/test_cli_no_persistence_flag.py
"""CLI : flag --no-persistence force persistence_enabled=False."""
import os
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from polymarket_bot.main import app


class TestNoPersistenceFlag(unittest.TestCase):
    def test_flag_sets_env_var_false(self) -> None:
        runner = CliRunner()
        # On vérifie juste que la commande accepte le flag sans crasher
        # (un dry-run complet n'est pas réaliste ici, on mock le tick)
        with patch("polymarket_bot.main._auto_loop_once") as mocked:
            mocked.return_value = None
            result = runner.invoke(
                app,
                [
                    "auto-loop",
                    "--dry-run",
                    "--no-persistence",
                    "--max-ticks", "0",
                    "--profile", "baseline",
                ],
            )
            # On veut juste que le flag soit accepté
            self.assertIn(result.exit_code, (0,))
            self.assertEqual(os.environ.get("POLYMARKET_PERSISTENCE_ENABLED"), "false")


if __name__ == "__main__":
    unittest.main()
```

NOTE : ce test dépend de l'architecture exacte d'`auto-loop`. Si la mise en place du flag se fait par un mécanisme différent (par ex. modification de `Settings` à la volée), adapter le test pour vérifier ce mécanisme. Vérifier d'abord `_auto_loop_once` existe — sinon mocker la boucle principale (ex: `time.sleep`).

- [ ] **Step 9.3 : Ajouter le flag dans la commande `auto-loop`**

Dans `polymarket_bot/main.py`, trouver la définition de la fonction handler de la commande `auto-loop` (probablement décorée par `@app.command`). Ajouter un paramètre :

```python
    no_persistence: bool = typer.Option(
        False,
        "--no-persistence",
        help="Désactive le filtre de persistance d'edge (pour A/B test).",
    ),
```

Dans le corps, avant que le tick principal lise `Settings()` :

```python
    if no_persistence:
        os.environ["POLYMARKET_PERSISTENCE_ENABLED"] = "false"
```

Vérifier que `import os` est présent en haut du fichier (sinon ajouter).

- [ ] **Step 9.4 : Lancer le test**

Run: `uv run python -B -m unittest tests.test_cli_no_persistence_flag -v`
Expected: OK. Si échec à cause d'arguments manquants à `auto-loop`, ajuster les args minimaux dans le test.

- [ ] **Step 9.5 : Lancer toute la suite**

Run: `uv run python -B -m unittest discover -s tests`

- [ ] **Step 9.6 : Commit**

```bash
git add polymarket_bot/main.py tests/test_cli_no_persistence_flag.py
git commit -m "feat(persistence): CLI flag --no-persistence pour A/B test"
```

---

## Task 10 : Profils existants — activation du filtre

**Files:**
- Modify: `configs/profiles/baseline.toml` (filtre désactivé)
- Modify: `configs/profiles/aggressive.toml`, `aggressive-live.toml`, `live-90.toml` (filtre activé, min=2)
- Modify: `configs/profiles/tight-filters.toml` (filtre activé, min=3)

- [ ] **Step 10.1 : `baseline.toml`** — ajouter à la fin :

```toml
[persistence]
# Filtre persistance désactivé sur baseline : sert de référence A/B.
enabled = false
```

- [ ] **Step 10.2 : `aggressive.toml`, `aggressive-live.toml`, `live-90.toml`** — ajouter à la fin de chaque :

```toml
[persistence]
# Filtre persistance d'edge : élimine les wallets "chanceux" du mois.
# OR entre intersection (≥2 listes sur WEEK/MONTH/ALL) et cache (≥70%
# des 30 derniers snapshots quotidiens).
enabled = true
window_days = 30
cache_threshold = 0.70
intersect_periods = "WEEK,MONTH,ALL"
intersect_min = 2
```

- [ ] **Step 10.3 : `tight-filters.toml`** — ajouter à la fin :

```toml
[persistence]
# Strict : 3/3 sur intersection ET cache à 80%.
enabled = true
window_days = 30
cache_threshold = 0.80
intersect_periods = "WEEK,MONTH,ALL"
intersect_min = 3
```

- [ ] **Step 10.4 : Valider chaque profil charge correctement**

```bash
for p in baseline aggressive aggressive-live live-90 tight-filters; do
    echo "=== $p ==="
    uv run python -c "
from pathlib import Path
from polymarket_bot.profiles import load_profile
prof = load_profile(Path('configs/profiles/$p.toml'))
print('persistence_enabled =', prof.values.get('POLYMARKET_PERSISTENCE_ENABLED', '<unset>'))
"
done
```

Expected: les 5 affichent `true`/`false` selon le profil.

- [ ] **Step 10.5 : Lancer toute la suite**

Run: `uv run python -B -m unittest discover -s tests`

- [ ] **Step 10.6 : Commit**

```bash
git add configs/profiles/
git commit -m "feat(persistence): activation du filtre dans les profils existants"
```

---

## Task 11 : Test d'intégration end-to-end

**Files:**
- Create: `tests/test_persistence_integration.py`

- [ ] **Step 11.1 : Test d'intégration**

```python
# tests/test_persistence_integration.py
"""Intégration : pipeline complet smart_money + filtre persistance.

Vérifie que :
1. Le filtre se branche bien sans casser le pipeline
2. Le SmartMoneyData propage correctement les signals
3. Le bypass (persistence_enabled=False) restaure le comportement antérieur
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from polymarket_bot.config import Settings
from polymarket_bot.smart_money import (
    SmartTrade,
    SmartTrader,
    SmartMoneyData,
    fetch_smart_money_data,
)


class FakeApiClient:
    """Stub DataApiClient pour les tests d'intégration."""
    def __init__(
        self,
        leaderboards: dict[tuple[str, str], list[SmartTrader]],
        trades_by_wallet: dict[str, list[SmartTrade]],
    ) -> None:
        self._leaderboards = leaderboards
        self._trades = trades_by_wallet

    def leaderboard(self, *, category: str, time_period: str, limit: int) -> list[SmartTrader]:
        return list(self._leaderboards.get((time_period, category), []))[:limit]

    def trades(self, *, user: str, start: int, limit: int = 100, side: str | None = "BUY") -> list[SmartTrade]:
        return list(self._trades.get(user.lower(), []))


class TestPersistenceIntegration(unittest.TestCase):
    def _trader(self, wallet: str, pnl: float = 5000, volume: float = 20000) -> SmartTrader:
        return SmartTrader(wallet=wallet, username=wallet, pnl=pnl, volume=volume, category="ALL")

    def test_filter_reduces_cohort(self) -> None:
        with TemporaryDirectory() as tmp:
            # 3 traders ; seul 0xa est dans 3/3, 0xc est dans 2/3
            traders_w = [self._trader("0xa"), self._trader("0xc")]
            traders_m = [self._trader("0xa"), self._trader("0xb"), self._trader("0xc")]
            traders_all = [self._trader("0xa")]
            leaderboards = {
                ("WEEK", "ALL"): traders_w,
                ("MONTH", "ALL"): traders_m,
                ("ALL", "ALL"): traders_all,
            }
            client = FakeApiClient(leaderboards, trades_by_wallet={})
            settings = Settings(
                smart_time_periods="WEEK,MONTH,ALL",
                smart_categories="ALL",
                smart_leaderboard_limit=10,
                persistence_enabled=True,
                persistence_cache_path=Path(tmp) / "history.json",
                persistence_window_days=30,
                persistence_intersect_min=2,
                quiet=True,
                # Désactiver les filtres PnL/Vol/ROI pour ne tester que la persistance
                smart_min_trader_pnl=0.0,
                smart_min_trader_volume=0.0,
                smart_min_trader_roi=0.0,
            )
            data = fetch_smart_money_data(settings, client=client)
            self.assertEqual(data.cohort_before_persistence, 3)
            self.assertEqual(data.cohort_after_persistence, 2)
            self.assertEqual({s.wallet for s in data.persistence_signals.values() if s.qualified}, {"0xa", "0xc"})

    def test_bypass_disabled(self) -> None:
        with TemporaryDirectory() as tmp:
            traders_m = [self._trader("0xa"), self._trader("0xb"), self._trader("0xc")]
            leaderboards = {("MONTH", "ALL"): traders_m}
            client = FakeApiClient(leaderboards, trades_by_wallet={})
            settings = Settings(
                smart_time_periods="MONTH",
                smart_categories="ALL",
                smart_leaderboard_limit=10,
                persistence_enabled=False,
                persistence_cache_path=Path(tmp) / "history.json",
                quiet=True,
                smart_min_trader_pnl=0.0,
                smart_min_trader_volume=0.0,
                smart_min_trader_roi=0.0,
            )
            data = fetch_smart_money_data(settings, client=client)
            # Bypass : pas de signals, cohort intacte
            self.assertEqual(data.persistence_signals, {})
            self.assertEqual(len(data.traders), 3)
```

- [ ] **Step 11.2 : Lancer le test**

Run: `uv run python -B -m unittest tests.test_persistence_integration -v`
Expected: 2 tests OK. Si une signature Settings ne matche pas, ajuster (les valeurs minimales requises peuvent varier selon Task 4).

- [ ] **Step 11.3 : Lancer toute la suite finale**

Run: `uv run python -B -m unittest discover -s tests`
Expected: 0 fail. Comptage attendu : ≥ 305 tests.

- [ ] **Step 11.4 : Commit**

```bash
git add tests/test_persistence_integration.py
git commit -m "test(persistence): test d'intégration end-to-end pipeline + bypass"
```

---

## Task 12 : Validation manuelle dry-run

**Files:**
- Aucun fichier modifié — étape de validation manuelle, pas de tests automatisés.

- [ ] **Step 12.1 : Reset des runs dry-run**

```bash
uv run pmbot dry-run rm smoke --yes
uv run pmbot dry-run rm smoke-nopers --yes
```

- [ ] **Step 12.2 : Lancer un tick de validation avec persistance ON**

```bash
POLYMARKET_QUIET=0 uv run pmbot auto-loop --dry-run --run smoke --profile aggressive --max-ticks 1
```

Vérifier dans la sortie :
- Présence de la ligne `cohort: X → Y (persistence: A cache, B intersect, C both)`
- Création du fichier `data/dry_run_wallet_history.json` (ou nom équivalent)
- Pas de stack trace

Si `--max-ticks` n'existe pas, exécuter sans ce flag et `Ctrl+C` après 1 tick.

- [ ] **Step 12.3 : Lancer un tick avec persistance OFF**

```bash
POLYMARKET_QUIET=0 uv run pmbot auto-loop --dry-run --run smoke-nopers --no-persistence --profile aggressive --max-ticks 1
```

Vérifier :
- **Pas** de ligne `cohort: X → Y (persistence: …)`
- Comportement identique au pré-existant

- [ ] **Step 12.4 : Inspecter le fichier cache créé**

```bash
cat data/dry_run_wallet_history.json | python -m json.tool | head -20
```

Vérifier :
- `version: 1`
- 1 snapshot avec la date du jour
- Liste de wallets non vide

- [ ] **Step 12.5 : Commit final (optionnel — pas de code modifié)**

Pas de commit nécessaire ; la validation manuelle ne modifie pas le repo. Reporter les observations à l'utilisateur.

---

## Critère de complétion globale

- 282 tests existants → **toujours verts**
- ≥ 25 nouveaux tests sur `wallet_persistence` et intégration
- Filtre fonctionnel dans `fetch_smart_money_data`
- Bypass via flag CLI et env var validé
- Tous les profils TOML mis à jour avec section `[persistence]`
- 1 validation manuelle réussie en dry-run
- Spec ↔ code : tout point de la spec est implémenté ou explicitement reporté

**Pas de PR / merge dans `integration/worktrees` sans autorisation explicite de l'utilisateur.** L'A/B test 14 jours requis avant déploiement live (voir spec section "Validation").
