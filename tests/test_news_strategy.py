import os

os.environ["POLYMARKET_SKIP_DOTENV"] = "1"
for _k in [k for k in os.environ if k.startswith("POLYMARKET_") and k != "POLYMARKET_SKIP_DOTENV"]:
    del os.environ[_k]

import unittest
from datetime import timedelta

from polymarket_bot.config import Settings
from polymarket_bot.models import utc_now
from polymarket_bot.news_strategy import (
    _build_news_candidates,
    _news_sell_plan,
    _position_age_minutes,
)


def _market(
    *,
    end_hours: float = 2.0,
    liquidity: float = 1500.0,
    volume_24h: float = 500.0,
    best_bid: float = 0.40,
    best_ask: float = 0.42,
    one_day_change: float = 0.05,
    accepting: bool = True,
    outcomes: list[str] | None = None,
    prices: list[str] | None = None,
    market_id: str = "m1",
) -> dict:
    end_iso = (utc_now() + timedelta(hours=end_hours)).isoformat().replace("+00:00", "Z")
    return {
        "id": market_id,
        "question": "Will it happen by close?",
        "slug": f"slug-{market_id}",
        "endDate": end_iso,
        "liquidity": str(liquidity),
        "volume": "5000",
        "volume24hr": volume_24h,
        "outcomes": '["Yes","No"]' if outcomes is None else str(outcomes).replace("'", '"'),
        "outcomePrices": '["0.42","0.58"]' if prices is None else str(prices).replace("'", '"'),
        "clobTokenIds": '["yes-token","no-token"]',
        "bestBid": best_bid,
        "bestAsk": best_ask,
        "orderPriceMinTickSize": 0.01,
        "acceptingOrders": accepting,
        "oneDayPriceChange": one_day_change,
        "events": [{"slug": f"event-{market_id}"}],
    }


class NewsCandidateBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            news_max_hours=4.0,
            news_min_hours=0.083,
            news_min_price=0.10,
            news_max_price=0.85,
            news_max_spread=0.04,
            news_max_relative_spread=0.30,
            news_min_liquidity_usd=300.0,
            news_min_volume_24h_usd=200.0,
            news_require_positive_momentum=True,
            news_min_abs_momentum=0.02,
        )

    def test_returns_yes_outcome_on_positive_momentum(self):
        markets = [_market(one_day_change=0.05)]
        scored = _build_news_candidates(markets, self.settings)
        self.assertEqual(len(scored), 1)
        candidate, score = scored[0]
        self.assertEqual(candidate.outcome, "Yes")
        self.assertGreater(score, 0)

    def test_returns_no_outcome_on_negative_momentum_when_required(self):
        # YES momentum negative -> NO momentum positive -> only NO survives.
        markets = [_market(one_day_change=-0.05, best_bid=0.55, best_ask=0.58)]
        scored = _build_news_candidates(markets, self.settings)
        self.assertEqual(len(scored), 1)
        candidate, _ = scored[0]
        self.assertEqual(candidate.outcome, "No")

    def test_filters_out_market_expiring_after_max_hours(self):
        # Hard constraint: end_date > now + max_hours must be rejected.
        markets = [_market(end_hours=6.0)]
        scored = _build_news_candidates(markets, self.settings)
        self.assertEqual(scored, [])

    def test_filters_out_market_expiring_too_soon(self):
        markets = [_market(end_hours=0.01)]  # 36 seconds — below 5min buffer
        scored = _build_news_candidates(markets, self.settings)
        self.assertEqual(scored, [])

    def test_filters_out_low_liquidity(self):
        markets = [_market(liquidity=50)]
        scored = _build_news_candidates(markets, self.settings)
        self.assertEqual(scored, [])

    def test_filters_out_wide_spread(self):
        markets = [_market(best_bid=0.30, best_ask=0.50)]  # 20c spread
        scored = _build_news_candidates(markets, self.settings)
        self.assertEqual(scored, [])

    def test_filters_out_non_accepting(self):
        markets = [_market(accepting=False)]
        scored = _build_news_candidates(markets, self.settings)
        self.assertEqual(scored, [])

    def test_filters_out_low_volume_24h(self):
        markets = [_market(volume_24h=50)]
        scored = _build_news_candidates(markets, self.settings)
        self.assertEqual(scored, [])

    def test_filters_out_below_min_momentum(self):
        markets = [_market(one_day_change=0.005)]  # < 2c threshold
        scored = _build_news_candidates(markets, self.settings)
        self.assertEqual(scored, [])

    def test_filters_out_price_outside_band(self):
        markets = [_market(best_bid=0.05, best_ask=0.07)]  # too cheap
        scored = _build_news_candidates(markets, self.settings)
        self.assertEqual(scored, [])

    def test_can_disable_positive_momentum_requirement(self):
        loose = Settings(
            news_max_hours=4.0,
            news_min_hours=0.083,
            news_min_price=0.10,
            news_max_price=0.85,
            news_max_spread=0.04,
            news_max_relative_spread=0.30,
            news_min_liquidity_usd=300.0,
            news_min_volume_24h_usd=200.0,
            news_require_positive_momentum=False,
            news_min_abs_momentum=0.02,
        )
        markets = [_market(one_day_change=0.05)]
        scored = _build_news_candidates(markets, loose)
        # Both YES (positive momentum) and NO (negative momentum) pass.
        self.assertEqual(len(scored), 2)


class NewsSellPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            news_take_profit_pct=0.25,
            news_stop_loss_pct=0.50,
            news_stop_loss_min_age_minutes=5,
            news_near_expiry_min_profit=0.0,
            news_near_expiry_minutes=5,
        )

    def _position(self, *, age_min: float = 30.0, end_in_minutes: float = 60.0) -> dict:
        opened = utc_now() - timedelta(minutes=age_min)
        end_at = utc_now() + timedelta(minutes=end_in_minutes)
        return {
            "status": "open",
            "live": True,
            "shares": 10.0,
            "entry_price": 0.40,
            "opened_at": opened.isoformat().replace("+00:00", "Z"),
            "end_date": end_at.isoformat().replace("+00:00", "Z"),
        }

    def test_take_profit_triggers_at_threshold(self):
        plan = _news_sell_plan(self._position(), current_pnl_pct=0.30, settings=self.settings)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["reason"], "news_take_profit")

    def test_stop_loss_does_not_fire_when_young(self):
        plan = _news_sell_plan(
            self._position(age_min=1.0), current_pnl_pct=-0.60, settings=self.settings
        )
        self.assertIsNone(plan)

    def test_stop_loss_fires_after_min_age(self):
        plan = _news_sell_plan(
            self._position(age_min=10.0), current_pnl_pct=-0.60, settings=self.settings
        )
        self.assertIsNotNone(plan)
        self.assertEqual(plan["reason"], "news_stop_loss")

    def test_near_expiry_positive_exit(self):
        plan = _news_sell_plan(
            self._position(end_in_minutes=3.0),
            current_pnl_pct=0.05,
            settings=self.settings,
        )
        self.assertIsNotNone(plan)
        self.assertEqual(plan["reason"], "news_near_expiry")

    def test_near_expiry_negative_does_not_force_sell(self):
        plan = _news_sell_plan(
            self._position(end_in_minutes=3.0),
            current_pnl_pct=-0.10,
            settings=self.settings,
        )
        self.assertIsNone(plan)

    def test_position_age_minutes_handles_missing(self):
        self.assertEqual(_position_age_minutes({}), 0.0)


if __name__ == "__main__":
    unittest.main()
