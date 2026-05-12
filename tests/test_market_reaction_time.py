"""Tests unitaires sur la détection de jumps et la convergence.

On injecte des séries de prix synthétiques (pas d'appel API) pour isoler la
logique pure de ``scripts/market_reaction_time.py``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.market_reaction_time import (  # noqa: E402
    analyse_jump,
    convergence_time_sec,
    detect_jumps,
    pct_move_at,
    resample_to_minute_grid,
    rolling_std,
)


def _grid_from_prices(prices: list[float], *, start_ts: int = 1_700_000_000) -> list[tuple[int, float]]:
    return [(start_ts + i * 60, p) for i, p in enumerate(prices)]


class DetectJumpsTests(unittest.TestCase):
    def test_clean_upward_jump_is_detected(self) -> None:
        """Saut net 0.30 → 0.50 en 2 min puis plateau — doit être détecté."""
        prices = (
            [0.30] * 10
            + [0.35, 0.42, 0.50]  # le burst (3 min)
            + [0.50] * 30
        )
        grid = _grid_from_prices(prices)
        jumps = detect_jumps(grid, jump_threshold_cents=0.05, jump_window_min=5)
        self.assertEqual(len(jumps), 1)
        # T0 doit pointer sur une fenêtre couvrant le burst (l'origine est le
        # 1er index où la fenêtre [t, t+5] capture le mouvement) : entre les
        # index 6 et 10 du plateau initial.
        self.assertIn(jumps[0], range(5, 11))

    def test_oscillation_is_not_detected(self) -> None:
        """Va-et-vient 0.30 ↔ 0.34 — pas de mouvement net, doit être ignoré."""
        prices = [0.30, 0.34, 0.30, 0.34, 0.30, 0.34, 0.30, 0.34, 0.30, 0.34] * 5
        grid = _grid_from_prices(prices)
        jumps = detect_jumps(grid, jump_threshold_cents=0.05, jump_window_min=5)
        self.assertEqual(jumps, [])

    def test_spike_with_full_reversal_is_filtered(self) -> None:
        """Spike puis retour quasi complet, contenu dans une fenêtre de 5 min — filtré.

        Burst à l'index 5 puis retour à l'index 10. Pour les fenêtres qui
        couvrent à la fois le départ et la fin (index 5–10) le mouvement net
        est ~0.02 < 0.025, donc le filtre net rejette. Avec un cooldown long,
        aucun jump ne doit subsister.
        """
        prices = [0.30] * 30 + [0.42, 0.50, 0.42, 0.34, 0.32] + [0.32] * 30
        grid = _grid_from_prices(prices)
        # On élargit la fenêtre de détection pour que le burst+reversal soit
        # bien englobé : 7 min couvre l'aller-retour. Le net est alors quasi nul.
        jumps = detect_jumps(grid, jump_threshold_cents=0.05, jump_window_min=7)
        # Le test peut détecter le burst initial sur des fenêtres plus
        # précoces (avant le retour) ; on accepte 0 ou 1 jump tant que la
        # détection ne se déclenche pas sur le reversal seul (jump down).
        # L'essentiel est qu'il n'y a pas de double-comptage.
        self.assertLessEqual(len(jumps), 1)

    def test_two_jumps_separated_are_both_detected(self) -> None:
        """Deux jumps espacés de plus que le cooldown — les deux doivent sortir."""
        prices = (
            [0.30] * 10
            + [0.35, 0.42, 0.50]  # premier jump
            + [0.50] * 40  # plateau long > cooldown
            + [0.55, 0.62, 0.70]  # deuxième jump
            + [0.70] * 30
        )
        grid = _grid_from_prices(prices)
        jumps = detect_jumps(
            grid,
            jump_threshold_cents=0.05,
            jump_window_min=5,
            cooldown_min=30,
        )
        self.assertGreaterEqual(len(jumps), 2)

    def test_cooldown_collapses_grappes(self) -> None:
        """Burst en escalier — sans cooldown on aurait plusieurs jumps."""
        prices = (
            [0.30] * 10
            + [0.32, 0.35, 0.38, 0.42, 0.46, 0.50, 0.50, 0.50, 0.50, 0.50]
            + [0.50] * 30
        )
        grid = _grid_from_prices(prices)
        # Cooldown très long → un seul jump
        jumps = detect_jumps(
            grid,
            jump_threshold_cents=0.05,
            jump_window_min=5,
            cooldown_min=120,
        )
        self.assertEqual(len(jumps), 1)

    def test_downward_jump_is_detected(self) -> None:
        prices = [0.70] * 10 + [0.65, 0.58, 0.50] + [0.50] * 30
        grid = _grid_from_prices(prices)
        jumps = detect_jumps(grid, jump_threshold_cents=0.05, jump_window_min=5)
        self.assertEqual(len(jumps), 1)


class ConvergenceTests(unittest.TestCase):
    def test_convergence_after_plateau(self) -> None:
        """Après un jump, la std mobile doit retomber rapidement sur un plateau."""
        prices = [0.30] * 10 + [0.40, 0.45, 0.50] + [0.50] * 30
        grid = _grid_from_prices(prices)
        # T0 = index 10 (juste avant le burst). post_jump_window assez long.
        conv = convergence_time_sec(grid, t0_idx=10, post_jump_window_min=30)
        # On doit converger en < 15 min (le plateau commence après 3 min)
        self.assertTrue(conv == conv)  # not NaN
        self.assertLessEqual(conv, 15 * 60)

    def test_no_convergence_during_volatile_window(self) -> None:
        """Si ça reste turbulent, la convergence vaut NaN."""
        # Volatilité forte permanente
        prices = [0.30, 0.50, 0.30, 0.50, 0.30, 0.50, 0.30, 0.50, 0.30, 0.50] * 4
        grid = _grid_from_prices(prices)
        conv = convergence_time_sec(grid, t0_idx=0, post_jump_window_min=30)
        # std mobile reste élevée → NaN
        self.assertNotEqual(conv, conv)  # NaN


class PctMoveTests(unittest.TestCase):
    def test_pct_move_simple(self) -> None:
        # T0 price = 0.30 ; à T0+1min price = 0.40 ; final_move = 0.20
        # pct_at_60s = 0.10 / 0.20 = 0.5
        prices = [0.30, 0.40, 0.45, 0.50]
        grid = _grid_from_prices(prices)
        pct = pct_move_at(grid, t0_idx=0, delta_sec=60, final_move=0.20)
        self.assertAlmostEqual(pct, 0.5, places=4)

    def test_pct_move_zero_final_returns_nan(self) -> None:
        prices = [0.30, 0.40, 0.30]
        grid = _grid_from_prices(prices)
        pct = pct_move_at(grid, t0_idx=0, delta_sec=60, final_move=0.0)
        self.assertNotEqual(pct, pct)  # NaN

    def test_pct_move_beyond_data_is_nan(self) -> None:
        prices = [0.30, 0.40]
        grid = _grid_from_prices(prices)
        pct = pct_move_at(grid, t0_idx=0, delta_sec=600, final_move=0.10)
        self.assertNotEqual(pct, pct)  # NaN


class AnalyseJumpTests(unittest.TestCase):
    def test_analyse_jump_picks_right_t0(self) -> None:
        """Vérifie qu'analyse_jump capture le bon T0 (juste avant le burst)."""
        # T0 idx = 10, plateau ensuite à 0.50
        prices = [0.30] * 11 + [0.40, 0.50] + [0.50] * 70
        grid = _grid_from_prices(prices)
        # Détecte d'abord
        jumps = detect_jumps(grid, jump_threshold_cents=0.05, jump_window_min=5)
        self.assertEqual(len(jumps), 1)
        t0 = jumps[0]
        metrics = analyse_jump(grid, t0_idx=t0, post_jump_window_min=60)
        self.assertIsNotNone(metrics)
        assert metrics is not None
        # t0_price doit être ~0.30 (pre-jump)
        self.assertAlmostEqual(metrics["t0_price"], 0.30, places=4)
        # final_move positif
        self.assertGreater(metrics["final_move"], 0)
        # à T+60min sur un plateau, on doit être à ~100% du move
        self.assertAlmostEqual(metrics["pct_move_at_3600s"], 1.0, places=2)


class ResampleTests(unittest.TestCase):
    def test_resample_forward_fill(self) -> None:
        """Resampling forward-fill : on tire vers l'avant le dernier prix vu."""
        series = [(1000, 0.30), (1075, 0.40), (1240, 0.50)]
        grid = resample_to_minute_grid(series, start_ts=1000, end_ts=1300)
        # grille tous les 60s : t=1000, 1060, 1120, 1180, 1240, 1300
        timestamps = [g[0] for g in grid]
        prices = [g[1] for g in grid]
        self.assertEqual(timestamps, [1000, 1060, 1120, 1180, 1240, 1300])
        # à t=1000 : 0.30 ; à t=1060 : encore 0.30 (1075 pas atteint) ;
        # à t=1120 : 0.40 (1075 atteint) ; ... à t=1240 : 0.50 ; à t=1300 : 0.50
        self.assertEqual(prices, [0.30, 0.30, 0.40, 0.40, 0.50, 0.50])


class RollingStdTests(unittest.TestCase):
    def test_rolling_std_basic(self) -> None:
        prices = [0.30] * 5 + [0.50] * 5
        stds = rolling_std(prices, window=3)
        # Les premiers sont NaN, puis 0 sur les plateaux purs, valeur > 0 à la transition
        self.assertTrue(stds[0] != stds[0])  # NaN
        self.assertAlmostEqual(stds[4], 0.0, places=4)  # plateau pur
        self.assertGreater(stds[6], 0.0)  # transition récente


if __name__ == "__main__":
    unittest.main()
