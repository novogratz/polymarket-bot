"""Tests for the live-position sync helpers in polymarket_bot.main."""

import unittest

from polymarket_bot.main import (
    _position_from_live_api,
    _update_position_from_live_api,
)


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


if __name__ == "__main__":
    unittest.main()
