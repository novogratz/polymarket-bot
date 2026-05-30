import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
MODULE_PATH = SCRIPTS_DIR / "live_analyst.py"
sys.path.insert(0, str(SCRIPTS_DIR))
SPEC = importlib.util.spec_from_file_location("live_analyst_under_test", MODULE_PATH)
live_analyst = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = live_analyst
SPEC.loader.exec_module(live_analyst)


class LiveAnalystStatsTests(unittest.TestCase):
    def setUp(self):
        self._orig_data_dir = live_analyst.DATA_DIR

    def tearDown(self):
        live_analyst.DATA_DIR = self._orig_data_dir

    def test_live_snapshot_counts_realized_pnl_rows_and_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            live_analyst.DATA_DIR = data_dir
            (data_dir / "paper_state.json").write_text(
                json.dumps({
                    "cash": 3.0,
                    "positions": [
                        {
                            "status": "open",
                            "shares": 5.0,
                            "current_price": 0.80,
                            "stake": 4.50,
                        }
                    ],
                })
            )
            (data_dir / "trade_journal.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({
                            "closed_at": "2026-05-26T10:00:00+00:00",
                            "token_id": "a",
                            "question": "Will X happen?",
                            "exit_reason": "take_profit",
                            "realized_pnl": 0.40,
                        }),
                        json.dumps({
                            "event": "position_closed",
                            "closed_at": "2026-05-26T11:00:00+00:00",
                            "token_id": "b",
                            "question": "Will Y happen?",
                            "exit_reason": "sync_closed",
                            "realized_pnl_usd": -0.10,
                        }),
                    ]
                )
            )
            (data_dir / "realized_trade_cache.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({
                            "closed_at": "2026-05-26T10:00:00+00:00",
                            "token_id": "a",
                            "question": "Will X happen?",
                            "exit_reason": "take_profit",
                            "realized_pnl": 0.40,
                        }),
                        json.dumps({
                            "closed_at": "2026-05-26T12:00:00+00:00",
                            "token_id": "c",
                            "question": "Will Z happen?",
                            "exit_reason": "race_take_profit",
                            "realized_pnl": 0.20,
                        }),
                    ]
                )
            )

            with mock.patch.dict(os.environ, {"POLYMARKET_PROFILE_LABEL": "grinder"}, clear=False):
                snap = live_analyst.load_live_snapshot()

        self.assertIsNotNone(snap)
        self.assertEqual(snap.profile, "grinder")
        self.assertEqual(snap.closed, 3)
        self.assertEqual(snap.wins, 2)
        self.assertEqual(snap.losses, 1)
        self.assertAlmostEqual(snap.realized_pnl, 0.50)
        self.assertAlmostEqual(snap.win_rate, 2 / 3 * 100.0)
        self.assertAlmostEqual(snap.equity, 7.0)

    def test_top_closed_trades_reads_cache_when_journal_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            live_analyst.DATA_DIR = data_dir
            (data_dir / "realized_trade_cache.jsonl").write_text(
                json.dumps({
                    "closed_at": "2026-05-26T12:00:00+00:00",
                    "question": "cached winner",
                    "exit_reason": "sync_closed_win",
                    "realized_pnl": 0.66,
                })
            )

            rows = live_analyst.load_top_closed_trades()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["question"], "cached winner")
        self.assertAlmostEqual(rows[0]["pnl"], 0.66)


if __name__ == "__main__":
    unittest.main()
