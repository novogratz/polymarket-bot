import os

os.environ["POLYMARKET_SKIP_DOTENV"] = "1"
for _k in [k for k in os.environ if k.startswith("POLYMARKET_") and k != "POLYMARKET_SKIP_DOTENV"]:
    del os.environ[_k]

import unittest
from datetime import timedelta

from polymarket_bot.config import Settings
from polymarket_bot.models import Candidate, utc_now
from polymarket_bot.portfolio import Portfolio
from polymarket_bot.pricing import ensure_open_positions_in_pool
from polymarket_bot.strategy import build_pricing_candidates


def _candidate(token_id: str, market_id: str = "m1", price: float = 0.5) -> Candidate:
    return Candidate(
        market_id=market_id,
        question="q",
        slug="s",
        end_date=utc_now() + timedelta(hours=10),
        hours_to_close=10.0,
        liquidity=1000.0,
        volume=2000.0,
        outcome="Yes",
        price=price,
        token_id=token_id,
        score=1.0,
        url="https://polymarket.com",
        best_bid=0.49,
        best_ask=0.50,
        tick_size=0.01,
        neg_risk=False,
        accepts_orders=True,
        event_slug="",
    )


class BuildPricingCandidatesTests(unittest.TestCase):
    def test_keeps_market_below_liquidity_and_volume_floor(self):
        market = {
            "id": "low-liq",
            "question": "Q",
            "slug": "low-liq",
            "endDate": (utc_now() + timedelta(hours=2)).isoformat(),
            "liquidity": "10",
            "volume": "20",
            "bestBid": "0.41",
            "bestAsk": "0.43",
            "orderPriceMinTickSize": "0.01",
            "acceptingOrders": True,
            "outcomes": '["Yes","No"]',
            "outcomePrices": '["0.42","0.58"]',
            "clobTokenIds": '["tok-yes","tok-no"]',
        }
        candidates = build_pricing_candidates([market])
        self.assertEqual({c.token_id for c in candidates}, {"tok-yes", "tok-no"})
        self.assertTrue(all(c.score == 0.0 for c in candidates))

    def test_keeps_market_past_horizon(self):
        market = {
            "id": "expired",
            "endDate": (utc_now() - timedelta(hours=1)).isoformat(),
            "liquidity": "5000",
            "volume": "10000",
            "outcomes": '["Yes","No"]',
            "outcomePrices": '["0.3","0.7"]',
            "clobTokenIds": '["exp-yes","exp-no"]',
        }
        candidates = build_pricing_candidates([market])
        self.assertEqual({c.token_id for c in candidates}, {"exp-yes", "exp-no"})

    def test_skips_outcomes_with_boundary_prices(self):
        market = {
            "id": "boundary",
            "endDate": (utc_now() + timedelta(hours=2)).isoformat(),
            "liquidity": "1000",
            "volume": "2000",
            "outcomes": '["Yes","No"]',
            "outcomePrices": '["0.0","1.0"]',
            "clobTokenIds": '["a","b"]',
        }
        self.assertEqual(build_pricing_candidates([market]), [])


class EnsureOpenPositionsInPoolTests(unittest.TestCase):
    def _settings(self) -> Settings:
        return Settings(quiet=True)

    def test_returns_input_when_no_open_positions(self):
        portfolio = Portfolio(cash=100.0, positions=[], pending_orders=[])
        candidates = [_candidate("a")]
        result = ensure_open_positions_in_pool(self._settings(), portfolio, candidates)
        self.assertEqual([c.token_id for c in result], ["a"])

    def test_returns_input_when_all_open_tokens_covered(self):
        portfolio = Portfolio(
            cash=100.0,
            positions=[{"status": "open", "token_id": "a", "stake": 10.0}],
            pending_orders=[],
        )
        candidates = [_candidate("a"), _candidate("b")]
        result = ensure_open_positions_in_pool(self._settings(), portfolio, candidates)
        self.assertEqual([c.token_id for c in result], ["a", "b"])

    def test_fetches_missing_token_via_gamma(self):
        portfolio = Portfolio(
            cash=100.0,
            positions=[
                {"status": "open", "token_id": "a", "stake": 10.0},
                {"status": "open", "token_id": "missing-1", "stake": 5.0},
                {"status": "closed", "token_id": "ignored", "stake": 0.0},
            ],
            pending_orders=[],
        )
        candidates = [_candidate("a")]
        captured: list[list[str]] = []

        class FakeGammaClient:
            def __init__(self, base_url):
                pass

            def get_markets_by_clob_token_ids(self, tokens):
                captured.append(list(tokens))
                return [{
                    "id": "missing-market",
                    "endDate": (utc_now() + timedelta(hours=1)).isoformat(),
                    "liquidity": "0",
                    "volume": "0",
                    "bestBid": "0.59",
                    "bestAsk": "0.61",
                    "orderPriceMinTickSize": "0.01",
                    "outcomes": '["Yes","No"]',
                    "outcomePrices": '["0.6","0.4"]',
                    "clobTokenIds": '["missing-1","missing-2"]',
                }]

        import polymarket_bot.pricing as pricing_module

        original = pricing_module.GammaClient
        try:
            pricing_module.GammaClient = FakeGammaClient
            result = ensure_open_positions_in_pool(self._settings(), portfolio, candidates)
        finally:
            pricing_module.GammaClient = original

        self.assertEqual(captured, [["missing-1"]])
        tokens = {c.token_id for c in result}
        self.assertIn("a", tokens)
        self.assertIn("missing-1", tokens)

    def test_tolerates_gamma_failure(self):
        portfolio = Portfolio(
            cash=100.0,
            positions=[{"status": "open", "token_id": "missing-x", "stake": 5.0}],
            pending_orders=[],
        )
        candidates = [_candidate("a")]

        class FailingGammaClient:
            def __init__(self, base_url):
                pass

            def get_markets_by_clob_token_ids(self, tokens):
                raise RuntimeError("gamma 500")

        import polymarket_bot.pricing as pricing_module

        original = pricing_module.GammaClient
        try:
            pricing_module.GammaClient = FailingGammaClient
            result = ensure_open_positions_in_pool(self._settings(), portfolio, candidates)
        finally:
            pricing_module.GammaClient = original

        self.assertEqual([c.token_id for c in result], ["a"])


if __name__ == "__main__":
    unittest.main()
