"""Tests for the live-position sync helpers in polymarket_bot.main."""

from pathlib import Path
from unittest import mock
import unittest

from polymarket_bot.config import Settings
from polymarket_bot.main import (
    _position_from_live_api,
    _sync_live_positions,
    _update_position_from_live_api,
    smart_money_once,
)
from polymarket_bot.portfolio import Portfolio


def _live_item(**overrides):
    base = {
        "conditionId": "0xabc",
        "title": "Will CF Montréal win on 2026-05-09?",
        "slug": "mls-mim-orl-2026-05-09-mim",
        "eventSlug": "mls-mim-orl-2026-05-09",
        "outcome": "Yes",
        "asset": "tok-1",
        "size": 10.0,
        "avgPrice": 0.50,
        "curPrice": 0.55,
        "initialValue": 5.0,
        "currentValue": 5.5,
        "totalBought": 10.0,
        "realizedPnl": 0.0,
        "endDate": "2026-05-09T22:00:00Z",
    }
    base.update(overrides)
    return base


class CreatePositionUrlTests(unittest.TestCase):
    def test_url_uses_event_slug_not_market_slug(self):
        position = _position_from_live_api(_live_item())
        self.assertEqual(
            position["url"],
            "https://polymarket.com/event/mls-mim-orl-2026-05-09",
        )

    def test_url_falls_back_to_slug_when_event_slug_missing(self):
        position = _position_from_live_api(_live_item(eventSlug=""))
        self.assertEqual(
            position["url"],
            "https://polymarket.com/event/mls-mim-orl-2026-05-09-mim",
        )


class UpdatePositionUrlTests(unittest.TestCase):
    def test_update_rewrites_broken_url_to_event_slug(self):
        position = {
            "status": "open",
            "slug": "mls-mim-orl-2026-05-09-mim",
            "event_slug": "",
            "url": "https://polymarket.com/event/mls-mim-orl-2026-05-09-mim",
        }
        _update_position_from_live_api(position, _live_item())
        self.assertEqual(
            position["url"],
            "https://polymarket.com/event/mls-mim-orl-2026-05-09",
        )
        self.assertEqual(position["event_slug"], "mls-mim-orl-2026-05-09")

    def test_update_keeps_url_when_no_slugs_received(self):
        position = {
            "status": "open",
            "slug": "fallback-slug",
            "event_slug": "fallback-event",
            "url": "https://polymarket.com/event/fallback-event",
        }
        item = _live_item(slug="", eventSlug="")
        _update_position_from_live_api(position, item)
        self.assertEqual(
            position["url"],
            "https://polymarket.com/event/fallback-event",
        )

    def test_update_promotes_slug_only_to_event_url(self):
        position = {
            "status": "open",
            "slug": "",
            "event_slug": "",
            "url": "https://polymarket.com",
        }
        item = _live_item(eventSlug="", slug="my-market-slug")
        _update_position_from_live_api(position, item)
        self.assertEqual(
            position["url"],
            "https://polymarket.com/event/my-market-slug",
        )


class SmartMoneyDrySyncTests(unittest.TestCase):
    def test_dry_run_reconciles_live_positions_when_enabled(self):
        with self.assertRaisesRegex(RuntimeError, "sync-stop"):
            with mock.patch("polymarket_bot.main.maybe_tune", return_value=({}, 0)), \
                mock.patch("polymarket_bot.main.load_smart_candidates", return_value=[]), \
                mock.patch(
                    "polymarket_bot.main.Portfolio.load",
                    return_value=Portfolio(cash=6.0, positions=[]),
                ), \
                mock.patch(
                    "polymarket_bot.main._sync_live_positions",
                    side_effect=RuntimeError("sync-stop"),
                ) as mock_sync, \
                mock.patch("polymarket_bot.main.ensure_open_positions_in_pool", return_value=[]), \
                mock.patch("polymarket_bot.main.build_client"), \
                mock.patch("polymarket_bot.main._cancel_stale_pending_orders", return_value=[]), \
                mock.patch("polymarket_bot.main._detect_cohort_exits", return_value=(set(), [])), \
                mock.patch("polymarket_bot.main.require_saved_api_creds"), \
                mock.patch("polymarket_bot.main._execute_sell_strategy", return_value=[]), \
                mock.patch("polymarket_bot.main.fetch_smart_money_data", return_value={}), \
                mock.patch("polymarket_bot.main.analyze_smart_money_with_data") as mock_analyze:
                mock_analyze.return_value = mock.Mock(
                    opportunities=[],
                    selected=None,
                    to_dict=lambda: {},
                )
                settings = Settings(
                    dry_run=True,
                    sync_live_positions=True,
                    funder_address="0xabc",
                    quiet=True,
                    state_path=Path("/private/tmp/state.json"),
                    trade_journal_path=Path("/private/tmp/journal.jsonl"),
                    paper_balance_usd=6.0,
                )
                smart_money_once(settings)
            self.assertEqual(mock_sync.call_count, 1)


class SyncLivePositionsTests(unittest.TestCase):
    def test_sync_close_returns_proceeds_to_cash(self):
        class FakeDataApiClient:
            def __init__(self, *args, **kwargs):
                pass

            def positions(self, *, user: str, limit: int = 500):
                return []

        settings = Settings(
            funder_address="0xabc",
            dry_run=True,
            sync_live_positions=True,
            paper_balance_usd=6.0,
            trade_journal_path=Path("/private/tmp/journal.jsonl"),
        )
        portfolio = Portfolio(
            cash=1.45,
            positions=[
                {
                    "status": "open",
                    "live": True,
                    "token_id": "tok-1",
                    "market_id": "m1",
                    "question": "Q",
                    "outcome": "Yes",
                    "stake": 4.55,
                    "shares": 5.0,
                    "current_price": 0.5,
                    "unrealized_pnl": -2.05,
                    "opened_at": "2026-05-25T00:00:00+00:00",
                }
            ],
            pending_orders=[],
        )

        with mock.patch("polymarket_bot.main.DataApiClient", FakeDataApiClient):
            report = _sync_live_positions(settings, portfolio)

        self.assertEqual(report[0]["action"], "closed_stale_local_position")
        self.assertEqual(portfolio.positions[0]["status"], "closed")
        self.assertAlmostEqual(portfolio.cash, 3.95, places=2)
        self.assertTrue(portfolio.positions[0]["sync_closed"])


class ResolvedReconcileTests(unittest.TestCase):
    """v4 (user 2026-06-21): resolved positions are booked promptly at their
    true on-chain value — redeemable winners as wins, settled-to-~0 losers at
    $0 (not the stale mid-price), and only once the market's endDate has passed."""

    def _settings(self):
        return Settings(
            funder_address="0xabc", dry_run=True, sync_live_positions=True,
            paper_balance_usd=1.0, trade_journal_path=Path("/private/tmp/journal.jsonl"),
        )

    def _portfolio(self, **pos):
        base = {
            "status": "open", "live": True, "token_id": "tok-1", "market_id": "m1",
            "question": "Q", "outcome": "Up", "stake": 5.0, "shares": 6.0,
            "current_price": 0.5, "opened_at": "2026-05-25T00:00:00+00:00",
        }
        base.update(pos)
        return Portfolio(cash=0.0, positions=[base], pending_orders=[])

    def _client(self, item):
        class FakeDataApiClient:
            def __init__(self, *a, **k):
                pass

            def positions(self, *, user, limit=500):
                return [item]
        return FakeDataApiClient

    def test_redeemable_winner_booked_as_win(self):
        # redeemable=True → resolved winner; close at full on-chain value.
        item = {"asset": "tok-1", "size": 6.0, "currentValue": 5.94,
                "redeemable": True, "endDate": "2026-06-21T22:00:00Z"}
        settings, portfolio = self._settings(), self._portfolio()
        with mock.patch("polymarket_bot.main.DataApiClient", self._client(item)):
            report = _sync_live_positions(settings, portfolio)
        p = portfolio.positions[0]
        self.assertEqual(p["status"], "closed")
        self.assertTrue(p["resolved_redeemable"])
        self.assertAlmostEqual(p["realized_pnl"], 0.94, places=2)  # 5.94 - 5.00
        self.assertEqual(report[0]["action"], "resolved_win")

    def test_settled_loser_booked_at_zero_when_past_end(self):
        # near-zero value + past endDate → resolved loss at ~0 (not stale 0.5).
        item = {"asset": "tok-1", "size": 6.0, "currentValue": 0.06,
                "redeemable": False, "endDate": "2026-06-21T22:00:00Z"}
        settings, portfolio = self._settings(), self._portfolio()
        with mock.patch("polymarket_bot.main.DataApiClient", self._client(item)):
            report = _sync_live_positions(settings, portfolio)
        p = portfolio.positions[0]
        self.assertEqual(p["status"], "closed")
        self.assertAlmostEqual(p["realized_pnl"], -4.94, places=2)  # 0.06 - 5.00
        self.assertEqual(report[0]["action"], "resolved_loss")

    def test_low_value_before_end_is_not_booked_as_resolved(self):
        # near-zero but endDate in the FUTURE → mid-game gap, NOT resolved.
        # Must not be booked as a resolved loss (it's a held favorite).
        from datetime import timedelta
        from polymarket_bot.models import utc_now
        item = {"asset": "tok-1", "size": 6.0, "currentValue": 0.30,
                "redeemable": False,
                "endDate": (utc_now() + timedelta(hours=3)).isoformat()}
        settings, portfolio = self._settings(), self._portfolio()
        with mock.patch("polymarket_bot.main.DataApiClient", self._client(item)):
            report = _sync_live_positions(settings, portfolio)
        # Not resolved → no resolved_* action for this token.
        self.assertNotIn("resolved", " ".join(str(r.get("action")) for r in report))


if __name__ == "__main__":
    unittest.main()
