"""Tests for the equity-curve hook fired at the end of smart_money_once
in dry-run mode."""

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


class _FakePortfolio:
    """Minimal portfolio stand-in mimicking the real Portfolio dataclass."""

    def __init__(self, cash: float, positions: list[dict]):
        self.cash = cash
        self.positions = positions


class AppendDryRunEquityPointTests(unittest.TestCase):
    def test_writes_one_equity_point_and_bumps_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            paths = ensure_run_directory(
                base, "sim1", starting_cash=100.0, profile_source="baseline.toml"
            )
            settings = SimpleNamespace(dry_run=True, state_path=paths.state)
            portfolio = _FakePortfolio(
                cash=70.0,
                positions=[
                    {"status": "open", "stake": 18.0, "unrealized_pnl": 2.5},
                    {"status": "open", "stake": 12.0, "unrealized_pnl": -1.0},
                    # Closed (stake=0) should be excluded.
                    {"status": "open", "stake": 0.0, "unrealized_pnl": 5.0},
                ],
            )

            _append_dry_run_equity_point(settings, portfolio)

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
            settings = SimpleNamespace(dry_run=True, state_path=paths.state)
            portfolio = _FakePortfolio(cash=100.0, positions=[])

            for _ in range(3):
                _append_dry_run_equity_point(settings, portfolio)

            lines = paths.equity_curve.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 3)
            ticks = [json.loads(line)["tick"] for line in lines]
            self.assertEqual(ticks, [1, 2, 3])
            metadata = load_metadata(paths)
            self.assertEqual(metadata.total_ticks, 3)

    def test_no_metadata_file_is_silent_noop(self):
        # If state_path points to a directory without metadata.json, the
        # hook short-circuits without raising and writes nothing.
        with tempfile.TemporaryDirectory() as tmp:
            stray = Path(tmp) / "stray"
            stray.mkdir()
            settings = SimpleNamespace(dry_run=True, state_path=stray / "state.json")
            portfolio = _FakePortfolio(cash=100.0, positions=[])
            _append_dry_run_equity_point(settings, portfolio)
            self.assertFalse((stray / "equity_curve.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
