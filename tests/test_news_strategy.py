import os

os.environ["POLYMARKET_SKIP_DOTENV"] = "1"
for _k in [k for k in os.environ if k.startswith("POLYMARKET_") and k != "POLYMARKET_SKIP_DOTENV"]:
    del os.environ[_k]

import unittest
from datetime import timedelta

from polymarket_bot.config import Settings
from polymarket_bot.models import utc_now
from polymarket_bot.models import Candidate
from polymarket_bot.news_strategy import (
    _adaptive_stop_pct,
    _asset_key,
    _build_news_candidates,
    _conviction_tier,
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
        # PnL between 0 and the adaptive SL: no exit (no flush, no SL).
        plan = _news_sell_plan(
            self._position(end_in_minutes=3.0),
            current_pnl_pct=-0.05,
            settings=self.settings,
        )
        self.assertIsNone(plan)

    def test_position_age_minutes_handles_missing(self):
        self.assertEqual(_position_age_minutes({}), 0.0)


class AssetKeyTests(unittest.TestCase):
    def test_bitcoin_variants_share_key(self):
        self.assertEqual(_asset_key("Bitcoin Up or Down - 8AM ET", ""), "crypto:BTC")
        self.assertEqual(_asset_key("BTC price band", ""), "crypto:BTC")
        # Different question wording, same underlying.
        self.assertEqual(
            _asset_key("Will Bitcoin close above 80k?", "btc-band"),
            _asset_key("Bitcoin Up or Down", "other-slug"),
        )

    def test_ethereum_solana_xrp_detected(self):
        self.assertEqual(_asset_key("Ethereum Up or Down", ""), "crypto:ETH")
        self.assertEqual(_asset_key("Solana 200 price", ""), "crypto:SOL")
        self.assertEqual(_asset_key("XRP rally", ""), "crypto:XRP")

    def test_falls_back_to_event_slug(self):
        self.assertEqual(_asset_key("Dota match outcome", "dota-event-123"), "event:dota-event-123")

    def test_returns_none_without_match_or_slug(self):
        self.assertIsNone(_asset_key("Random question text", ""))


class ConvictionTierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            news_stake_usd=5.0,
            news_max_stake_usd=12.0,
            news_min_stake_usd=3.0,
            news_smart_money_min_flow_usd=250.0,
        )

    def _cand(self, ask: float = 0.50) -> Candidate:
        return Candidate(
            market_id="m1",
            question="q",
            slug="s",
            end_date=None,
            hours_to_close=2.0,
            liquidity=2000.0,
            volume=5000.0,
            outcome="Yes",
            price=ask,
            token_id="t1",
            score=5.0,
            url="",
            best_bid=ask - 0.02,
            best_ask=ask,
            tick_size=0.01,
            neg_risk=False,
            accepts_orders=True,
            event_slug="ev1",
        )

    def test_high_tier_requires_flow_and_either_low_price_or_high_score(self):
        tier, stake = _conviction_tier(
            self._cand(ask=0.30), score=5.0, smart_money_flow_usd=500.0, settings=self.settings
        )
        self.assertEqual(tier, "high")
        self.assertEqual(stake, 12.0)

    def test_mid_tier_on_flow_alone(self):
        tier, stake = _conviction_tier(
            self._cand(ask=0.60), score=3.0, smart_money_flow_usd=300.0, settings=self.settings
        )
        self.assertEqual(tier, "mid")
        self.assertEqual(stake, 5.0)

    def test_low_tier_without_signal(self):
        tier, stake = _conviction_tier(
            self._cand(ask=0.60), score=1.0, smart_money_flow_usd=0.0, settings=self.settings
        )
        self.assertEqual(tier, "low")
        self.assertGreaterEqual(stake, self.settings.news_min_stake_usd)


class AdaptiveStopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            news_stop_loss_pct=0.25,
            news_tight_stop_hours=1.0,
            news_tight_stop_pct=0.15,
            news_very_tight_stop_hours=0.5,
            news_very_tight_stop_pct=0.10,
        )

    def test_far_from_expiry_uses_base(self):
        self.assertAlmostEqual(_adaptive_stop_pct(3.0, self.settings), 0.25)

    def test_inside_tight_window(self):
        self.assertAlmostEqual(_adaptive_stop_pct(0.8, self.settings), 0.15)

    def test_inside_very_tight_window(self):
        self.assertAlmostEqual(_adaptive_stop_pct(0.3, self.settings), 0.10)


class PartialTakeProfitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            news_take_profit_pct=0.25,
            news_partial_tp_fraction=0.50,
            news_trailing_arm_pct=0.35,
            news_trailing_giveback_pct=0.50,
            news_stop_loss_pct=0.25,
            news_stop_loss_min_age_minutes=3,
            news_near_expiry_min_profit=0.0,
            news_near_expiry_minutes=5,
            news_tight_stop_hours=1.0,
            news_tight_stop_pct=0.15,
            news_very_tight_stop_hours=0.5,
            news_very_tight_stop_pct=0.10,
        )

    def _position(self, *, age_min: float = 30.0, end_min: float = 120.0, tier_hit: str = "", peak: float = 0.0) -> dict:
        opened = utc_now() - timedelta(minutes=age_min)
        end_at = utc_now() + timedelta(minutes=end_min)
        return {
            "status": "open",
            "live": True,
            "shares": 10.0,
            "entry_price": 0.40,
            "opened_at": opened.isoformat().replace("+00:00", "Z"),
            "end_date": end_at.isoformat().replace("+00:00", "Z"),
            "news_tier_hit": tier_hit,
            "peak_pnl_pct": peak,
        }

    def test_partial_tp_sells_half(self):
        plan = _news_sell_plan(self._position(), current_pnl_pct=0.30, settings=self.settings)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["reason"], "news_take_profit")
        self.assertEqual(plan["tier"], "tp1")
        self.assertAlmostEqual(plan["shares"], 5.0)

    def test_partial_tp_does_not_refire(self):
        # After tier_hit=tp1, even at +30% we don't TP again.
        plan = _news_sell_plan(
            self._position(tier_hit="tp1", peak=0.30),
            current_pnl_pct=0.30,
            settings=self.settings,
        )
        # Should not retrigger TP; trailing armed at 0.35 not yet hit.
        self.assertIsNone(plan)

    def test_trailing_exits_after_giveback(self):
        # After tier_hit=tp1 and peak +50%, current +20% means giveback
        # to 25% of peak -> trailing should fire.
        plan = _news_sell_plan(
            self._position(tier_hit="tp1", peak=0.50),
            current_pnl_pct=0.15,
            settings=self.settings,
        )
        self.assertIsNotNone(plan)
        self.assertEqual(plan["reason"], "news_trailing_stop")
        self.assertEqual(plan["shares"], 10.0)

    def test_adaptive_stop_fires_near_expiry(self):
        # 20 min to expiry → very_tight_stop_pct = 0.10
        plan = _news_sell_plan(
            self._position(end_min=20.0),
            current_pnl_pct=-0.12,
            settings=self.settings,
        )
        self.assertIsNotNone(plan)
        self.assertEqual(plan["reason"], "news_stop_loss")

    def test_adaptive_stop_does_not_fire_at_base_with_time_left(self):
        # 3h left → base 25%; -12% should NOT fire.
        plan = _news_sell_plan(
            self._position(end_min=180.0),
            current_pnl_pct=-0.12,
            settings=self.settings,
        )
        self.assertIsNone(plan)


if __name__ == "__main__":
    unittest.main()
