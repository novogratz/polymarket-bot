import json
import tempfile
from datetime import timedelta
from pathlib import Path
import unittest

from polymarket_bot.leaderboard import PositionSummary, RunStats, format_leaderboard, format_leaderboard_telegram, gather_live_stats, gather_run_stats
from polymarket_bot.models import Candidate, utc_now


class LiveLeaderboardCacheTests(unittest.TestCase):
    def _candidate(self, *, token_id: str, price: float, best_bid: float, best_ask: float, end_date) -> Candidate:
        return Candidate(
            market_id="m1",
            question="Coritiba FBC vs. EC Bahia: O/U 4.5",
            slug="cor-bah",
            end_date=end_date,
            hours_to_close=1.0,
            liquidity=1000.0,
            volume=2000.0,
            outcome="Under",
            price=price,
            token_id=token_id,
            score=1.0,
            url="https://polymarket.com/sports/bra/bra-cor-bah-2026-05-25",
            best_bid=best_bid,
            best_ask=best_ask,
            tick_size=0.01,
            neg_risk=False,
            accepts_orders=True,
            event_slug="bra-cor-bah-2026-05-25",
        )

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

    def test_dry_run_refreshes_open_position_quotes_before_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            run = base / "dry_runs" / "grinder"
            run.mkdir(parents=True)
            (run / "state.json").write_text(
                json.dumps(
                    {
                        "cash": 1.51,
                        "positions": [
                            {
                                "status": "open",
                                "token_id": "tok-under",
                                "market_id": "m1",
                                "question": "Coritiba FBC vs. EC Bahia: O/U 4.5",
                                "slug": "cor-bah",
                                "event_slug": "bra-cor-bah-2026-05-25",
                                "outcome": "Under",
                                "stake": 4.55,
                                "shares": 21.666666,
                                "entry_price": 0.21,
                                "current_price": 0.21,
                                "unrealized_pnl": -3.50,
                                "end_date": "2026-05-25T14:00:00+00:00",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (run / "metadata.json").write_text(
                json.dumps({"starting_cash": 6.0, "total_ticks": 1, "started_at": utc_now().isoformat()}),
                encoding="utf-8",
            )
            (run / "journal.jsonl").write_text("", encoding="utf-8")

            import polymarket_bot.leaderboard as leaderboard

            orig = leaderboard.ensure_open_positions_in_pool
            try:
                leaderboard.ensure_open_positions_in_pool = lambda _settings, _portfolio, candidates: [
                    self._candidate(
                        token_id="tok-under",
                        price=0.90,
                        best_bid=0.89,
                        best_ask=0.92,
                        end_date=utc_now() + timedelta(minutes=35),
                    )
                ]
                stats = gather_run_stats(base, "grinder")
            finally:
                leaderboard.ensure_open_positions_in_pool = orig

            assert stats is not None
            self.assertEqual(stats.open_positions, 1)
            self.assertAlmostEqual(stats.top_open[0].price, 0.89, places=2)
            self.assertEqual(stats.top_open[0].outcome, "Under")
            self.assertNotIn("expired", format_leaderboard([stats]).lower())
            self.assertIn("Under Coritiba FBC vs. EC Bahia: O/U 4.5", format_leaderboard([stats]))
            self.assertIn("in ", format_leaderboard([stats]))

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
        # Telegram: with no dry runs, the live bot IS the board (live-only).
        # The closed W/L (3W/0L) must stay separate from the open position's
        # unrealized PnL — the open shows on its own "Open:" line, never
        # folded into the W/L count.
        telegram = format_leaderboard_telegram([], live=live)
        self.assertIn("LIVE only", telegram)
        self.assertIn("🔵 grinder", telegram)
        self.assertIn("3W", telegram)
        self.assertIn("0L", telegram)
        self.assertIn("Open:", telegram)

    def test_live_stats_refreshes_open_position_quotes_before_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "paper_state.json").write_text(
                json.dumps(
                    {
                        "cash": 1.51,
                        "positions": [
                            {
                                "status": "open",
                                "token_id": "tok-under",
                                "market_id": "m1",
                                "question": "Coritiba FBC vs. EC Bahia: O/U 4.5",
                                "slug": "cor-bah",
                                "event_slug": "bra-cor-bah-2026-05-25",
                                "outcome": "Under",
                                "stake": 4.55,
                                "shares": 21.666666,
                                "entry_price": 0.21,
                                "current_price": 0.21,
                                "unrealized_pnl": -3.50,
                                "end_date": "2026-05-25T14:00:00+00:00",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (base / "live_baseline.json").write_text(
                json.dumps({"starting_cash": 6.0}),
                encoding="utf-8",
            )
            (base / "live_config_snapshot.toml").write_text(
                "[run]\nstarting_cash = 6.0\nmode = \"grinder\"\n",
                encoding="utf-8",
            )
            (base / "trade_journal.jsonl").write_text("", encoding="utf-8")

            import polymarket_bot.leaderboard as leaderboard

            orig = leaderboard.ensure_open_positions_in_pool
            try:
                leaderboard.ensure_open_positions_in_pool = lambda _settings, _portfolio, candidates: [
                    self._candidate(
                        token_id="tok-under",
                        price=0.90,
                        best_bid=0.89,
                        best_ask=0.92,
                        end_date=utc_now() + timedelta(minutes=35),
                    )
                ]
                stats = gather_live_stats(base)
            finally:
                leaderboard.ensure_open_positions_in_pool = orig

            assert stats is not None
            self.assertEqual(stats.open_positions, 1)
            self.assertAlmostEqual(stats.top_open[0].price, 0.89, places=2)
            self.assertNotIn("expired", format_leaderboard([], live=stats).lower())
            self.assertIn("Under Coritiba FBC vs. EC Bahia: O/U 4.5", format_leaderboard([], live=stats))

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
