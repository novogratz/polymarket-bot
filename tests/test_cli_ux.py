"""Tests pour les commandes UX du CLI : status, positions, journal-stats.

Aucun appel réseau, aucun appel SDK : tout passe par le ledger / journal
locaux écrits dans tmp_path.
"""

from __future__ import annotations

import os

os.environ["POLYMARKET_SKIP_DOTENV"] = "1"
for _k in [k for k in os.environ if k.startswith("POLYMARKET_") and k != "POLYMARKET_SKIP_DOTENV"]:
    del os.environ[_k]

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from polymarket_bot.config import Settings
from polymarket_bot.main import (
    _humanize_close_eta,
    _humanize_seconds,
    format_positions_table,
    journal_stats,
    status_summary,
)
from polymarket_bot.models import utc_now
from polymarket_bot.portfolio import Portfolio


def _make_open_position(question: str, *, stake: float, entry: float, current: float, end_in_hours: float | None = 24) -> dict:
    end_iso = (utc_now() + timedelta(hours=end_in_hours)).isoformat() if end_in_hours is not None else None
    shares = stake / entry if entry > 0 else 0.0
    unrealized = round(shares * current - stake, 2)
    return {
        "status": "open",
        "opened_at": utc_now().isoformat(),
        "market_id": "m-" + question[:6],
        "question": question,
        "outcome": "Yes",
        "token_id": "tok-" + question[:6],
        "entry_price": entry,
        "current_price": current,
        "stake": stake,
        "shares": shares,
        "initial_shares": shares,
        "unrealized_pnl": unrealized,
        "end_date": end_iso,
    }


class StatusSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.state_path = self.tmp_path / "state.json"
        self.journal_path = self.tmp_path / "journal.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _settings(self, **overrides) -> Settings:
        return Settings(
            state_path=self.state_path,
            trade_journal_path=self.journal_path,
            paper_balance_usd=100.0,
            **overrides,
        )

    def test_disabled_mode_when_neither_live_nor_dry_run(self) -> None:
        snapshot = status_summary(self._settings())
        self.assertEqual(snapshot["mode"], "disabled")
        self.assertFalse(snapshot["state_exists"])
        self.assertEqual(snapshot["open_positions"], 0)
        self.assertEqual(snapshot["cash"], 100.0)

    def test_dry_run_mode(self) -> None:
        snapshot = status_summary(self._settings(dry_run=True))
        self.assertEqual(snapshot["mode"], "dry-run")
        # state_path explicite => pas de swap automatique vers data/dry_run_state.json
        self.assertEqual(snapshot["state_path"], str(self.state_path))

    def test_live_mode(self) -> None:
        snapshot = status_summary(self._settings(live_trading_enabled=True))
        self.assertEqual(snapshot["mode"], "live")

    def test_reports_journal_records_count(self) -> None:
        self.journal_path.write_text(
            "\n".join(
                [
                    json.dumps({"realized_pnl": 1.0}),
                    json.dumps({"realized_pnl": -0.5}),
                    "",  # ligne vide ignorée
                    json.dumps({"realized_pnl": 0.0}),
                ]
            )
        )
        snapshot = status_summary(self._settings())
        self.assertEqual(snapshot["journal_records"], 3)

    def test_reads_existing_ledger(self) -> None:
        portfolio = Portfolio(cash=42.0, positions=[_make_open_position("Q1", stake=10.0, entry=0.50, current=0.55)])
        portfolio.save(self.state_path)
        snapshot = status_summary(self._settings())
        self.assertEqual(snapshot["cash"], 42.0)
        self.assertEqual(snapshot["open_positions"], 1)
        self.assertTrue(snapshot["state_exists"])
        self.assertIsNotNone(snapshot["ledger_age_seconds"])


class PositionsTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "state.json"
        os.environ.setdefault("NO_COLOR", "1")  # désactive ANSI pour tests stables

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _settings(self) -> Settings:
        return Settings(state_path=self.state_path, paper_balance_usd=100.0)

    def test_empty_when_no_positions(self) -> None:
        out = format_positions_table(self._settings())
        self.assertIn("no open positions", out)

    def test_renders_headers_and_rows_sorted_by_pnl(self) -> None:
        positions = [
            _make_open_position("Loser market question text", stake=10.0, entry=0.50, current=0.40),
            _make_open_position("Winner market question text", stake=10.0, entry=0.50, current=0.70),
            _make_open_position("Flat market question text", stake=10.0, entry=0.50, current=0.50),
        ]
        Portfolio(cash=70.0, positions=positions).save(self.state_path)
        out = format_positions_table(self._settings())
        for header in ("Market", "Outcome", "Stake", "Entry", "Now", "PnL", "Return", "Closes"):
            self.assertIn(header, out)
        winner_idx = out.index("Winner")
        flat_idx = out.index("Flat")
        loser_idx = out.index("Loser")
        # Tri PnL décroissant : winner avant flat, flat avant loser.
        self.assertLess(winner_idx, flat_idx)
        self.assertLess(flat_idx, loser_idx)


class JournalStatsExposureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.journal_path = Path(self.tmp.name) / "journal.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _settings(self, **overrides) -> Settings:
        return Settings(
            trade_journal_path=self.journal_path,
            state_path=Path(self.tmp.name) / "state.json",
            **overrides,
        )

    def test_empty_journal_includes_path_and_dry_run_flag(self) -> None:
        result = journal_stats(self._settings(dry_run=True))
        self.assertEqual(result["records"], 0)
        self.assertEqual(result["journal_path"], str(self.journal_path))
        self.assertTrue(result["dry_run"])

    def test_populated_journal_includes_path_and_dry_run_flag(self) -> None:
        self.journal_path.write_text(
            "\n".join(
                json.dumps(
                    {
                        "realized_pnl": pnl,
                        "category": "POLITICS",
                        "consensus": 2,
                        "strategy": "smart_money",
                        "exit_reason": "take_profit",
                        "entry_price": 0.5,
                    }
                )
                for pnl in (0.5, -0.3, 0.1)
            )
        )
        result = journal_stats(self._settings())
        self.assertEqual(result["records"], 3)
        self.assertEqual(result["journal_path"], str(self.journal_path))
        self.assertFalse(result["dry_run"])


class HumanizeHelpersTests(unittest.TestCase):
    def test_humanize_seconds_buckets(self) -> None:
        self.assertEqual(_humanize_seconds(5), "5s ago")
        self.assertEqual(_humanize_seconds(120), "2m ago")
        self.assertIn("h ago", _humanize_seconds(7200))
        self.assertIn("d ago", _humanize_seconds(200_000))

    def test_humanize_close_eta_handles_none(self) -> None:
        self.assertEqual(_humanize_close_eta(None), "—")

    def test_humanize_close_eta_future(self) -> None:
        future = (utc_now() + timedelta(hours=4)).isoformat()
        out = _humanize_close_eta(future)
        self.assertTrue(out.startswith("in "))
        self.assertIn("h", out)

    def test_humanize_close_eta_past(self) -> None:
        past = (utc_now() - timedelta(minutes=12)).isoformat()
        out = _humanize_close_eta(past)
        self.assertTrue(out.startswith("expired "))


if __name__ == "__main__":
    unittest.main()
