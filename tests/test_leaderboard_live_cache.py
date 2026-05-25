import json
import tempfile
from pathlib import Path
import unittest

from polymarket_bot.leaderboard import gather_live_stats


class LiveLeaderboardCacheTests(unittest.TestCase):
    def test_live_stats_reads_realized_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "paper_state.json").write_text(
                json.dumps({"cash": 6.7, "positions": []}),
                encoding="utf-8",
            )
            (base / "live_baseline.json").write_text(
                json.dumps({"starting_cash": 7.34}),
                encoding="utf-8",
            )
            (base / "live_config_snapshot.toml").write_text(
                "[run]\nstarting_cash = 6.0\nmode = \"grinder\"\n",
                encoding="utf-8",
            )
            (base / "trade_journal.jsonl").write_text("", encoding="utf-8")
            (base / "realized_trade_cache.jsonl").write_text(
                json.dumps({
                    "closed_at": "2026-05-25T10:00:00+00:00",
                    "market_id": "fred",
                    "token_id": "fred-token",
                    "question": "Spread: Fredrikstad FK (-1.5)",
                    "exit_reason": "race_take_profit",
                    "realized_pnl": 0.45,
                })
                + "\n"
                + json.dumps({
                    "closed_at": "2026-05-25T11:00:00+00:00",
                    "market_id": "btc",
                    "token_id": "btc-token",
                    "question": "BTC range",
                    "exit_reason": "race_big_win_resolved",
                    "realized_pnl": 0.25,
                })
                + "\n",
                encoding="utf-8",
            )

            stats = gather_live_stats(base)

            self.assertIsNotNone(stats)
            assert stats is not None
            self.assertEqual(stats.wins, 2)
            self.assertEqual(stats.losses, 0)
            self.assertEqual(stats.closed_trades, 2)
            self.assertAlmostEqual(stats.realized_pnl, 0.70)
            self.assertAlmostEqual(stats.total_pnl, 0.70)
            self.assertAlmostEqual(stats.roi_pct, 11.6667, places=3)


if __name__ == "__main__":
    unittest.main()
