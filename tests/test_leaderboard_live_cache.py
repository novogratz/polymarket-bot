import json
import tempfile
from pathlib import Path
import unittest

from polymarket_bot.leaderboard import PositionSummary, RunStats, format_leaderboard, format_leaderboard_telegram, gather_live_stats


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

    def test_live_row_separates_closed_and_open_positions(self) -> None:
        live = RunStats(
            run_name="grinder",
            starting_cash=6.0,
            cash=2.0,
            invested=4.0,
            unrealized_pnl=0.0,
            equity=6.0,
            open_positions=1,
            closed_trades=3,
            wins=3,
            losses=0,
            realized_pnl=1.1,
            started_at=None,
            total_ticks=0,
            top_open=[
                PositionSummary(
                    title="Coritiba FBC vs. EC Bahia: O/U 4.5",
                    pnl=-3.50,
                    stake=4.55,
                    price=0.60,
                    reason="grinder",
                    outcome="Under",
                    end_date="2099-01-01T00:00:00+00:00",
                    status="open",
                )
            ],
        )
        text = format_leaderboard([], live=live)
        self.assertIn("Closed: 3W/0L  Open: 1", text)
        telegram = format_leaderboard_telegram([], live=live)
        self.assertIn("🔵 grinder LIVE is also running!", telegram)
        self.assertIn("Closed 3W/0L  Open 1", telegram)
        self.assertIn("Ongoing:", telegram)

    def test_open_position_line_shows_side_and_close_eta(self) -> None:
        line = format_leaderboard(
            [],
            live=RunStats(
                run_name="grinder",
                starting_cash=6.0,
                cash=2.0,
                invested=4.0,
                unrealized_pnl=0.0,
                equity=6.0,
                open_positions=1,
                closed_trades=3,
                wins=3,
                losses=0,
                realized_pnl=1.1,
                started_at=None,
                total_ticks=0,
                top_open=[
                    PositionSummary(
                        title="Coritiba FBC vs. EC Bahia: O/U 4.5",
                        pnl=-3.50,
                        stake=4.55,
                        price=0.60,
                        reason="grinder",
                        outcome="Under",
                        end_date="2099-01-01T00:00:00+00:00",
                        status="open",
                    )
                ],
            ),
        )
        self.assertIn("Under Coritiba FBC vs. EC Bahia: O/U 4.5", line)
        self.assertIn("in ", line)


if __name__ == "__main__":
    unittest.main()
