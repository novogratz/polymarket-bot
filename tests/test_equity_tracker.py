"""Tests for polymarket_bot.equity_tracker."""

import os
os.environ["POLYMARKET_SKIP_DOTENV"] = "1"

import json
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.equity_tracker import append_equity_point, read_equity_curve


class EquityTrackerTests(unittest.TestCase):
    def test_append_creates_file_with_one_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "equity_curve.jsonl"
            append_equity_point(path, tick=1, cash=70.0, invested=30.0, unrealized=5.0)
            self.assertTrue(path.is_file())
            content = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(content), 1)
            point = json.loads(content[0])
            self.assertEqual(point["tick"], 1)
            self.assertEqual(point["cash"], 70.0)
            self.assertEqual(point["invested"], 30.0)
            self.assertEqual(point["unrealized"], 5.0)
            self.assertEqual(point["equity"], 105.0)
            self.assertIn("ts", point)

    def test_append_multiple_points(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "equity_curve.jsonl"
            for tick in range(1, 4):
                append_equity_point(path, tick=tick, cash=100.0 - tick, invested=tick, unrealized=0.0)
            points = read_equity_curve(path)
            self.assertEqual(len(points), 3)
            self.assertEqual([p["tick"] for p in points], [1, 2, 3])
            self.assertEqual(points[0]["equity"], 100.0)
            self.assertEqual(points[2]["equity"], 100.0)

    def test_read_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "equity_curve.jsonl"
            self.assertEqual(read_equity_curve(path), [])

    def test_read_creates_parent_directory_on_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "nested" / "sub" / "equity_curve.jsonl"
            append_equity_point(nested, tick=1, cash=0.0, invested=0.0, unrealized=0.0)
            self.assertTrue(nested.is_file())


if __name__ == "__main__":
    unittest.main()
