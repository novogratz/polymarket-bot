import os

os.environ["POLYMARKET_SKIP_DOTENV"] = "1"
for _k in [k for k in os.environ if k.startswith("POLYMARKET_") and k != "POLYMARKET_SKIP_DOTENV"]:
    del os.environ[_k]

import unittest
from datetime import timedelta

from polymarket_bot.config import Settings
from polymarket_bot.external_prices import SpotQuote
from polymarket_bot.models import Candidate, utc_now
from polymarket_bot.edge_strategy import (
    _crypto_fair_probability,
    _edge_sell_plan,
    _parse_price_levels,
    find_arb_opportunities,
    find_crypto_edge_opportunities,
    find_near_certainty_opportunities,
    kelly_fraction,
    size_signal,
    EdgeSignal,
    LANE_CRYPTO,
    LANE_NEAR_CERT,
    LANE_SCALP,
)


def _market(
    *,
    qid: str = "m1",
    question: str = "Will it happen?",
    end_hours: float = 2.0,
    liquidity: float = 5000.0,
    volume_24h: float = 1000.0,
    yes_bid: float = 0.40,
    yes_ask: float = 0.42,
    accepting: bool = True,
    event_slug: str | None = None,
) -> dict:
    end_iso = (utc_now() + timedelta(hours=end_hours)).isoformat().replace("+00:00", "Z")
    return {
        "id": qid,
        "question": question,
        "slug": f"slug-{qid}",
        "endDate": end_iso,
        "liquidity": str(liquidity),
        "volume": "10000",
        "volume24hr": volume_24h,
        "outcomes": '["Yes","No"]',
        "outcomePrices": f'["{yes_ask}","{round(1 - yes_ask, 4)}"]',
        "clobTokenIds": f'["yes-{qid}","no-{qid}"]',
        "bestBid": yes_bid,
        "bestAsk": yes_ask,
        "orderPriceMinTickSize": 0.01,
        "acceptingOrders": accepting,
        "oneDayPriceChange": 0.0,
        "events": [{"slug": event_slug or f"event-{qid}"}],
    }


class PriceLevelParserTests(unittest.TestCase):
    def test_band_parsed(self):
        low, high = _parse_price_levels("BTC between $80,000 and $82,000?")
        self.assertEqual((low, high), (80000.0, 82000.0))

    def test_above_parsed(self):
        low, high = _parse_price_levels("BTC above $90,000?")
        self.assertEqual((low, high), (90000.0, None))

    def test_below_parsed(self):
        low, high = _parse_price_levels("ETH below $3000?")
        self.assertEqual((low, high), (None, 3000.0))

    def test_no_threshold(self):
        self.assertEqual(_parse_price_levels("Bitcoin Up or Down"), (None, None))


class CryptoFairProbabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            edge_crypto_annual_vol=0.60,
            edge_crypto_momentum_alpha=4.0,
        )
        self.quote_up = SpotQuote(
            symbol="BTCUSDT", price=80000.0, momentum_5m=0.005, momentum_15m=0.012, fetched_at=0.0
        )
        self.quote_flat = SpotQuote(
            symbol="BTCUSDT", price=80000.0, momentum_5m=0.0, momentum_15m=0.0, fetched_at=0.0
        )

    def test_direction_market_positive_momentum_favors_up(self):
        result = _crypto_fair_probability("Bitcoin Up or Down", "Up", self.quote_up, 2.0, self.settings)
        self.assertIsNotNone(result)
        p, _ = result
        self.assertGreater(p, 0.50)

    def test_direction_market_no_momentum_is_50_50(self):
        result = _crypto_fair_probability(
            "Bitcoin Up or Down", "Up", self.quote_flat, 2.0, self.settings
        )
        self.assertIsNotNone(result)
        p, _ = result
        self.assertAlmostEqual(p, 0.50, places=2)

    def test_band_probability_centered_when_spot_inside(self):
        result = _crypto_fair_probability(
            "Will BTC be between $79,500 and $80,500?", "Yes", self.quote_flat, 2.0, self.settings
        )
        self.assertIsNotNone(result)
        p, _ = result
        # Tight 1k band around 80k spot, 2h horizon, 60% annual vol → small but nonzero.
        self.assertGreater(p, 0.0)
        self.assertLess(p, 1.0)

    def test_below_threshold_probability_above_strike_pushes_down(self):
        # Spot 80k vs strike 75k: P(spot < 75k at 2h) is small.
        result = _crypto_fair_probability(
            "Will BTC be below $75,000?", "Yes", self.quote_flat, 2.0, self.settings
        )
        self.assertIsNotNone(result)
        p, _ = result
        self.assertLess(p, 0.3)


class KellyTests(unittest.TestCase):
    def test_kelly_at_fair_odds_is_zero(self):
        self.assertAlmostEqual(kelly_fraction(0.5, 0.5), 0.0)

    def test_kelly_grows_with_edge(self):
        f = kelly_fraction(0.6, 0.5)
        self.assertGreater(f, 0.0)
        self.assertLess(f, 1.0)

    def test_kelly_clipped_to_zero_on_no_edge(self):
        self.assertEqual(kelly_fraction(0.3, 0.5), 0.0)


class SizingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            edge_kelly_fraction=0.25,
            edge_max_position_pct=0.15,
            edge_arb_max_position_pct=0.25,
            edge_scalp_max_position_pct=0.05,
        )

    def _sig(self, lane: str = LANE_CRYPTO, fair: float = 0.65, ask: float = 0.55, confidence: float = 1.0) -> EdgeSignal:
        return EdgeSignal(
            lane=lane,
            candidate=Candidate(
                market_id="m", question="q", slug="s", end_date=None, hours_to_close=2.0,
                liquidity=1000, volume=1000, outcome="Yes", price=ask, token_id="t",
                score=0.0, url="", best_bid=ask - 0.01, best_ask=ask, tick_size=0.01,
                neg_risk=False, accepts_orders=True, event_slug="",
            ),
            fair_prob=fair, market_price=ask, edge_pct=fair - ask, confidence=confidence,
            stake_usd=0.0, rationale="test",
        )

    def test_size_respects_per_trade_cap(self):
        stake = size_signal(self._sig(fair=0.99, ask=0.10), cash=100, equity=100, settings=self.settings)
        # Huge Kelly would suggest big bet; cap is 15% of equity = $15.
        self.assertLessEqual(stake, 15.0)

    def test_no_edge_yields_zero_stake(self):
        stake = size_signal(self._sig(fair=0.40, ask=0.50), cash=100, equity=100, settings=self.settings)
        self.assertEqual(stake, 0.0)

    def test_scalp_lane_uses_tighter_cap(self):
        stake = size_signal(
            self._sig(lane=LANE_SCALP, fair=0.99, ask=0.10),
            cash=100, equity=100, settings=self.settings,
        )
        # Scalp cap is 5% of equity = $5.
        self.assertLessEqual(stake, 5.0)


class ArbOpportunitiesTests(unittest.TestCase):
    def test_arb_detected_when_sum_under_threshold(self):
        # YES ask 0.45, NO ask 0.50 → sum 0.95 < 0.98 = arb.
        markets = [_market(yes_ask=0.45, yes_bid=0.43)]
        # NO ask = 1 - YES bid = 0.57; YES ask = 0.45; sum = 1.02 — NOT arb.
        # We need to construct via lower bestAsk explicitly. Build a custom market.
        markets[0]["bestBid"] = 0.50
        markets[0]["bestAsk"] = 0.45
        # Now: YES bid=0.50, YES ask=0.45 (illegal in real markets, but for the test:
        # NO bid = 1 - YES ask = 0.55, NO ask = 1 - YES bid = 0.50, sum = 0.45+0.50 = 0.95.
        settings = Settings(
            edge_max_hours=4.0,
            edge_min_liquidity_usd=100,
            edge_min_volume_24h_usd=10,
            edge_arb_fee_buffer=0.02,
        )
        opps = find_arb_opportunities(markets, settings)
        self.assertEqual(len(opps), 1)
        yes, no, edge = opps[0]
        self.assertGreater(edge, 0)

    def test_no_arb_when_sum_above_threshold(self):
        # YES ask 0.55, NO ask 0.55 → 1.10 (no arb).
        markets = [_market()]
        markets[0]["bestBid"] = 0.45
        markets[0]["bestAsk"] = 0.55
        settings = Settings(
            edge_max_hours=4.0,
            edge_min_liquidity_usd=100,
            edge_min_volume_24h_usd=10,
        )
        self.assertEqual(find_arb_opportunities(markets, settings), [])


class NearCertaintyTests(unittest.TestCase):
    def test_detects_favorite_under_max_ask(self):
        markets = [_market(end_hours=1.0)]
        markets[0]["bestBid"] = 0.93
        markets[0]["bestAsk"] = 0.95
        settings = Settings(
            edge_max_hours=4.0,
            edge_min_liquidity_usd=100,
            edge_min_volume_24h_usd=10,
            edge_near_cert_max_hours=2.0,
            edge_near_cert_min_bid=0.92,
            edge_near_cert_max_ask=0.96,
            edge_near_cert_bias_multiplier=1.05,
            edge_fee_pct=0.0,
            edge_min_edge_pct=0.0,
        )
        sigs = find_near_certainty_opportunities(markets, settings)
        self.assertGreaterEqual(len(sigs), 1)
        self.assertEqual(sigs[0].lane, LANE_NEAR_CERT)

    def test_rejects_when_ask_too_high(self):
        markets = [_market(end_hours=1.0)]
        markets[0]["bestBid"] = 0.93
        markets[0]["bestAsk"] = 0.98
        settings = Settings(
            edge_max_hours=4.0,
            edge_min_liquidity_usd=100,
            edge_min_volume_24h_usd=10,
            edge_near_cert_max_hours=2.0,
            edge_near_cert_min_bid=0.92,
            edge_near_cert_max_ask=0.96,
            edge_near_cert_bias_multiplier=1.05,
        )
        self.assertEqual(find_near_certainty_opportunities(markets, settings), [])

    def test_bias_multiplier_of_one_disables_lane(self):
        markets = [_market(end_hours=1.0)]
        markets[0]["bestBid"] = 0.93
        markets[0]["bestAsk"] = 0.95
        settings = Settings(
            edge_max_hours=4.0,
            edge_min_liquidity_usd=100,
            edge_min_volume_24h_usd=10,
            edge_near_cert_max_hours=2.0,
            edge_near_cert_min_bid=0.92,
            edge_near_cert_max_ask=0.96,
            edge_near_cert_bias_multiplier=1.0,  # no bias → fair = bid → edge negative
            edge_fee_pct=0.02,
            edge_min_edge_pct=0.04,
        )
        self.assertEqual(find_near_certainty_opportunities(markets, settings), [])


class CryptoEdgeIntegrationTests(unittest.TestCase):
    @unittest.skip("crypto banned 2026-06-03")
    def test_filters_to_crypto_markets_only(self):
        # NOTE: "Up or Down" markets are now blanket-excluded via
        # is_excluded_market (low liquidity / FOK bounces). Use a
        # threshold-style question instead so the test exercises the
        # crypto filter rather than the exclusion logic.
        markets = [
            _market(qid="btc1", question="Bitcoin above $79,000 by 8AM ET", yes_ask=0.40, yes_bid=0.38),
            _market(qid="other", question="Will Trump tweet today?", yes_ask=0.40, yes_bid=0.38),
        ]
        # Spot $80k vs threshold $79k → ~60% Yes probability via BS model.
        quote = SpotQuote(symbol="BTCUSDT", price=80000.0, momentum_5m=0.01, momentum_15m=0.02, fetched_at=0.0)
        settings = Settings(
            edge_max_hours=4.0,
            edge_min_liquidity_usd=100,
            edge_min_volume_24h_usd=10,
            edge_min_price=0.05,
            edge_max_price=0.95,
            edge_max_spread=0.10,
            edge_fee_pct=0.0,
            edge_min_edge_pct=0.01,
            edge_crypto_annual_vol=0.60,
            edge_crypto_momentum_alpha=4.0,
        )
        sigs = find_crypto_edge_opportunities(markets, settings, {"BTC": quote})
        # Should produce at least one BTC signal, zero non-crypto signals.
        self.assertGreater(len(sigs), 0)
        for sig in sigs:
            self.assertEqual(sig.extra["asset"], "BTC")


class ExitPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            edge_take_profit_pct=0.25,
            edge_stop_loss_pct=0.25,
            edge_stop_loss_min_age_minutes=3,
            edge_tight_stop_hours=1.0,
            edge_tight_stop_pct=0.15,
            edge_very_tight_stop_hours=0.5,
            edge_very_tight_stop_pct=0.10,
            edge_near_expiry_minutes=5,
            edge_scalp_tp_pct=0.03,
            edge_scalp_sl_pct=0.05,
            edge_scalp_max_age_minutes=15,
        )

    def _position(self, *, lane: str = LANE_CRYPTO, age_min: float = 30.0, end_min: float = 120.0) -> dict:
        opened = utc_now() - timedelta(minutes=age_min)
        end_at = utc_now() + timedelta(minutes=end_min)
        return {
            "status": "open",
            "live": True,
            "shares": 10.0,
            "entry_price": 0.50,
            "opened_at": opened.isoformat().replace("+00:00", "Z"),
            "end_date": end_at.isoformat().replace("+00:00", "Z"),
            "edge_lane": lane,
        }

    def test_crypto_tp_at_25_pct(self):
        plan = _edge_sell_plan(self._position(), 0.30, self.settings)
        self.assertEqual(plan["reason"], "edge_take_profit")

    def test_scalp_lane_has_tight_tp(self):
        plan = _edge_sell_plan(self._position(lane=LANE_SCALP), 0.04, self.settings)
        self.assertEqual(plan["reason"], "edge_scalp_tp")

    def test_scalp_lane_timeout(self):
        plan = _edge_sell_plan(self._position(lane=LANE_SCALP, age_min=20.0), 0.0, self.settings)
        self.assertEqual(plan["reason"], "edge_scalp_timeout")

    def test_arb_lane_holds_position(self):
        plan = _edge_sell_plan(self._position(lane="arb", end_min=120.0), 0.05, self.settings)
        self.assertIsNone(plan)

    def test_arb_lane_flushes_at_expiry_if_positive(self):
        plan = _edge_sell_plan(self._position(lane="arb", end_min=2.0), 0.05, self.settings)
        self.assertEqual(plan["reason"], "edge_arb_expiry_flush")

    def test_adaptive_stop_inside_30min(self):
        # 20 min left → very_tight_stop_pct = 0.10
        plan = _edge_sell_plan(self._position(end_min=20.0), -0.11, self.settings)
        self.assertEqual(plan["reason"], "edge_stop_loss")


if __name__ == "__main__":
    unittest.main()
