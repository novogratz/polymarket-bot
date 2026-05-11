"""Tests pour wallet_persistence : PersistenceSignal, WalletHistoryStore."""
from __future__ import annotations

import json
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from polymarket_bot.config import Settings
from polymarket_bot.smart_money import SmartTrader
from polymarket_bot.wallet_persistence import (
    PersistenceSignal,
    WalletHistoryStore,
    compute_persistence,
    filter_cohort_by_persistence,
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
            self.assertEqual(data["snapshots"][0]["wallets"], ["0xa"])


class TestWalletHistoryStoreCount(unittest.TestCase):
    def _populate(self, store: WalletHistoryStore, wallet: str, days: list[int]) -> None:
        """Enregistre `wallet` pour les jours (offsets depuis 2026-06-01)."""
        from datetime import timedelta
        anchor = date(2026, 6, 1)
        for offset in sorted(set(days)):
            store.record_snapshot(anchor + timedelta(days=offset), [wallet])

    def test_presence_count_full_window(self) -> None:
        with TemporaryDirectory() as tmp:
            store = WalletHistoryStore(Path(tmp) / "h.json", window_days=10)
            # wallet présent sur 7 jours sur les 10 derniers
            self._populate(store, "0xa", list(range(7)))
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
        self.assertTrue(sig.qualified)
        self.assertAlmostEqual(sig.cache_score, 0.80, places=2)

    def test_cache_boundary_at_threshold(self) -> None:
        sig = self._call(cache_presence_days=21, snapshot_count_in_store=30, window_days=30)
        self.assertTrue(sig.qualified)

    def test_cache_boundary_below_threshold(self) -> None:
        sig = self._call(cache_presence_days=20, snapshot_count_in_store=30, window_days=30)
        self.assertFalse(sig.qualified)

    def test_warmup_disables_cache(self) -> None:
        sig = self._call(
            cache_presence_days=10, snapshot_count_in_store=10, window_days=30
        )
        self.assertAlmostEqual(sig.cache_score, 0.0)
        self.assertFalse(sig.qualified)

    def test_intersect_min_3_requires_all_three(self) -> None:
        sig = self._call(in_week=True, in_month=True, in_all=False, intersect_min=3)
        self.assertFalse(sig.qualified)
        sig2 = self._call(in_week=True, in_month=True, in_all=True, intersect_min=3)
        self.assertTrue(sig2.qualified)

    def test_persistence_score_is_max(self) -> None:
        sig = self._call(
            in_week=True, in_month=True, in_all=False,
            cache_presence_days=15, snapshot_count_in_store=30, window_days=30,
        )
        # intersect = 2/3 ≈ 0.667 ; cache = 15/30 = 0.50 → max = 0.667
        self.assertAlmostEqual(sig.persistence_score, 2 / 3, places=2)


class TestFilterCohort(unittest.TestCase):
    def _trader(self, wallet: str) -> SmartTrader:
        return SmartTrader(wallet=wallet, username=wallet, pnl=1000.0, volume=5000.0, category="ALL")

    def _settings(self, **overrides: Any) -> Settings:
        kw: dict[str, Any] = dict(
            persistence_enabled=True,
            persistence_cache_path=Path("/tmp/never-used.json"),
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
            # 0xa présent dans 8/10 jours → cache_score 0.80 > 0.70 threshold
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


if __name__ == "__main__":
    unittest.main()
