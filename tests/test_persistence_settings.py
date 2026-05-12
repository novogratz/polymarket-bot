"""Settings : nouveaux champs persistance lus depuis env vars."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from polymarket_bot.config import Settings


PERSISTENCE_ENV_KEYS = (
    "POLYMARKET_PERSISTENCE_ENABLED",
    "POLYMARKET_PERSISTENCE_CACHE_PATH",
    "POLYMARKET_PERSISTENCE_WINDOW_DAYS",
    "POLYMARKET_PERSISTENCE_CACHE_THRESHOLD",
    "POLYMARKET_PERSISTENCE_INTERSECT_PERIODS",
    "POLYMARKET_PERSISTENCE_INTERSECT_MIN",
)


def _clean_env() -> None:
    for key in PERSISTENCE_ENV_KEYS:
        os.environ.pop(key, None)


class TestPersistenceSettings(unittest.TestCase):
    def test_defaults(self) -> None:
        _clean_env()
        s = Settings()
        self.assertTrue(s.persistence_enabled)
        self.assertEqual(s.persistence_cache_path, Path("data/wallet_history.json"))
        self.assertEqual(s.persistence_window_days, 30)
        self.assertAlmostEqual(s.persistence_cache_threshold, 0.70)
        self.assertEqual(s.persistence_intersect_periods, "WEEK,MONTH,ALL")
        self.assertEqual(s.persistence_intersect_min, 2)

    def test_env_override(self) -> None:
        _clean_env()
        with patch.dict(os.environ, {
            "POLYMARKET_PERSISTENCE_ENABLED": "false",
            "POLYMARKET_PERSISTENCE_WINDOW_DAYS": "14",
            "POLYMARKET_PERSISTENCE_CACHE_THRESHOLD": "0.60",
            "POLYMARKET_PERSISTENCE_INTERSECT_MIN": "3",
            "POLYMARKET_PERSISTENCE_INTERSECT_PERIODS": "MONTH,ALL",
        }):
            s = Settings()
            self.assertFalse(s.persistence_enabled)
            self.assertEqual(s.persistence_window_days, 14)
            self.assertAlmostEqual(s.persistence_cache_threshold, 0.60)
            self.assertEqual(s.persistence_intersect_min, 3)
            self.assertEqual(s.persistence_intersect_periods, "MONTH,ALL")

    def test_custom_path_env(self) -> None:
        _clean_env()
        with patch.dict(os.environ, {
            "POLYMARKET_PERSISTENCE_CACHE_PATH": "/tmp/custom-wallet-history.json",
        }):
            s = Settings()
            self.assertEqual(s.persistence_cache_path, Path("/tmp/custom-wallet-history.json"))


if __name__ == "__main__":
    unittest.main()
