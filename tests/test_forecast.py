"""v4 forecasting model + quality scoring + analytics (user 2026-06-21)."""

import unittest
from datetime import timedelta

from polymarket_bot.config import Settings
from polymarket_bot.forecast import (
    DEFAULT_PRIOR_WIN_RATE,
    build_context,
    calibration_table,
    edge,
    evaluate_market,
    max_drawdown,
    predicted_probability,
    price_bucket,
    profit_factor,
    promotion_status,
    quality_score,
    sharpe_ratio,
)
from polymarket_bot.models import utc_now


def _rec(entry, pnl, *, question="Will team reach final?", slug="m", stake=5.0, closed="1"):
    return {"question": question, "slug": slug, "entry_price": entry,
            "realized_pnl": pnl, "stake": stake, "closed_at": closed}


class PriceBucketTests(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(price_bucket(0.80), "0.80-0.85")
        self.assertEqual(price_bucket(0.849), "0.80-0.85")
        self.assertEqual(price_bucket(0.85), "0.85-0.90")
        self.assertEqual(price_bucket(0.93), "0.90-0.94")
        self.assertEqual(price_bucket(0.95), "0.94-0.96")
        self.assertEqual(price_bucket(0.96), "0.94-0.96")  # top bound inclusive
        self.assertIsNone(price_bucket(0.79))
        self.assertIsNone(price_bucket(0.99))


class ForecastModelTests(unittest.TestCase):
    def test_no_history_returns_prior(self):
        self.assertAlmostEqual(predicted_probability("soccer", 0.90, {}, prior=0.95), 0.95)
        self.assertAlmostEqual(edge("soccer", 0.90, {}, prior=0.95), 0.05)

    def test_favorite_has_positive_edge_below_prior_price(self):
        # An ask below the prior win rate is +EV; an ask above it is −EV.
        self.assertGreater(edge("sports", 0.90, {}, prior=0.95), 0)
        self.assertLess(edge("sports", 0.97, {}, prior=0.95), 0)

    def test_bad_history_pulls_prediction_below_price(self):
        # 200 soccer 0.90-0.94 trades, only 40% win → calibrated well below 0.90.
        recs = [_rec(0.91, -5.0, question="Will FC win on date?", slug="x") for _ in range(120)]
        recs += [_rec(0.91, +0.5, question="Will FC win on date?", slug="x") for _ in range(80)]
        table = calibration_table(recs)
        pred = predicted_probability("soccer", 0.91, table, prior=0.95, pseudo_count=20)
        self.assertLess(pred, 0.91)              # below the price → negative edge
        self.assertLess(edge("soccer", 0.91, table, prior=0.95, pseudo_count=20), 0)

    def test_prediction_bounded(self):
        self.assertLessEqual(predicted_probability("x", 0.90, {}, prior=1.5), 1.0)
        self.assertGreaterEqual(predicted_probability("x", 0.90, {}, prior=-1.0), 0.0)


class QualityScoreTests(unittest.TestCase):
    def test_bounded_0_100(self):
        self.assertLessEqual(quality_score(edge_value=1.0, volume_usd=1e9, category_roi=1.0, bucket_roi=1.0), 100.0)
        self.assertGreaterEqual(quality_score(edge_value=-1.0, volume_usd=0, category_roi=-1.0, bucket_roi=-1.0), 0.0)

    def test_monotonic_in_edge_and_volume(self):
        low = quality_score(edge_value=0.02, volume_usd=1000)
        hi = quality_score(edge_value=0.08, volume_usd=1000)
        self.assertGreater(hi, low)
        vlow = quality_score(edge_value=0.05, volume_usd=1000)
        vhi = quality_score(edge_value=0.05, volume_usd=5000)
        self.assertGreater(vhi, vlow)

    def test_positive_history_lifts_score(self):
        base = quality_score(edge_value=0.05, volume_usd=5000, category_roi=0.0, bucket_roi=0.0)
        lifted = quality_score(edge_value=0.05, volume_usd=5000, category_roi=0.08, bucket_roi=0.08)
        self.assertGreater(lifted, base)


class ResolutionSafetyTests(unittest.TestCase):
    def test_clean_markets_score_100(self):
        from polymarket_bot.forecast import resolution_clarity
        for q in (
            "Will the Lakers beat the Celtics?",
            "Will Real Madrid win on 2026-06-21?",
            "Will Trump win the 2026 election?",
            "Bitcoin Up or Down on June 21?",
        ):
            self.assertEqual(resolution_clarity(q), 100.0, q)

    def test_one_strong_marker_drops_below_60(self):
        from polymarket_bot.forecast import resolution_clarity
        for q in (
            "Will X be deemed the winner by the judges?",
            "Will this be considered a major upset?",
            "Will the disputed result stand?",
            "Winner to be determined by the committee",
        ):
            self.assertLess(resolution_clarity(q), 60.0, q)

    def test_two_soft_markers_drop_below_60(self):
        from polymarket_bot.forecast import resolution_clarity
        self.assertLess(resolution_clarity("Will the price be approximately around 100?"), 60.0)

    def test_filter_skips_ambiguous_market(self):
        from datetime import timedelta
        from polymarket_bot.race_strategies import _build_eligible_candidates
        end = (utc_now() + timedelta(hours=2)).isoformat()

        def mkt(q, slug, mid):
            return {
                "id": mid, "question": q, "slug": slug, "endDate": end,
                "acceptingOrders": True, "liquidity": 1500, "volume24hr": 2000,
                "bestBid": 0.88, "bestAsk": 0.90, "orderPriceMinTickSize": 0.01,
                "outcomes": '["Yes", "No"]', "outcomePrices": '["0.9", "0.1"]',
                "clobTokenIds": '["a", "b"]',
            }
        s = Settings(race_min_price=0.80, race_max_price=0.94, race_max_spread=0.04,
                     race_max_hours=4.0, race_min_liquidity_usd=250.0,
                     race_min_volume_24h_usd=1000.0, race_fixed_stake_usd=5.0,
                     unban_all_markets=True, race_min_resolution_clarity=60.0)
        clean = mkt("Will the home team reach the final?", "clean", "c1")
        ambiguous = mkt("Will the result be deemed valid by the judges?", "amb", "a1")
        out = _build_eligible_candidates([clean, ambiguous], s)
        slugs = {c.slug for c, _ in out}
        self.assertIn("clean", slugs)
        self.assertNotIn("amb", slugs)
        # Filter off (0) → ambiguous passes too.
        s_off = Settings(race_min_price=0.80, race_max_price=0.94, race_max_spread=0.04,
                         race_max_hours=4.0, race_min_liquidity_usd=250.0,
                         race_min_volume_24h_usd=1000.0, race_fixed_stake_usd=5.0,
                         unban_all_markets=True, race_min_resolution_clarity=0.0)
        out_off = _build_eligible_candidates([ambiguous], s_off)
        self.assertTrue(out_off)


class AnalyticsTests(unittest.TestCase):
    def test_sharpe_profit_factor_drawdown(self):
        recs = [_rec(0.9, 0.5, closed="1"), _rec(0.9, -5.0, closed="2"), _rec(0.9, 0.5, closed="3")]
        self.assertNotEqual(sharpe_ratio(recs), 0.0)
        self.assertAlmostEqual(profit_factor(recs), round(1.0 / 5.0, 3))
        self.assertEqual(max_drawdown(recs), -5.0)

    def test_sharpe_zero_below_two_trades(self):
        self.assertEqual(sharpe_ratio([_rec(0.9, 0.5)]), 0.0)

    def test_promotion_gate(self):
        winning = [_rec(0.9, 0.5) for _ in range(500)]
        st = promotion_status(winning, min_trades=500, min_roi=0.05)
        self.assertTrue(st["eligible"])
        self.assertEqual(st["trades"], 500)
        # Below sample size → not eligible even if ROI is great.
        self.assertFalse(promotion_status(winning[:499], min_trades=500)["eligible"])
        # Enough trades but negative ROI → not eligible.
        losing = [_rec(0.9, -1.0) for _ in range(500)]
        self.assertFalse(promotion_status(losing, min_trades=500, min_roi=0.05)["eligible"])


class GateWiringTests(unittest.TestCase):
    def _market(self, ask, vol=5000, question="Will team reach final?", slug="m1", mid="m1"):
        end = (utc_now() + timedelta(hours=2)).isoformat()
        return {
            "id": mid, "question": question, "slug": slug, "endDate": end,
            "acceptingOrders": True, "liquidity": 1500, "volume24hr": vol,
            "bestBid": round(ask - 0.02, 2), "bestAsk": ask,
            "orderPriceMinTickSize": 0.01,
            "outcomes": '["Yes", "No"]',
            "outcomePrices": f'["{ask}", "{round(1 - ask, 2)}"]',
            "clobTokenIds": '["a", "b"]',
        }

    def _settings(self, **over):
        base = dict(race_min_price=0.80, race_max_price=0.94, race_max_price_hard_cap=0.96,
                    race_max_spread=0.04, race_max_hours=4.0, race_min_liquidity_usd=250.0,
                    race_min_volume_24h_usd=1000.0, race_fixed_stake_usd=5.0,
                    unban_all_markets=True, race_forecast_prior=0.95,
                    race_preferred_volume_usd=5000.0)
        base.update(over)
        return Settings(**base)

    def test_gates_off_by_default_passes(self):
        from polymarket_bot.race_strategies import _build_eligible_candidates
        # Defaults: race_min_edge / race_min_quality_score are 0 → no gating.
        self.assertEqual(Settings().race_min_edge, 0.0)
        self.assertEqual(Settings().race_min_quality_score, 0.0)
        s = self._settings()
        ctx = build_context([], prior_default=0.95)
        out = _build_eligible_candidates([self._market(0.90)], s, forecast_ctx=ctx)
        self.assertTrue(out)

    def test_edge_gate_filters_thin_edge(self):
        from polymarket_bot.race_strategies import _build_eligible_candidates
        s = self._settings(race_min_edge=0.03)
        ctx = build_context([], prior_default=0.95)
        # ask 0.90 → edge 0.05 ≥ 0.03 → passes.
        self.assertTrue(_build_eligible_candidates([self._market(0.90)], s, forecast_ctx=ctx))
        # ask 0.94 → edge 0.01 < 0.03 → filtered.
        self.assertEqual(_build_eligible_candidates([self._market(0.94)], s, forecast_ctx=ctx), [])

    def test_quality_gate_filters_low_quality(self):
        from polymarket_bot.race_strategies import _build_eligible_candidates
        s = self._settings(race_min_quality_score=70.0)
        ctx = build_context([], prior_default=0.95)
        ev_hi = evaluate_market(category="sports", ask=0.90, volume_usd=5000,
                                question="x", ctx=ctx)
        ev_lo = evaluate_market(category="sports", ask=0.90, volume_usd=1000,
                                question="x", ctx=ctx)
        # Sanity: more volume scores higher.
        self.assertGreater(ev_hi["quality"], ev_lo["quality"])
        # The thin-volume market is below 70 and is filtered.
        self.assertEqual(
            _build_eligible_candidates([self._market(0.90, vol=1000)], s, forecast_ctx=ctx), []
        )


if __name__ == "__main__":
    unittest.main()
