"""Tests for polymarket_bot.decisions_log."""

import os
os.environ["POLYMARKET_SKIP_DOTENV"] = "1"

import json
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.decisions_log import (
    Decision,
    append_decisions,
    read_decisions,
)


class DecisionsLogTests(unittest.TestCase):
    def test_append_one_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            candidates = [
                Decision(market_id="123", outcome="Yes", score=70.0, decision="BUY", stake=18.0),
                Decision(market_id="456", outcome="No", score=50.0, decision="REJECT", reason="spread_too_wide"),
            ]
            append_decisions(path, tick=42, candidates=candidates)
            content = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(content), 1)
            entry = json.loads(content[0])
            self.assertEqual(entry["tick"], 42)
            self.assertEqual(len(entry["candidates"]), 2)
            self.assertEqual(entry["candidates"][0]["decision"], "BUY")
            self.assertEqual(entry["candidates"][1]["reason"], "spread_too_wide")

    def test_append_skips_empty_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            append_decisions(path, tick=1, candidates=[])
            self.assertFalse(path.is_file())

    def test_read_returns_iterator(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            append_decisions(path, tick=1, candidates=[Decision(market_id="a", outcome="Yes", score=1.0, decision="BUY")])
            append_decisions(path, tick=2, candidates=[Decision(market_id="b", outcome="No", score=2.0, decision="REJECT", reason="x")])
            ticks = list(read_decisions(path))
            self.assertEqual(len(ticks), 2)
            self.assertEqual(ticks[0]["tick"], 1)
            self.assertEqual(ticks[1]["candidates"][0]["reason"], "x")

    def test_decision_with_optional_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            decision = Decision(
                market_id="123",
                outcome="Yes",
                score=70.0,
                decision="BUY",
                stake=18.0,
                consensus=3,
                copied_usdc=1240.0,
            )
            append_decisions(path, tick=1, candidates=[decision])
            entry = json.loads(path.read_text(encoding="utf-8").strip())
            cand = entry["candidates"][0]
            self.assertEqual(cand["consensus"], 3)
            self.assertEqual(cand["copied_usdc"], 1240.0)


if __name__ == "__main__":
    unittest.main()
