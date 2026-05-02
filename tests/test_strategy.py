from datetime import timedelta
import unittest

from polymarket_bot.config import Settings
from polymarket_bot.models import utc_now
from polymarket_bot.strategy import rank_markets, stake_for_candidate


class StrategyTests(unittest.TestCase):
    def test_rank_markets_filters_and_scores_soon_markets(self):
        end_date = (utc_now() + timedelta(hours=12)).isoformat().replace("+00:00", "Z")
        markets = [
            {
                "id": "1",
                "question": "Will test pass?",
                "slug": "will-test-pass",
                "endDate": end_date,
                "liquidity": "1000",
                "volume": "2000",
                "outcomes": '["Yes","No"]',
                "outcomePrices": '["0.62","0.38"]',
                "clobTokenIds": '["yes-token","no-token"]',
            }
        ]
        settings = Settings(min_liquidity_usd=100, min_volume_usd=100, soon_hours=24)
        candidates = rank_markets(markets, settings)

        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0].market_id, "1")
        self.assertIn(candidates[0].outcome, {"Yes", "No"})
        self.assertGreater(candidates[0].score, 0)

    def test_stake_is_capped(self):
        end_date = (utc_now() + timedelta(hours=1)).isoformat()
        candidate = rank_markets(
            [
                {
                    "id": "1",
                    "question": "Q",
                    "slug": "q",
                    "endDate": end_date,
                    "liquidity": "1000",
                    "volume": "2000",
                    "outcomes": '["Yes"]',
                    "outcomePrices": '["0.50"]',
                }
            ],
            Settings(min_liquidity_usd=0, min_volume_usd=0),
        )[0]

        self.assertEqual(stake_for_candidate(candidate, 20.0, Settings(max_position_usd=5.0)), 5.0)


if __name__ == "__main__":
    unittest.main()
