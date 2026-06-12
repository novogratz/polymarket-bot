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

    def test_todays_trades_include_large_stakes(self):
        # Regression: a fixed cost_basis > $100 filter silently dropped every
        # full-size win once percentage sizing pushed stakes past $100
        # (Nigeria / Las Palmas / Orebro on 2026-06-10).
        import time
        today = time.strftime("%Y-%m-%d", time.gmtime())
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            live_analyst.DATA_DIR = data_dir
            (data_dir / "trade_journal.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({
                            "event": "position_closed",
                            "closed_at": f"{today}T18:57:17+00:00",
                            "token_id": "big",
                            "question": "Orebro SK vs. GIF Sundsvall: O/U 4.5",
                            "exit_reason": "race_big_win_resolved",
                            "cost_basis": 350.16,
                            "realized_pnl": 39.34,
                            "pnl_pct": 0.1123,
                        }),
                        json.dumps({
                            "event": "position_closed",
                            "closed_at": f"{today}T20:25:37+00:00",
                            "token_id": "sweep",
                            "question": "Will Nigeria win on 2026-06-10?",
                            "exit_reason": "resolved_market_sweep_win",
                            "cost_basis": 347.96,
                            "realized_pnl_usd": 22.5352,
                            "realized_pnl_pct": 0.0648,
                        }),
                        json.dumps({
                            "event": "position_closed",
                            "closed_at": f"{today}T12:30:17+00:00",
                            "token_id": "small",
                            "question": "Will annual inflation be 4.1% in May?",
                            "exit_reason": "race_big_win_resolved",
                            "cost_basis": 16.82,
                            "realized_pnl": 0.95,
                            "pnl_pct": 0.0565,
                        }),
                    ]
                )
            )

            rows = live_analyst.load_todays_trades()

        questions = {r["question"] for r in rows}
        self.assertIn("Orebro SK vs. GIF Sundsvall: O/U 4.5", questions)
        self.assertIn("Will Nigeria win on 2026-06-10?", questions)
        self.assertIn("Will annual inflation be 4.1% in May?", questions)
        self.assertEqual(len(rows), 3)
        # Sorted by PnL desc — the big win leads the list
        self.assertAlmostEqual(rows[0]["pnl"], 39.34)

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



class OpenPositionExpiryTests(unittest.TestCase):
    """User request 2026-06-11: POSITIONS OUVERTES sorted by expiry, each
    line showing when the game finishes / the market expires."""

    def test_positions_sorted_by_soonest_expiry_missing_dates_last(self):
        import tempfile

        def _no_api():
            raise ValueError("hermetic test: no Data API")

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            old = live_analyst.DATA_DIR
            old_client = live_analyst._get_settings_and_client
            live_analyst.DATA_DIR = data_dir
            live_analyst._get_settings_and_client = _no_api
            try:
                (data_dir / "paper_state.json").write_text(json.dumps({"positions": [
                    {"status": "open", "question": "late", "outcome": "No",
                     "entry_price": 0.9, "shares": 10, "current_price": 0.95,
                     "stake": 9.0, "end_date": "2026-06-11T20:00:00+00:00"},
                    {"status": "open", "question": "no-date", "outcome": "No",
                     "entry_price": 0.9, "shares": 10, "current_price": 0.99,
                     "stake": 9.0},
                    {"status": "open", "question": "soon", "outcome": "No",
                     "entry_price": 0.9, "shares": 10, "current_price": 0.91,
                     "stake": 9.0, "end_date": "2026-06-11T13:30:00+00:00"},
                ]}))
                rows = live_analyst.load_open_positions()
            finally:
                live_analyst.DATA_DIR = old
                live_analyst._get_settings_and_client = old_client
        self.assertEqual([r["question"] for r in rows], ["soon", "late", "no-date"])

    def test_fmt_expiry_fr_future_past_and_missing(self):
        from datetime import datetime, timezone
        now = datetime(2026, 6, 11, 16, 0, tzinfo=timezone.utc)  # 12:00 ET
        line = live_analyst._fmt_expiry_fr("2026-06-11T18:05:00+00:00", now=now)
        self.assertIn("Fin prévue", line)
        self.assertIn("14:05 ET", line)
        self.assertIn("dans 2h05", line)
        short = live_analyst._fmt_expiry_fr("2026-06-11T16:25:00+00:00", now=now)
        self.assertIn("dans 25min", short)
        past = live_analyst._fmt_expiry_fr("2026-06-11T15:00:00+00:00", now=now)
        self.assertIn("Échéance passée", past)
        self.assertEqual(live_analyst._fmt_expiry_fr(""), "")
        self.assertEqual(live_analyst._fmt_expiry_fr("garbage"), "")

    def test_date_only_end_shows_date_never_a_fake_clock_time(self):
        # Gamma stamps date-level markets at midnight UTC; June 12 00:00 UTC
        # used to render as "11/06 20:00 ET" — a fabricated time. Show the
        # date alone.
        from datetime import datetime, timezone
        now = datetime(2026, 6, 11, 16, 0, tzinfo=timezone.utc)
        line = live_analyst._fmt_expiry_fr("2026-06-12T00:00:00+00:00", now=now)
        self.assertIn("Expire le 12/06", line)
        self.assertNotIn("ET", line)
        # Recently-past date-only stamp (e.g. PPI print morning-of): not
        # declared past until a full day beyond the stamp.
        recent = live_analyst._fmt_expiry_fr("2026-06-11T00:00:00+00:00", now=now)
        self.assertIn("Expire le 11/06", recent)
        old = live_analyst._fmt_expiry_fr("2026-06-09T00:00:00+00:00", now=now)
        self.assertIn("Échéance passée", old)

    def test_sports_show_kickoff_not_end_date(self):
        from datetime import datetime, timezone
        kickoff = "2026-06-11T18:00:00+00:00"   # 14:00 ET
        end_date = "2026-06-11T00:00:00+00:00"  # useless midnight stamp
        before = live_analyst._fmt_expiry_fr(
            end_date, kickoff, now=datetime(2026, 6, 11, 16, 0, tzinfo=timezone.utc))
        self.assertIn("Coup d'envoi : 14:00 ET", before)
        self.assertIn("dans 2h00", before)
        during = live_analyst._fmt_expiry_fr(
            end_date, kickoff, now=datetime(2026, 6, 11, 19, 0, tzinfo=timezone.utc))
        self.assertIn("Match en cours", during)
        after = live_analyst._fmt_expiry_fr(
            end_date, kickoff, now=datetime(2026, 6, 11, 22, 0, tzinfo=timezone.utc))
        self.assertIn("Match terminé", after)

    def test_sort_kickoff_before_dateonly_next_day(self):
        # Mexico (kickoff this afternoon) must sort before PPI (date-only
        # stamp of today = resolves through tomorrow morning) and Israel
        # (date-only tomorrow).
        mexico = {"game_start": "2026-06-11T18:00:00+00:00",
                  "end_date": "2026-06-11T00:00:00+00:00"}
        ppi = {"end_date": "2026-06-11T00:00:00+00:00"}
        israel = {"end_date": "2026-06-12T00:00:00+00:00"}
        ordered = sorted([israel, ppi, mexico], key=live_analyst._position_end_sort_key)
        self.assertEqual(ordered, [mexico, ppi, israel])


class RedeemableWatchdogTests(unittest.TestCase):
    """User request 2026-06-12: surface resolved-but-unpaid positions in the
    report. Losing-side dust (value 0) and sub-$1 remainders are ignored."""

    def test_redeemable_positions_filtered_and_sorted(self):
        class FakeDC:
            def positions(self, user):
                return [
                    {"title": "Won market", "outcome": "Under", "size": 295.35,
                     "currentValue": 295.35, "redeemable": True},
                    {"title": "Losing dust", "outcome": "Yes", "size": 14580,
                     "currentValue": 0, "redeemable": True},
                    {"title": "Still open", "outcome": "No", "size": 100,
                     "currentValue": 92.0, "redeemable": False},
                    {"title": "Small winner", "outcome": "Over", "size": 5,
                     "currentValue": 5.0, "redeemable": True},
                ]

        class FakeSettings:
            funder_address = "0xabc"

        old = live_analyst._get_settings_and_client
        live_analyst._get_settings_and_client = lambda: (FakeSettings(), FakeDC())
        try:
            rows = live_analyst.load_redeemable_positions()
        finally:
            live_analyst._get_settings_and_client = old
        self.assertEqual([r["question"] for r in rows], ["Won market", "Small winner"])
        self.assertAlmostEqual(rows[0]["value"], 295.35)

    def test_fails_open_on_api_error(self):
        def _boom():
            raise ValueError("no api")
        old = live_analyst._get_settings_and_client
        live_analyst._get_settings_and_client = _boom
        try:
            self.assertEqual(live_analyst.load_redeemable_positions(), [])
        finally:
            live_analyst._get_settings_and_client = old

if __name__ == "__main__":
    unittest.main()
