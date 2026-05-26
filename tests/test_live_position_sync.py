"""Tests for the live-position sync helpers in polymarket_bot.main."""

from pathlib import Path
from unittest import mock
import unittest

from polymarket_bot.config import Settings
from polymarket_bot.main import (
    _position_from_live_api,
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


if __name__ == "__main__":
    unittest.main()
