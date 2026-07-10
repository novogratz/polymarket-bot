"""v4 category classification + data-driven auto-disable (user 2026-06-21)."""

import unittest
from datetime import timedelta

from polymarket_bot.categories import (
    classify_category,
    category_stats,
    disabled_categories,
)
from polymarket_bot.config import Settings
from polymarket_bot.models import utc_now


class ClassifyCategoryTests(unittest.TestCase):
    def test_each_category(self):
        cases = [
            ("Bitcoin Up or Down on June 21?", "bitcoin-up-or-down", "crypto"),
            ("Will ETH close above $4000?", "ethereum-4000", "crypto"),
            ("Will the Fed cut interest rates in July?", "fed-rate-cut", "economics"),
            ("US CPI inflation above 3%?", "cpi-inflation", "economics"),
            ("Will Trump win the 2026 election?", "trump-2026-election", "politics"),
            ("California Governor primary winner?", "ca-governor-primary", "politics"),
            ("UFC 320: Jones vs Aspinall", "ufc-320-jones-aspinall", "ufc"),
            ("Will Rory McIlroy win the Masters?", "golf-masters-mcilroy", "golf"),
            ("Will Real Madrid FC win on 2026-06-21?", "real-madrid-win", "soccer"),
            ("Premier League: Arsenal vs Chelsea", "epl-arsenal-chelsea", "soccer"),
            ("Lakers vs Celtics NBA Finals Game 7", "nba-lakers-celtics", "sports"),
            ("Will the movie win Best Picture at the Oscars?", "oscars-best-picture", "entertainment"),
            # Weather is its own category since 2026-07-10 (weather-only lane
            # must report under its own bucket, not the catch-all).
            ("Will it rain in Paris tomorrow?", "paris-weather", "weather"),
            ("Highest temperature in NYC on July 9?", "highest-temperature-nyc-july-9", "weather"),
            ("Will it reach 100°F in Phoenix today?", "phoenix-100f", "weather"),
            ("Total rainfall in London this week above 20mm?", "london-rainfall", "weather"),
        ]
        for question, slug, expected in cases:
            self.assertEqual(classify_category(question, slug), expected, question)

    def test_weather_beats_generic_sports_vs(self):
        # "Higher temperature: Miami vs Dallas?" must classify weather, not
        # the generic sports "vs" rule — weather is checked first.
        self.assertEqual(
            classify_category("Higher temperature: Miami vs Dallas?", "temp-miami-dallas"),
            "weather",
        )

    def test_classification_order_crypto_beats_sports_vs(self):
        # "Bitcoin ... vs ..." must classify crypto, not the generic sports vs.
        self.assertEqual(
            classify_category("Bitcoin vs Ethereum: which is higher?", "btc-vs-eth"),
            "crypto",
        )

    def test_always_returns_a_known_category(self):
        from polymarket_bot.categories import CATEGORIES
        self.assertIn(classify_category("", ""), CATEGORIES)
        self.assertIn(classify_category("random gibberish zxcv", "qwer"), CATEGORIES)


class CategoryAutoDisableTests(unittest.TestCase):
    def _recs(self, n, pnl, stake=5.0, question="Bitcoin Up or Down?", slug="btc"):
        return [
            {"question": question, "slug": slug, "realized_pnl": pnl, "stake": stake}
            for _ in range(n)
        ]

    def test_roi_computation(self):
        stats = category_stats(self._recs(10, -1.0, stake=5.0))
        self.assertEqual(stats["crypto"]["trades"], 10)
        self.assertEqual(stats["crypto"]["total_pnl"], -10.0)
        self.assertEqual(stats["crypto"]["total_cost"], 50.0)
        self.assertEqual(stats["crypto"]["roi"], -0.2)

    def test_not_disabled_below_min_samples(self):
        # 99 losing crypto trades — below the 100 sample floor → not disabled.
        self.assertEqual(
            disabled_categories(self._recs(99, -2.0), min_samples=100, roi_threshold=-0.05),
            set(),
        )

    def test_disabled_at_sample_floor_when_roi_below_threshold(self):
        self.assertEqual(
            disabled_categories(self._recs(100, -2.0), min_samples=100, roi_threshold=-0.05),
            {"crypto"},
        )

    def test_not_disabled_when_roi_above_threshold(self):
        # 100 winning crypto trades → ROI positive → never disabled.
        self.assertEqual(
            disabled_categories(self._recs(100, +0.5), min_samples=100, roi_threshold=-0.05),
            set(),
        )

    def test_other_never_disabled(self):
        recs = self._recs(200, -3.0, question="Will it rain in Paris?", slug="paris")
        self.assertEqual(category_stats(recs).get("other", {}).get("trades"), 200)
        self.assertEqual(disabled_categories(recs, min_samples=100), set())

    def test_zero_min_samples_disables_nothing(self):
        self.assertEqual(disabled_categories(self._recs(100, -5.0), min_samples=0), set())


class CategoryGateTests(unittest.TestCase):
    """The auto-disabled set drops markets from entry selection."""

    def _market(self, ask, question, slug, mid):
        end = (utc_now() + timedelta(hours=2)).isoformat()
        return {
            "id": mid, "question": question, "slug": slug, "endDate": end,
            "acceptingOrders": True, "liquidity": 1500, "volume24hr": 2000,
            "bestBid": round(ask - 0.02, 2), "bestAsk": ask,
            "orderPriceMinTickSize": 0.01,
            "outcomes": '["Yes", "No"]',
            "outcomePrices": f'["{ask}", "{round(1 - ask, 2)}"]',
            "clobTokenIds": '["tok-a", "tok-b"]',
        }

    def test_disabled_category_dropped_others_kept(self):
        from polymarket_bot.race_strategies import _build_eligible_candidates
        s = Settings(race_min_price=0.80, race_max_price=0.94,
                     race_max_price_hard_cap=0.96, race_max_spread=0.04,
                     race_max_hours=4.0, race_min_liquidity_usd=250.0,
                     race_min_volume_24h_usd=1000.0, race_fixed_stake_usd=5.0,
                     unban_all_markets=True)
        crypto = self._market(0.90, "Bitcoin Up or Down on June 21?", "btc-updown", "btc")
        sports = self._market(0.90, "Lakers vs Celtics: who wins Game 7?", "nba-game7", "nba")
        # With crypto disabled, only the sports market survives.
        out = _build_eligible_candidates(
            [crypto, sports], s, disabled_categories={"crypto"}
        )
        slugs = {c.slug for c, _ in out}
        self.assertIn("nba-game7", slugs)
        self.assertNotIn("btc-updown", slugs)
        # No disabled set → both survive.
        out_all = _build_eligible_candidates([crypto, sports], s)
        self.assertEqual(len({c.slug for c, _ in out_all}), 2)


if __name__ == "__main__":
    unittest.main()
