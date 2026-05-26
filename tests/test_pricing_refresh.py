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


class _FakeClobClient:
    """Stubs the parts of py_clob_client.ClobClient that pricing.py uses."""

    def __init__(self, *, midpoints: dict[str, str] | None = None,
                 prices: dict[str, dict[str, str]] | None = None,
                 raise_on: str | None = None):
        self._mids = midpoints or {}
        self._prices = prices or {}
        self._raise_on = raise_on

    def __call__(self, host):
        # Mimic the class so `ClobClient('https://...')` returns this instance.
        return self

    def get_midpoints(self, params):
        if self._raise_on == "midpoints":
            raise RuntimeError("CLOB midpoints 500")
        return {p.token_id: self._mids[p.token_id] for p in params if p.token_id in self._mids}

    def get_prices(self, params):
        if self._raise_on == "prices":
            raise RuntimeError("CLOB prices 500")
        out: dict[str, dict[str, str]] = {}
        for p in params:
            if p.token_id in self._prices:
                out.setdefault(p.token_id, {})[p.side] = self._prices[p.token_id].get(p.side, "")
        return out


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

    def _patch_clob(self, fake: _FakeClobClient):
        import polymarket_bot.pricing as pricing_module
        orig = pricing_module.ClobClient
        pricing_module.ClobClient = fake
        return pricing_module, orig

    def test_returns_input_when_no_open_positions(self):
        portfolio = Portfolio(cash=100.0, positions=[], pending_orders=[])
        candidates = [_candidate("a")]
        # No CLOB call expected — no open positions.
        result = ensure_open_positions_in_pool(self._settings(), portfolio, candidates)
        self.assertEqual([c.token_id for c in result], ["a"])

    def test_clob_prices_held_position_with_bid_ask(self):
        portfolio = Portfolio(
            cash=100.0,
            positions=[{
                "status": "open", "token_id": "tok-a", "stake": 10.0,
                "market_id": "m1", "outcome": "Yes",
            }],
            pending_orders=[],
        )
        fake = _FakeClobClient(
            midpoints={"tok-a": "0.4275"},
            prices={"tok-a": {"BUY": "0.42", "SELL": "0.43"}},
        )
        mod, orig = self._patch_clob(fake)
        try:
            result = ensure_open_positions_in_pool(self._settings(), portfolio, [])
        finally:
            mod.ClobClient = orig

        # One pricing candidate, with CLOB mid and bid/ask filled.
        priced = [c for c in result if c.token_id == "tok-a"]
        self.assertEqual(len(priced), 1)
        self.assertAlmostEqual(priced[0].price, 0.4275, places=4)
        self.assertAlmostEqual(priced[0].best_bid, 0.42, places=4)
        self.assertAlmostEqual(priced[0].best_ask, 0.43, places=4)
        self.assertAlmostEqual(priced[0].tick_size, 0.01, places=4)
        self.assertEqual(priced[0].score, 0.0)

    def test_clob_pricing_uses_stored_position_tick_size_without_scan_match(self):
        portfolio = Portfolio(
            cash=100.0,
            positions=[{
                "status": "open", "token_id": "tok-a", "stake": 10.0,
                "market_id": "m1", "outcome": "Yes", "tick_size": 0.001,
                "neg_risk": True,
            }],
            pending_orders=[],
        )
        fake = _FakeClobClient(
            midpoints={"tok-a": "0.4275"},
            prices={"tok-a": {"BUY": "0.42", "SELL": "0.43"}},
        )
        mod, orig = self._patch_clob(fake)
        try:
            result = ensure_open_positions_in_pool(self._settings(), portfolio, [])
        finally:
            mod.ClobClient = orig

        priced = next(c for c in result if c.token_id == "tok-a")
        self.assertAlmostEqual(priced.tick_size, 0.001, places=4)
        self.assertTrue(priced.neg_risk)

    def test_clob_pricing_overrides_scan_candidate(self):
        """When the smart-money scan returns a cached price and CLOB
        returns a fresher one, mark_to_market (via by_token last-wins)
        should pick up the CLOB value. We verify that the CLOB
        candidate is appended AFTER the scan one."""
        portfolio = Portfolio(
            cash=100.0,
            positions=[{
                "status": "open", "token_id": "tok-a", "stake": 10.0,
                "market_id": "m1", "outcome": "Yes",
            }],
            pending_orders=[],
        )
        scan = [_candidate("tok-a", price=0.50)]  # Gamma cache
        fake = _FakeClobClient(
            midpoints={"tok-a": "0.55"},
            prices={"tok-a": {"BUY": "0.54", "SELL": "0.56"}},
        )
        mod, orig = self._patch_clob(fake)
        try:
            result = ensure_open_positions_in_pool(self._settings(), portfolio, scan)
        finally:
            mod.ClobClient = orig

        # Last entry for tok-a in the list should be the CLOB one.
        toks = [c for c in result if c.token_id == "tok-a"]
        self.assertEqual(len(toks), 2)
        self.assertAlmostEqual(toks[0].price, 0.50)  # scan
        self.assertAlmostEqual(toks[1].price, 0.55)  # CLOB wins

    def test_clob_pricing_inherits_tick_size_from_scan(self):
        """CLOB endpoints don't return tick_size / neg_risk; when a matching
        scan candidate exists, the CLOB-built candidate must inherit that
        metadata so downstream sells aren't blocked by a missing tick_size.
        """
        portfolio = Portfolio(
            cash=100.0,
            positions=[{
                "status": "open", "token_id": "tok-a", "stake": 10.0,
                "market_id": "m1", "outcome": "Yes",
            }],
            pending_orders=[],
        )
        # Scan candidate carries tick_size=0.005 and neg_risk=True.
        scan_cand = Candidate(
            market_id="m1", question="q", slug="s",
            end_date=utc_now() + timedelta(hours=10), hours_to_close=10.0,
            liquidity=1000.0, volume=2000.0, outcome="Yes", price=0.50,
            token_id="tok-a", score=1.0, url="https://polymarket.com",
            best_bid=0.49, best_ask=0.50, tick_size=0.005, neg_risk=True,
            accepts_orders=True, event_slug="",
        )
        fake = _FakeClobClient(
            midpoints={"tok-a": "0.55"},
            prices={"tok-a": {"BUY": "0.54", "SELL": "0.56"}},
        )
        mod, orig = self._patch_clob(fake)
        try:
            result = ensure_open_positions_in_pool(self._settings(), portfolio, [scan_cand])
        finally:
            mod.ClobClient = orig

        clob_cand = next(c for c in result if c.token_id == "tok-a" and c.score == 0.0)
        self.assertAlmostEqual(clob_cand.tick_size, 0.005)
        self.assertTrue(clob_cand.neg_risk)

    def test_clob_pricing_preserves_stored_neg_risk_when_scan_default_false(self):
        """Quand le scan revient avec neg_risk=False par défaut (champ absent
        côté Gamma) mais que la position a enregistré neg_risk=True à
        l'entrée, le candidat CLOB doit conserver le True stocké — sinon
        un sell ultérieur signerait avec le mauvais exchange."""
        portfolio = Portfolio(
            cash=100.0,
            positions=[{
                "status": "open", "token_id": "tok-a", "stake": 10.0,
                "market_id": "m1", "outcome": "Yes",
                "tick_size": 0.01, "neg_risk": True,
            }],
            pending_orders=[],
        )
        scan_cand = Candidate(
            market_id="m1", question="q", slug="s",
            end_date=utc_now() + timedelta(hours=10), hours_to_close=10.0,
            liquidity=1000.0, volume=2000.0, outcome="Yes", price=0.50,
            token_id="tok-a", score=1.0, url="https://polymarket.com",
            best_bid=0.49, best_ask=0.50, tick_size=0.01, neg_risk=False,
            accepts_orders=True, event_slug="",
        )
        fake = _FakeClobClient(
            midpoints={"tok-a": "0.55"},
            prices={"tok-a": {"BUY": "0.54", "SELL": "0.56"}},
        )
        mod, orig = self._patch_clob(fake)
        try:
            result = ensure_open_positions_in_pool(self._settings(), portfolio, [scan_cand])
        finally:
            mod.ClobClient = orig

        clob_cand = next(c for c in result if c.token_id == "tok-a" and c.score == 0.0)
        self.assertTrue(clob_cand.neg_risk)

    def test_clob_pricing_refreshes_end_date_from_scan(self):
        portfolio = Portfolio(
            cash=100.0,
            positions=[{
                "status": "open", "token_id": "tok-a", "stake": 10.0,
                "market_id": "m1", "outcome": "Yes",
                "end_date": (utc_now() - timedelta(hours=1)).isoformat(),
            }],
            pending_orders=[],
        )
        stale_scan = Candidate(
            market_id="m1", question="q", slug="s",
            end_date=utc_now() + timedelta(hours=5), hours_to_close=5.0,
            liquidity=1000.0, volume=2000.0, outcome="Yes", price=0.50,
            token_id="tok-a", score=1.0, url="https://polymarket.com",
            best_bid=0.49, best_ask=0.50, tick_size=0.01, neg_risk=False,
            accepts_orders=True, event_slug="",
        )
        fake = _FakeClobClient(
            midpoints={"tok-a": "0.55"},
            prices={"tok-a": {"BUY": "0.54", "SELL": "0.56"}},
        )
        mod, orig = self._patch_clob(fake)
        try:
            result = ensure_open_positions_in_pool(self._settings(), portfolio, [stale_scan])
        finally:
            mod.ClobClient = orig

        clob_cand = next(c for c in result if c.token_id == "tok-a" and c.score == 0.0)
        self.assertIsNotNone(clob_cand.end_date)
        assert clob_cand.end_date is not None
        self.assertGreater(clob_cand.end_date, utc_now())

    def test_falls_back_to_gamma_when_clob_has_no_data(self):
        portfolio = Portfolio(
            cash=100.0,
            positions=[{
                "status": "open", "token_id": "missing-1", "stake": 5.0,
                "market_id": "m9", "outcome": "Yes",
            }],
            pending_orders=[],
        )
        # CLOB returns nothing for this token.
        fake_clob = _FakeClobClient(midpoints={}, prices={})

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

        mod, orig_clob = self._patch_clob(fake_clob)
        orig_gamma = mod.GammaClient
        try:
            mod.GammaClient = FakeGammaClient
            result = ensure_open_positions_in_pool(self._settings(), portfolio, [])
        finally:
            mod.ClobClient = orig_clob
            mod.GammaClient = orig_gamma

        self.assertEqual(captured, [["missing-1"]])
        tokens = {c.token_id for c in result}
        self.assertIn("missing-1", tokens)

    def test_tolerates_clob_failure_and_gamma_failure(self):
        portfolio = Portfolio(
            cash=100.0,
            positions=[{
                "status": "open", "token_id": "missing-x", "stake": 5.0,
                "market_id": "m9", "outcome": "Yes",
            }],
            pending_orders=[],
        )
        fake_clob = _FakeClobClient(raise_on="midpoints")

        class FailingGammaClient:
            def __init__(self, base_url):
                pass

            def get_markets_by_clob_token_ids(self, tokens):
                raise RuntimeError("gamma 500")

        mod, orig_clob = self._patch_clob(fake_clob)
        orig_gamma = mod.GammaClient
        try:
            mod.GammaClient = FailingGammaClient
            scan = [_candidate("a")]
            result = ensure_open_positions_in_pool(self._settings(), portfolio, scan)
        finally:
            mod.ClobClient = orig_clob
            mod.GammaClient = orig_gamma

        # Original scan candidates are preserved, no crash.
        self.assertEqual([c.token_id for c in result], ["a"])


if __name__ == "__main__":
    unittest.main()
