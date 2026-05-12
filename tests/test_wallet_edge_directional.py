"""Tests synthétiques (sans réseau) pour ``scripts/wallet_edge_directional.py``.

On injecte des séries de prix et de trades fabriqués pour valider la logique
pure (calcul d'edge, agrégation, filtrage).
"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from polymarket_bot.smart_money import SmartTrade  # noqa: E402
from scripts.wallet_edge_directional import (  # noqa: E402
    JUMP_FLOOR,
    TRADE_CSV_COLUMNS,
    WALLET_CSV_COLUMNS,
    TradeRow,
    aggregate_wallet,
    compute_trade_edge,
    detect_enclosing_jump,
    filter_buys_ytd,
    nearest_price,
    write_trade_csv,
    write_wallet_csv,
)


def _series_flat(price: float = 0.50, n: int = 60, start_ts: int = 1_700_000_000) -> list[tuple[int, float]]:
    """Série plate de ``n`` points espacés de 60 s."""
    return [(start_ts + i * 60, price) for i in range(n)]


def _series_step(
    *,
    price_lo: float,
    price_hi: float,
    jump_at: int,
    n: int = 60,
    start_ts: int = 1_700_000_000,
) -> list[tuple[int, float]]:
    """Mono-jump : ``price_lo`` jusqu'à l'index ``jump_at``, puis ``price_hi``."""
    out = []
    for i in range(n):
        out.append((start_ts + i * 60, price_lo if i < jump_at else price_hi))
    return out


# ---------------------------------------------------------------------------


class NearestPriceTests(unittest.TestCase):
    def test_returns_closest_point(self) -> None:
        series = [(100, 0.30), (160, 0.40), (220, 0.50)]
        self.assertEqual(nearest_price(series, 165), (160, 0.40))
        self.assertEqual(nearest_price(series, 200), (220, 0.50))

    def test_empty_series_returns_none(self) -> None:
        self.assertIsNone(nearest_price([], 100))


class DetectEnclosingJumpTests(unittest.TestCase):
    def test_jump_in_5min_is_detected(self) -> None:
        """Jump 0.10 en 5 min doit donner un final_move ≥ 0.05."""
        series = _series_step(price_lo=0.30, price_hi=0.40, jump_at=30)
        signed = detect_enclosing_jump(series, window_s=600)
        self.assertGreaterEqual(abs(signed), 0.05)
        self.assertGreater(signed, 0)  # signe positif (montée)

    def test_flat_series_returns_zero(self) -> None:
        signed = detect_enclosing_jump(_series_flat(0.50), window_s=600)
        self.assertEqual(signed, 0.0)

    def test_downward_jump_returns_negative(self) -> None:
        series = _series_step(price_lo=0.60, price_hi=0.40, jump_at=30)
        signed = detect_enclosing_jump(series, window_s=600)
        self.assertLess(signed, 0)


class ComputeTradeEdgeTests(unittest.TestCase):
    def test_skip_when_series_too_short(self) -> None:
        series = [(100, 0.5), (160, 0.5), (220, 0.5)]  # 3 points < 5
        result = compute_trade_edge(side="BUY", ts_trade=160, price_trade=0.5, series=series)
        self.assertIsNone(result)

    def test_no_edge_when_final_move_below_floor(self) -> None:
        """Bougé < 0.05 → edge_jump doit être None (jump non détecté)."""
        # Petit drift de 0.01 sur 60 min → pas de jump 10 min.
        series = [(1_700_000_000 + i * 60, 0.50 + i * 0.0001) for i in range(60)]
        result = compute_trade_edge(side="BUY", ts_trade=1_700_001_800, price_trade=0.50, series=series)
        self.assertIsNotNone(result)
        self.assertIsNone(result["edge_jump"])

    def test_edge_directional_none_when_move_30_below_noise(self) -> None:
        """|move_30| < 0.005 → edge_directional non-mesurable."""
        series = _series_flat(0.5)  # rigoureusement plat
        result = compute_trade_edge(side="BUY", ts_trade=1_700_001_800, price_trade=0.5, series=series)
        self.assertIsNotNone(result)
        self.assertIsNone(result["edge_directional"])

    def test_buy_at_start_of_jump_yields_edge_near_one(self) -> None:
        """BUY juste avant un jump 0.30 → 0.50 — wallet *ahead*, edge_jump ≈ 1.0."""
        # Jump à 30 min après le départ ; trade à t = start + 0 min.
        # On a donc price_at = 0.30, price_+15 = 0.30 (jump à +30), price_+30 = 0.50.
        # final_move = +0.20, move_15 = 0 → edge_jump = 0/0.20 = 0.0
        # Pour avoir edge ≈ 1, il faut que le jump arrive avant +15 min.
        series = _series_step(price_lo=0.30, price_hi=0.50, jump_at=10)
        # Trade à l'index 5 (avant le jump à l'index 10).
        ts_trade = 1_700_000_000 + 5 * 60
        result = compute_trade_edge(side="BUY", ts_trade=ts_trade, price_trade=0.30, series=series)
        self.assertIsNotNone(result)
        self.assertIsNotNone(result["edge_jump"])
        # Le wallet a buy 5 min avant le jump → +15 min capture la totalité du move.
        self.assertGreater(result["edge_jump"], 0.5)

    def test_buy_after_jump_yields_chasing_low_edge(self) -> None:
        """BUY après un jump déjà fait — wallet *chase*, edge_jump ≤ 0."""
        # Jump entre les index 5 et 6. Trade à l'index 30 (bien après le jump).
        # price_at = 0.50, price_+15 = 0.50, move_15 = 0, final_move = 0.20 → edge=0.
        series = _series_step(price_lo=0.30, price_hi=0.50, jump_at=6, n=60)
        ts_trade = 1_700_000_000 + 30 * 60
        result = compute_trade_edge(side="BUY", ts_trade=ts_trade, price_trade=0.50, series=series)
        self.assertIsNotNone(result)
        self.assertIsNotNone(result["edge_jump"])
        # Pas de mouvement post-trade → edge proche de 0, pas > 0.5.
        self.assertLessEqual(result["edge_jump"], 0.5)

    def test_buy_then_retracement_yields_negative_edge_jump(self) -> None:
        """BUY puis le marché part dans l'autre sens → edge_jump < 0."""
        # Construction : prix à 0.50 jusqu'à index 10, puis spike à 0.65 entre
        # 10 et 12, puis retour à 0.50 entre 12 et 14, puis chute à 0.40
        # entre 14 et 16. Trade à l'index 12 (sur le pic).
        # final_move (sur 10 min glissants) = chute 0.65 → 0.40 = -0.25 (signé)
        # price_at (t=12) = 0.65, price_+15 (t=27, plateau bas) = 0.40
        # move_15 = -0.25 (BUY)  → oriented = +1 * -0.25 * (-1 car final<0) = +0.25
        # edge_jump = 0.25 / 0.25 = 1.0... mais on veut négatif.
        # Reconfiguration : BUY pile sur le sommet et le marché chute.
        # final_move signe < 0, move_15 < 0 (chute) avec sign BUY (+1).
        # oriented = +1 * (-0.25) * (-1) = +0.25 → edge = +1
        # Cela représente que le wallet a "bien lu" la magnitude finale...
        # Pour avoir edge négatif il faut que move_15 soit OPPOSÉ au final_move.
        # Ex : BUY pendant la chute, prix remonte un peu mais le mouvement
        # global reste à la baisse.
        prices = (
            [0.30] * 10
            + [0.55, 0.55]  # spike up à l'index 10
            + [0.55] * 18
            + [0.40] * 30   # chute après index 30
        )
        start = 1_700_000_000
        series = [(start + i * 60, p) for i, p in enumerate(prices)]
        # Trade à l'index 25 (sur le plateau haut, AVANT la chute).
        # price_at = 0.55, price_+15 (t = 25+15 = 40) = 0.40 → move_15 = -0.15
        # final_move (10 min glissant max) = +0.25 (montée index 9→11) ou
        # +0.25 (descente index 30→32). En valeur absolue elles sont égales.
        # Le détecteur retient celle rencontrée en premier (index 9→11 : montée).
        # signed = +0.25, sign BUY = +1 → oriented = +1 * (-0.15) * +1 = -0.15
        # edge_jump = -0.15 / 0.25 = -0.6 → négatif ✓
        ts_trade = start + 25 * 60
        result = compute_trade_edge(side="BUY", ts_trade=ts_trade, price_trade=0.55, series=series)
        self.assertIsNotNone(result)
        self.assertIsNotNone(result["edge_jump"])
        self.assertLess(result["edge_jump"], 0)


class FilterBuysYtdTests(unittest.TestCase):
    def _trade(self, ts: int, side: str = "BUY") -> SmartTrade:
        return SmartTrade(
            wallet="0xabc", asset="tok", side=side, price=0.5, size=100, usdc_size=50,
            timestamp=ts, title="", outcome="", slug="",
        )

    def test_sells_are_filtered_out(self) -> None:
        trades = [self._trade(2_000_000_000, "BUY"), self._trade(2_000_000_001, "SELL")]
        kept = filter_buys_ytd(trades, since_ts=0)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].side, "BUY")

    def test_pre_ytd_trades_dropped(self) -> None:
        trades = [
            self._trade(1_500_000_000),  # avant YTD
            self._trade(2_500_000_000),  # après
        ]
        kept = filter_buys_ytd(trades, since_ts=2_000_000_000)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].timestamp, 2_500_000_000)


class AggregateWalletTests(unittest.TestCase):
    def _row(self, *, edge_dir: float | None = 0.5, edge_jump: float | None = None,
             move_15: float | None = 0.05, final_move: float = 0.10) -> TradeRow:
        return TradeRow(
            wallet="0xabc",
            token_id="tok",
            ts_trade=1_700_000_000,
            side="BUY",
            price_trade=0.5,
            price_at_trade=0.5,
            price_5min=0.5,
            price_15min=0.55,
            price_30min=0.6,
            move_15min=move_15,
            move_30min=0.10,
            edge_directional=edge_dir,
            final_move=final_move,
            edge_jump=edge_jump,
            category="OTHER",
            title="",
        )

    def test_pct_ahead_chasing_nojump(self) -> None:
        rows = [
            self._row(edge_jump=0.8, final_move=0.10),    # ahead
            self._row(edge_jump=0.6, final_move=0.10),    # ahead
            self._row(edge_jump=-0.3, final_move=0.10),   # chasing
            self._row(edge_jump=None, final_move=0.0),    # nojump
        ]
        agg = aggregate_wallet(rows, wallet="0xabc", n_buy_total=4, n_skipped=0)
        self.assertEqual(agg.n_trades_analyzed, 4)
        self.assertAlmostEqual(agg.pct_ahead, 50.0, places=1)
        self.assertAlmostEqual(agg.pct_chasing, 25.0, places=1)
        self.assertAlmostEqual(agg.pct_nojump, 25.0, places=1)

    def test_mean_median_move_15min(self) -> None:
        rows = [
            self._row(move_15=0.02),
            self._row(move_15=0.04),
            self._row(move_15=0.06),
        ]
        agg = aggregate_wallet(rows, wallet="0xabc", n_buy_total=3, n_skipped=0)
        self.assertAlmostEqual(agg.mean_move_15min, 0.04, places=4)
        self.assertAlmostEqual(agg.median_move_15min, 0.04, places=4)

    def test_empty_rows_safe_aggregation(self) -> None:
        agg = aggregate_wallet([], wallet="0xabc", n_buy_total=0, n_skipped=0)
        self.assertEqual(agg.n_trades_analyzed, 0)
        self.assertIsNone(agg.mean_edge)
        self.assertIsNone(agg.median_move_15min)
        # Les pourcentages restent finis (base=1 par défense).
        self.assertEqual(agg.pct_ahead, 0.0)


class CsvFormatTests(unittest.TestCase):
    def test_trade_csv_contains_all_columns(self) -> None:
        row = TradeRow(
            wallet="0xabc",
            token_id="tok123",
            ts_trade=1_700_000_000,
            side="BUY",
            price_trade=0.50,
            price_at_trade=0.51,
            price_5min=0.52,
            price_15min=0.55,
            price_30min=0.60,
            move_15min=0.04,
            move_30min=0.09,
            edge_directional=0.44,
            final_move=0.10,
            edge_jump=0.40,
            category="POLITICS",
            title="Trump 2028?",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trades.csv"
            write_trade_csv([row], path)
            with path.open("r", encoding="utf-8") as fh:
                reader = csv.reader(fh)
                header = next(reader)
                first = next(reader)
        self.assertEqual(header, TRADE_CSV_COLUMNS)
        self.assertEqual(first[0], "0xabc")
        self.assertEqual(first[1], "tok123")
        self.assertEqual(first[15], "POLITICS")

    def test_wallet_csv_sorted_by_pct_ahead_desc(self) -> None:
        from scripts.wallet_edge_directional import WalletAggregate

        a = WalletAggregate(
            wallet="0xLOW", n_trades_total_buy=10, n_trades_analyzed=10, n_trades_skipped=0,
            mean_move_15min=0.0, median_move_15min=0.0, mean_edge=0.0, median_edge=0.0,
            pct_ahead=10.0, pct_chasing=20.0, pct_nojump=70.0,
            total_pnl_usd=1_000.0, username="lowahead",
        )
        b = WalletAggregate(
            wallet="0xHIGH", n_trades_total_buy=10, n_trades_analyzed=10, n_trades_skipped=0,
            mean_move_15min=0.0, median_move_15min=0.0, mean_edge=0.0, median_edge=0.0,
            pct_ahead=80.0, pct_chasing=5.0, pct_nojump=15.0,
            total_pnl_usd=2_000.0, username="highahead",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wallets.csv"
            write_wallet_csv([a, b], path)
            with path.open("r", encoding="utf-8") as fh:
                reader = csv.reader(fh)
                header = next(reader)
                rows = list(reader)
        self.assertEqual(header, WALLET_CSV_COLUMNS)
        # HIGH d'abord (pct_ahead=80 > 10).
        self.assertEqual(rows[0][0], "0xHIGH")
        self.assertEqual(rows[1][0], "0xLOW")


if __name__ == "__main__":
    unittest.main()
