"""Tests for the equity-curve hook fired after each successful tick
in dry-run mode (runs for any strategy: smart-money, mirror, …)."""

import os
os.environ["POLYMARKET_SKIP_DOTENV"] = "1"
for _k in [k for k in os.environ if k.startswith("POLYMARKET_") and k != "POLYMARKET_SKIP_DOTENV"]:
    del os.environ[_k]

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from polymarket_bot.dry_run_runs import (
    DryRunPaths,
    ensure_run_directory,
    load_metadata,
)
from polymarket_bot.main import _append_dry_run_equity_point


def _write_state(path: Path, cash: float, positions: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cash": cash, "positions": positions, "pending_orders": []}))


class AppendDryRunEquityPointTests(unittest.TestCase):
    def test_writes_one_equity_point_and_bumps_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            paths = ensure_run_directory(
                base, "sim1", starting_cash=100.0, profile_source="baseline.toml"
            )
            _write_state(
                paths.state,
                cash=70.0,
                positions=[
                    {"status": "open", "stake": 18.0, "unrealized_pnl": 2.5},
                    {"status": "open", "stake": 12.0, "unrealized_pnl": -1.0},
                    {"status": "open", "stake": 0.0, "unrealized_pnl": 5.0},
                ],
            )
            settings = SimpleNamespace(
                dry_run=True, state_path=paths.state, paper_balance_usd=100.0
            )

            _append_dry_run_equity_point(settings)

            self.assertTrue(paths.equity_curve.is_file())
            lines = paths.equity_curve.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            point = json.loads(lines[0])
            self.assertEqual(point["tick"], 1)
            self.assertEqual(point["cash"], 70.0)
            self.assertEqual(point["invested"], 30.0)
            self.assertEqual(point["unrealized"], 1.5)
            self.assertEqual(point["equity"], 101.5)

            metadata = load_metadata(paths)
            self.assertEqual(metadata.total_ticks, 1)
            self.assertIsNotNone(metadata.last_tick_at)

    def test_multiple_calls_increment_tick_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            paths = ensure_run_directory(
                base, "sim2", starting_cash=100.0, profile_source="baseline.toml"
            )
            _write_state(paths.state, cash=100.0, positions=[])
            settings = SimpleNamespace(
                dry_run=True, state_path=paths.state, paper_balance_usd=100.0
            )

            for _ in range(3):
                _append_dry_run_equity_point(settings)

            lines = paths.equity_curve.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 3)
            ticks = [json.loads(line)["tick"] for line in lines]
            self.assertEqual(ticks, [1, 2, 3])
            metadata = load_metadata(paths)
            self.assertEqual(metadata.total_ticks, 3)

    def test_no_metadata_file_is_silent_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            stray = Path(tmp) / "stray"
            stray.mkdir()
            settings = SimpleNamespace(
                dry_run=True,
                state_path=stray / "state.json",
                paper_balance_usd=100.0,
            )
            _append_dry_run_equity_point(settings)
            self.assertFalse((stray / "equity_curve.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
