"""Tests unitaires pour les helpers de calcul de `scripts/news_reaction.py`.

On utilise des series synthetiques pour verifier latence et pct_at_X. Pas
d'appel reseau ; le script complet s'execute via un test d'integration manuel
(`uv run python scripts/news_reaction.py`).
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.news_reaction import (  # noqa: E402
    MOVE_THRESHOLD,
    latency_to_first_move,
    move_at,
    parse_news_ts,
    pct_of_final,
)


class LatencyTests(unittest.TestCase):
    """Verifie le calcul de la latence du premier move >= 2c."""

    def test_immediate_move(self):
        # Prix monte de 2c entre minute 0 et minute 1 -> latence = 60s.
        grid = [
            (1000, 0.50),
            (1060, 0.52),
            (1120, 0.54),
        ]
        lat = latency_to_first_move(grid, t0_idx=0, threshold=MOVE_THRESHOLD)
        self.assertAlmostEqual(lat, 60.0)

    def test_at_t0_exact_threshold(self):
        # |p[0] - p[0]| = 0, le seuil est franchi au pas suivant.
        grid = [(0, 0.50), (60, 0.50), (120, 0.52)]
        lat = latency_to_first_move(grid, t0_idx=0, threshold=MOVE_THRESHOLD)
        self.assertAlmostEqual(lat, 120.0)

    def test_no_move_within_window(self):
        # Aucun point ne depasse 1.5c d'amplitude -> NaN.
        grid = [(0, 0.50), (60, 0.505), (120, 0.51), (180, 0.515)]
        lat = latency_to_first_move(grid, t0_idx=0, threshold=MOVE_THRESHOLD)
        self.assertTrue(math.isnan(lat))

    def test_negative_move_detected(self):
        # Mouvement de -3c -> seuil |delta| >= 2c franchi.
        grid = [(0, 0.50), (60, 0.49), (120, 0.47)]
        lat = latency_to_first_move(grid, t0_idx=0, threshold=MOVE_THRESHOLD)
        self.assertAlmostEqual(lat, 120.0)

    def test_t0_offset(self):
        # T0 n'est pas en debut de grille.
        grid = [(0, 0.50), (60, 0.50), (120, 0.50), (180, 0.53)]
        lat = latency_to_first_move(grid, t0_idx=2, threshold=MOVE_THRESHOLD)
        self.assertAlmostEqual(lat, 60.0)


class MoveAndPctTests(unittest.TestCase):
    """Verifie move_at et pct_of_final."""

    def test_move_at_exact_minute(self):
        grid = [(0, 0.50), (60, 0.52), (120, 0.55), (180, 0.60)]
        self.assertAlmostEqual(move_at(grid, t0_idx=0, delta_sec=60), 0.02)
        self.assertAlmostEqual(move_at(grid, t0_idx=0, delta_sec=120), 0.05)

    def test_move_at_out_of_range(self):
        grid = [(0, 0.50), (60, 0.52)]
        self.assertTrue(math.isnan(move_at(grid, t0_idx=0, delta_sec=600)))

    def test_pct_basic(self):
        self.assertAlmostEqual(pct_of_final(0.02, 0.10), 0.20)
        self.assertAlmostEqual(pct_of_final(0.10, 0.10), 1.0)
        self.assertAlmostEqual(pct_of_final(0.15, 0.10), 1.5)  # overshoot
        self.assertAlmostEqual(pct_of_final(-0.02, 0.10), -0.20)  # contre-sens

    def test_pct_zero_final(self):
        self.assertTrue(math.isnan(pct_of_final(0.02, 0.0)))
        self.assertTrue(math.isnan(pct_of_final(0.02, 0.0000001)))


class NewsTsTests(unittest.TestCase):
    def test_parse_zulu(self):
        ts = parse_news_ts("2026-04-10T12:30:00Z")
        # 2026-04-10 12:30 UTC : compare via datetime aller-retour.
        from datetime import datetime, timezone
        expected = int(datetime(2026, 4, 10, 12, 30, tzinfo=timezone.utc).timestamp())
        self.assertEqual(ts, expected)

    def test_parse_with_offset(self):
        ts1 = parse_news_ts("2026-04-10T12:30:00+00:00")
        ts2 = parse_news_ts("2026-04-10T12:30:00Z")
        self.assertEqual(ts1, ts2)


if __name__ == "__main__":
    unittest.main()
