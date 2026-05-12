"""Tests unitaires du FIFO matching de scripts/wallet_history_ytd.py."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from polymarket_bot.smart_money import SmartTrade  # noqa: E402

from wallet_history_ytd import (  # noqa: E402
    aggregate_wallet_stats,
    compute_realized_pnl_fifo,
    compute_unrealized_pnl,
    top_category_for,
)


def _trade(
    *,
    asset: str,
    side: str,
    price: float,
    size: float,
    timestamp: int,
    title: str = "",
    slug: str = "",
    wallet: str = "0xabc",
) -> SmartTrade:
    return SmartTrade(
        wallet=wallet,
        asset=asset,
        side=side,
        price=price,
        size=size,
        usdc_size=price * size,
        timestamp=timestamp,
        title=title,
        outcome="Yes",
        slug=slug,
    )


class TestFifoMatching(unittest.TestCase):
    def test_simple_buy_then_sell_winning(self) -> None:
        trades = [
            _trade(asset="TOK1", side="BUY", price=0.40, size=100.0, timestamp=1_700_000_000),
            _trade(asset="TOK1", side="SELL", price=0.60, size=100.0, timestamp=1_700_000_600),
        ]
        realized, buy_vol, n_matched, n_wins, holds, residual = compute_realized_pnl_fifo(trades)
        self.assertAlmostEqual(realized, 100.0 * (0.60 - 0.40), places=6)
        self.assertAlmostEqual(buy_vol, 40.0, places=6)
        self.assertEqual(n_matched, 1)
        self.assertEqual(n_wins, 1)
        self.assertEqual(len(holds), 1)
        self.assertAlmostEqual(holds[0], 10.0, places=3)  # 600 sec = 10 min
        self.assertEqual(len(residual.get("TOK1", [])), 0)

    def test_partial_fill_leaves_residual_buy(self) -> None:
        trades = [
            _trade(asset="TOK1", side="BUY", price=0.30, size=200.0, timestamp=1_700_000_000),
            _trade(asset="TOK1", side="SELL", price=0.50, size=80.0, timestamp=1_700_000_300),
        ]
        realized, _, n_matched, n_wins, holds, residual = compute_realized_pnl_fifo(trades)
        self.assertAlmostEqual(realized, 80.0 * (0.50 - 0.30), places=6)
        self.assertEqual(n_matched, 1)
        self.assertEqual(n_wins, 1)
        self.assertEqual(len(residual["TOK1"]), 1)
        # 200 - 80 = 120 shares restantes au buy_price 0.30
        buy_price, buy_size, _ts = residual["TOK1"][0]
        self.assertAlmostEqual(buy_price, 0.30, places=6)
        self.assertAlmostEqual(buy_size, 120.0, places=6)
        self.assertEqual(len(holds), 1)

    def test_multiple_buys_consumed_by_single_sell(self) -> None:
        trades = [
            _trade(asset="TOK1", side="BUY", price=0.20, size=50.0, timestamp=1_700_000_000),
            _trade(asset="TOK1", side="BUY", price=0.40, size=50.0, timestamp=1_700_000_120),
            _trade(asset="TOK1", side="SELL", price=0.50, size=100.0, timestamp=1_700_000_600),
        ]
        realized, _, n_matched, n_wins, holds, residual = compute_realized_pnl_fifo(trades)
        expected = 50.0 * (0.50 - 0.20) + 50.0 * (0.50 - 0.40)
        self.assertAlmostEqual(realized, expected, places=6)
        self.assertEqual(n_matched, 1)  # un seul SELL
        self.assertEqual(n_wins, 1)
        self.assertEqual(len(holds), 2)  # un hold-time par BUY consommé
        self.assertEqual(len(residual.get("TOK1", [])), 0)

    def test_losing_sell_does_not_count_as_win(self) -> None:
        trades = [
            _trade(asset="TOK1", side="BUY", price=0.70, size=100.0, timestamp=1_700_000_000),
            _trade(asset="TOK1", side="SELL", price=0.30, size=100.0, timestamp=1_700_000_900),
        ]
        realized, _, n_matched, n_wins, _holds, _ = compute_realized_pnl_fifo(trades)
        self.assertAlmostEqual(realized, 100.0 * (0.30 - 0.70), places=6)
        self.assertEqual(n_matched, 1)
        self.assertEqual(n_wins, 0)

    def test_sell_without_buy_is_ignored(self) -> None:
        trades = [_trade(asset="TOK1", side="SELL", price=0.50, size=100.0, timestamp=1_700_000_000)]
        realized, buy_vol, n_matched, n_wins, holds, residual = compute_realized_pnl_fifo(trades)
        self.assertEqual(realized, 0.0)
        self.assertEqual(buy_vol, 0.0)
        self.assertEqual(n_matched, 0)
        self.assertEqual(n_wins, 0)
        self.assertEqual(holds, [])
        self.assertEqual(residual, {})

    def test_separate_tokens_do_not_cross_match(self) -> None:
        trades = [
            _trade(asset="TOK1", side="BUY", price=0.20, size=100.0, timestamp=1_700_000_000),
            _trade(asset="TOK2", side="SELL", price=0.80, size=100.0, timestamp=1_700_000_120),
        ]
        realized, _, n_matched, _, _, residual = compute_realized_pnl_fifo(trades)
        self.assertEqual(realized, 0.0)
        self.assertEqual(n_matched, 0)
        self.assertEqual(len(residual["TOK1"]), 1)
        self.assertNotIn("TOK2", residual)

    def test_unsorted_input_is_handled_via_timestamp_sort(self) -> None:
        trades = [
            _trade(asset="TOK1", side="SELL", price=0.60, size=100.0, timestamp=1_700_000_600),
            _trade(asset="TOK1", side="BUY", price=0.40, size=100.0, timestamp=1_700_000_000),
        ]
        realized, _, n_matched, n_wins, _, _ = compute_realized_pnl_fifo(trades)
        self.assertAlmostEqual(realized, 20.0, places=6)
        self.assertEqual(n_matched, 1)
        self.assertEqual(n_wins, 1)


class TestUnrealizedPnl(unittest.TestCase):
    def test_uses_cash_pnl_field(self) -> None:
        positions = [
            {"asset": "A", "cashPnl": 12.5},
            {"asset": "B", "cashPnl": -3.0},
        ]
        self.assertAlmostEqual(compute_unrealized_pnl(positions), 9.5, places=6)

    def test_fallback_to_size_times_diff_when_no_cashpnl(self) -> None:
        positions = [{"asset": "A", "size": 100.0, "avgPrice": 0.30, "curPrice": 0.50}]
        self.assertAlmostEqual(compute_unrealized_pnl(positions), 20.0, places=6)

    def test_ignores_malformed_entries(self) -> None:
        positions = [{"asset": "A", "cashPnl": "not-a-number"}, {"asset": "B", "cashPnl": 5.0}]
        self.assertAlmostEqual(compute_unrealized_pnl(positions), 5.0, places=6)


class TestTopCategory(unittest.TestCase):
    def test_picks_most_frequent(self) -> None:
        trades = [
            _trade(asset="A", side="BUY", price=0.5, size=10.0, timestamp=1, title="Trump 2028 election"),
            _trade(asset="B", side="BUY", price=0.5, size=10.0, timestamp=2, title="Senate vote on bill"),
            _trade(asset="C", side="BUY", price=0.5, size=10.0, timestamp=3, title="NBA finals winner"),
        ]
        self.assertEqual(top_category_for(trades), "POLITICS")

    def test_empty_list_returns_other(self) -> None:
        self.assertEqual(top_category_for([]), "OTHER")


class TestAggregateWalletStats(unittest.TestCase):
    def test_full_pipeline_row(self) -> None:
        trades = [
            _trade(asset="TOK1", side="BUY", price=0.40, size=100.0, timestamp=1_700_000_000, title="Election"),
            _trade(asset="TOK1", side="SELL", price=0.60, size=100.0, timestamp=1_700_000_600, title="Election"),
            _trade(asset="TOK2", side="BUY", price=0.50, size=50.0, timestamp=1_700_000_120, title="NBA finals"),
        ]
        positions = [{"cashPnl": 7.5}]
        row = aggregate_wallet_stats(wallet="0xabc", username="WHALE", trades=trades, positions=positions)
        self.assertAlmostEqual(row.pnl_realized, 20.0, places=6)
        self.assertAlmostEqual(row.pnl_unrealized, 7.5, places=6)
        self.assertAlmostEqual(row.pnl_net_ytd, 27.5, places=6)
        self.assertAlmostEqual(row.volume_buy_ytd, 40.0 + 25.0, places=6)
        self.assertEqual(row.n_trades, 3)
        self.assertEqual(row.n_matched, 1)
        self.assertEqual(row.n_winning_trades, 1)
        self.assertAlmostEqual(row.win_rate, 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
