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
            persistance_score=0.80,
            qualified=True,
        )
        self.assertEqual(sig.wallet, "0xabc")
        self.assertAlmostEqual(sig.intersect_score, 0.67)
        self.assertAlmostEqual(sig.cache_score, 0.80)
        self.assertAlmostEqual(sig.persistance_score, 0.80)
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


if __name__ == "__main__":
    unittest.main()
