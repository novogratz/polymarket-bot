"""Tests for the favorite-dip lane (bot 2's third trigger).

fetch_dip_signals buys a strong favorite that just dropped into the buy band
but is still alive (e.g. a soccer Under 4.5 that fell at halftime, or a
politics market like 0.85 -> 0.70).
"""

import os
os.environ["POLYMARKET_SKIP_DOTENV"] = "1"

import unittest
from dataclasses import replace

from polymarket_bot.config import Settings
from polymarket_bot.models import Candidate
from polymarket_bot.smart_money import fetch_dip_signals


def _cand(token, *, ask, bid=None, hour_change=0.0, day_change=0.0):
    bid = bid if bid is not None else round(ask - 0.01, 4)
    return Candidate(
        market_id="m-" + token, question="Will X?", slug="x", end_date=None,
        hours_to_close=3.0, liquidity=5000.0, volume=4000.0, outcome="Yes",
        price=ask, token_id=token, score=0.0, url="https://polymarket.com",
        best_bid=bid, best_ask=ask, tick_size=0.01, accepts_orders=True,
        one_day_change=day_change, one_hour_change=hour_change,
    )


class FavoriteDipTests(unittest.TestCase):
    def _settings(self, **over):
        base = dict(
            smart_dip_buy_enabled=True,
            smart_dip_min_price=0.60,
            smart_dip_max_price=0.85,
            smart_dip_reference_min=0.84,
            smart_dip_use_day_change=False,
            smart_max_spread=0.10,
        )
        base.update(over)
        return replace(Settings(), **base)

    def test_disabled_returns_empty(self):
        s = self._settings(smart_dip_buy_enabled=False)
        self.assertEqual(fetch_dip_signals(s, [_cand("a", ask=0.70, hour_change=-0.15)]), [])

    def test_dropped_favorite_qualifies(self):
        # 0.85 -> 0.70 (prior 0.85 >= 0.84). The Drummond case.
        s = self._settings()
        sigs = fetch_dip_signals(s, [_cand("a", ask=0.70, hour_change=-0.15)])
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].source, "favorite_dip")
        self.assertAlmostEqual(sigs[0].avg_copy_price, 0.85)

    def test_ask_above_band_skipped(self):
        s = self._settings()
        self.assertEqual(fetch_dip_signals(s, [_cand("a", ask=0.90, hour_change=-0.05)]), [])

    def test_ask_below_band_skipped(self):
        s = self._settings()
        self.assertEqual(fetch_dip_signals(s, [_cand("a", ask=0.55, hour_change=-0.40)]), [])

    def test_did_not_drop_skipped(self):
        s = self._settings()
        self.assertEqual(fetch_dip_signals(s, [_cand("a", ask=0.70, hour_change=+0.05)]), [])

    def test_prior_below_reference_skipped(self):
        # ask 0.65, dropped only 0.05 -> prior 0.70 < 0.84: not a strong favorite.
        s = self._settings()
        self.assertEqual(fetch_dip_signals(s, [_cand("a", ask=0.65, hour_change=-0.05)]), [])

    def test_wide_spread_skipped(self):
        s = self._settings(smart_max_spread=0.03)
        self.assertEqual(
            fetch_dip_signals(s, [_cand("a", ask=0.70, bid=0.60, hour_change=-0.15)]), []
        )

    def test_day_change_mode(self):
        s = self._settings(smart_dip_use_day_change=True)
        # 1h flat, but 24h shows the drop.
        sigs = fetch_dip_signals(s, [_cand("a", ask=0.72, hour_change=0.0, day_change=-0.16)])
        self.assertEqual(len(sigs), 1)
        self.assertAlmostEqual(sigs[0].avg_copy_price, 0.88)

    def test_no_bid_skipped(self):
        # Phantom one-sided book (no bid) must not be bought.
        s = self._settings()
        c = _cand("a", ask=0.70, hour_change=-0.15)
        c = replace(c, best_bid=None)
        self.assertEqual(fetch_dip_signals(s, [c]), [])

    def test_collapsed_bid_skipped(self):
        # Ask shows a dip but the bid has collapsed (< floor 0.50) → skip.
        s = self._settings(smart_max_spread=0.40)
        c = _cand("a", ask=0.70, bid=0.40, hour_change=-0.15)
        self.assertEqual(fetch_dip_signals(s, [c]), [])

    def test_biggest_drop_first(self):
        s = self._settings()
        sigs = fetch_dip_signals(s, [
            _cand("small", ask=0.82, hour_change=-0.04),   # prior 0.86, drop 0.04
            _cand("big", ask=0.65, hour_change=-0.25),     # prior 0.90, drop 0.25
        ])
        self.assertEqual([x.candidate.token_id for x in sigs], ["big", "small"])


if __name__ == "__main__":
    unittest.main()
