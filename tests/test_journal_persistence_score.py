"""Trade journal : champ persistence_score propagé."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from polymarket_bot.config import Settings
from polymarket_bot.main import _append_trade_journal


class TestJournalPersistenceScore(unittest.TestCase):
    def test_journal_entry_includes_persistence_score(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.jsonl"
            settings = Settings(trade_journal_path=journal, quiet=True)
            position = {
                "id": "abc",
                "market_id": "m1",
                "side": "yes",
                "size": 10.0,
                "entry_price": 0.40,
                "exit_price": 0.50,
                "pnl_usd": 1.0,
                "persistence_score": 0.83,
                "consensus": 2,
                "copied_usdc": 200.0,
                "tag": "smart_money",
                "opened_at": 1700000000,
                "closed_at": 1700001000,
            }
            _append_trade_journal(settings, position, reason="tp")
            lines = journal.read_text().splitlines()
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[0])
            self.assertIn("persistence_score", entry)
            self.assertAlmostEqual(entry["persistence_score"], 0.83)

    def test_journal_entry_persistence_score_defaults_zero(self) -> None:
        """Si la position n'a pas persistence_score (ex: pré-filtre désactivé), default 0.0."""
        with TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.jsonl"
            settings = Settings(trade_journal_path=journal, quiet=True)
            position = {
                "id": "xyz",
                "market_id": "m2",
                "side": "yes",
                "size": 5.0,
                "entry_price": 0.30,
                "exit_price": 0.20,
                "pnl_usd": -0.5,
                "tag": "noise_fallback",
                "opened_at": 1700000000,
                "closed_at": 1700001000,
                # pas de persistence_score
            }
            _append_trade_journal(settings, position, reason="stop_loss")
            entry = json.loads(journal.read_text().splitlines()[0])
            self.assertAlmostEqual(entry.get("persistence_score", 0.0), 0.0)


if __name__ == "__main__":
    unittest.main()
