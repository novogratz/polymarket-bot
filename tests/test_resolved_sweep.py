"""Regression tests for the universal winners-only sweep threshold.

2026-06-10: PR #29 raised ``race_resolved_exit_threshold`` 0.97 → 0.99, but
``_force_close_resolved_positions`` kept reading
``smart_resolved_exit_threshold`` (default 0.97) and front-ran the race
resolved-exit — Spurs/Knicks O/U 196.5 was sold at 0.97 instead of riding
to 0.99. The sweep must use the strictest configured threshold.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ["POLYMARKET_SKIP_DOTENV"] = "1"
for _k in [k for k in os.environ if k.startswith("POLYMARKET_") and k != "POLYMARKET_SKIP_DOTENV"]:
    del os.environ[_k]

from polymarket_bot.config import Settings
from polymarket_bot.main import _force_close_resolved_positions


def _state_with_position(tmp: Path, current_price: float) -> Path:
    state = tmp / "paper_state.json"
    state.write_text(json.dumps({
        "cash": 100.0,
        "positions": [{
            "status": "open",
            "opened_at": "2026-06-10T18:57:31+00:00",
            "market_id": "2476455",
            "question": "Spurs vs. Knicks: O/U 196.5",
            "outcome": "Over",
            "token_id": "tok-spurs",
            "entry_price": 0.9099,
            "shares": 393.1868,
            "stake": 357.8,
            "current_price": current_price,
        }],
    }))
    return state


class ResolvedSweepThresholdTests(unittest.TestCase):
    def _settings(self, tmp: Path) -> Settings:
        return Settings(
            dry_run=True,
            state_path=tmp / "paper_state.json",
            trade_journal_path=tmp / "trade_journal.jsonl",
            race_resolved_exit_threshold=0.99,
            # smart_resolved_exit_threshold stays at its 0.97 default,
            # exactly like the live grinder profiles.
        )

    def test_sweep_holds_winner_below_race_threshold(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            _state_with_position(tmp, current_price=0.97)
            closed = _force_close_resolved_positions(self._settings(tmp), "grinder")
            self.assertEqual(closed, [])
            state = json.loads((tmp / "paper_state.json").read_text())
            self.assertEqual(state["positions"][0]["status"], "open")

    def test_sweep_realizes_winner_at_race_threshold(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            _state_with_position(tmp, current_price=0.991)
            closed = _force_close_resolved_positions(self._settings(tmp), "grinder")
            self.assertEqual(len(closed), 1)
            self.assertEqual(closed[0]["exit_reason"], "resolved_market_sweep_win")


if __name__ == "__main__":
    unittest.main()
