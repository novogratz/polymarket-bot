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


if __name__ == "__main__":
    unittest.main()
