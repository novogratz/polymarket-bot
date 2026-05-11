"""Tests for polymarket_bot.dry_run_compare."""

import os
os.environ["POLYMARKET_SKIP_DOTENV"] = "1"

import json
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.dry_run_runs import ensure_run_directory, save_metadata, load_metadata
from polymarket_bot.equity_tracker import append_equity_point
from polymarket_bot.dry_run_compare import (
    RunStats,
    compute_run_stats,
    format_comparison_table,
)


class ComputeRunStatsTests(unittest.TestCase):
    def _make_run(self, base: Path, name: str, starting_cash: float, equity: float, ticks: int):
        paths = ensure_run_directory(base, name, starting_cash=starting_cash, profile_source="x.toml")
        # Write a ledger with cash + 1 position.
        paths.state.write_text(json.dumps({
            "cash": equity * 0.3,
            "positions": [
                {"stake": equity * 0.7, "unrealized_pnl": 0.0, "shares": 100.0, "entry_price": 0.5, "current_price": 0.5}
            ],
            "pending_orders": [],
        }), encoding="utf-8")
        # Write equity curve points.
        for i in range(ticks):
            append_equity_point(paths.equity_curve, tick=i + 1, cash=equity * 0.3, invested=equity * 0.7, unrealized=0.0)
        # Bump total_ticks in metadata to match.
        metadata = load_metadata(paths)
        metadata.total_ticks = ticks
        save_metadata(paths, metadata)
        return paths

    def test_compute_basic(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._make_run(base, "a", 100.0, 110.0, 50)
            stats = compute_run_stats(base, "a")
            self.assertEqual(stats.run_name, "a")
            self.assertEqual(stats.starting_cash, 100.0)
            self.assertEqual(stats.total_ticks, 50)
            self.assertAlmostEqual(stats.equity, 110.0)
            self.assertAlmostEqual(stats.cash, 33.0)  # 110 * 0.3
            self.assertAlmostEqual(stats.invested, 77.0)  # 110 * 0.7
            self.assertAlmostEqual(stats.return_pct, 0.10)

    def test_compute_run_without_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ensure_run_directory(base, "empty", starting_cash=100.0, profile_source="x.toml")
            stats = compute_run_stats(base, "empty")
            self.assertEqual(stats.total_ticks, 0)
            self.assertEqual(stats.equity, 100.0)  # fallback to starting_cash
            self.assertEqual(stats.cash, 100.0)
            self.assertEqual(stats.invested, 0.0)


class FormatComparisonTableTests(unittest.TestCase):
    def test_format_basic(self):
        stats_a = RunStats(
            run_name="a", profile_source="baseline.toml", starting_cash=100.0,
            cash=70.0, invested=40.0, unrealized=5.0, equity=115.0, return_pct=0.15,
            total_ticks=100, started_at="2026-05-10T00:00:00+00:00",
            realized_pnl=10.0, trades_closed=8, win_rate=0.625, max_drawdown=-5.0,
            avg_pnl=1.25,
        )
        stats_b = RunStats(
            run_name="b", profile_source="aggressive.toml", starting_cash=100.0,
            cash=20.0, invested=70.0, unrealized=-2.0, equity=88.0, return_pct=-0.12,
            total_ticks=80, started_at="2026-05-10T00:00:00+00:00",
            realized_pnl=-10.0, trades_closed=12, win_rate=0.333, max_drawdown=-15.0,
            avg_pnl=-0.83,
        )
        text = format_comparison_table([stats_a, stats_b])
        self.assertIn("a", text)
        self.assertIn("b", text)
        self.assertIn("baseline.toml", text)
        self.assertIn("aggressive.toml", text)
        self.assertIn("115", text)  # equity a
        self.assertIn("88", text)   # equity b


if __name__ == "__main__":
    unittest.main()
