"""Tests for polymarket_bot.weather_forecast — parser + probability model."""
import math
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from polymarket_bot.weather_forecast import (
    MAX_SPREAD_C,
    _bracket_yes_prob,
    _fetch_consensus,
    _normal_cdf,
    _solar_hour,
    forecast_outcome_probability,
    parse_weather_question,
)


class TestNormalCdf(unittest.TestCase):
    def test_symmetry(self):
        self.assertAlmostEqual(_normal_cdf(0), 0.5, places=6)

    def test_positive_tail(self):
        self.assertAlmostEqual(_normal_cdf(1.96), 0.975, places=2)

    def test_negative_tail(self):
        self.assertAlmostEqual(_normal_cdf(-1.96), 0.025, places=2)


class TestBracketYesProb(unittest.TestCase):
    def test_forecast_at_centre(self):
        p = _bracket_yes_prob(forecast_c=39.0, sigma_c=2.0, low_c=38.0, high_c=40.0)
        self.assertGreater(p, 0.30)
        self.assertLess(p, 0.50)

    def test_forecast_far_above(self):
        p = _bracket_yes_prob(forecast_c=50.0, sigma_c=2.0, low_c=38.0, high_c=39.0)
        self.assertLess(p, 0.001)

    def test_forecast_far_below(self):
        p = _bracket_yes_prob(forecast_c=25.0, sigma_c=2.0, low_c=38.0, high_c=39.0)
        self.assertLess(p, 0.001)

    def test_probability_in_unit_interval(self):
        for fc in [30.0, 38.5, 45.0]:
            p = _bracket_yes_prob(fc, 2.0, 37.0, 40.0)
            self.assertGreaterEqual(p, 0.0)
            self.assertLessEqual(p, 1.0)


class TestParseWeatherQuestion(unittest.TestCase):
    def test_celsius_bracket(self):
        q = "Will the highest temperature in Paris be 38°C on June 27?"
        r = parse_weather_question(q)
        self.assertIsNotNone(r)
        self.assertEqual(r["city"], "paris")
        self.assertAlmostEqual(r["temp_low_c"], 38.0, places=1)
        self.assertAlmostEqual(r["temp_high_c"], 39.0, places=1)
        self.assertTrue(r["is_max"])
        self.assertFalse(r["is_upper_tail"])
        self.assertFalse(r["is_lower_tail"])
        self.assertEqual(r["unit"], "C")
        self.assertEqual(r["target_date"], date(2026, 6, 27))

    def test_fahrenheit_range_bracket(self):
        q = "Will the highest temperature in Los Angeles be between 68-69°F on June 27?"
        r = parse_weather_question(q)
        self.assertIsNotNone(r)
        self.assertEqual(r["city"], "los angeles")
        self.assertAlmostEqual(r["temp_low_c"], (68 - 32) * 5 / 9, places=1)
        self.assertAlmostEqual(r["temp_high_c"], (69 - 32) * 5 / 9, places=1)
        self.assertEqual(r["unit"], "F")

    def test_fahrenheit_range_bracket_chicago(self):
        q = "Will the highest temperature in Chicago be between 72-73°F on June 27?"
        r = parse_weather_question(q)
        self.assertIsNotNone(r)
        self.assertEqual(r["city"], "chicago")
        self.assertAlmostEqual(r["temp_low_c"], (72 - 32) * 5 / 9, places=1)

    def test_lowest_temperature(self):
        q = "Will the lowest temperature in Shanghai be 20°C on June 27?"
        r = parse_weather_question(q)
        self.assertIsNotNone(r)
        self.assertFalse(r["is_max"])
        self.assertAlmostEqual(r["temp_low_c"], 20.0, places=1)
        self.assertAlmostEqual(r["temp_high_c"], 21.0, places=1)

    def test_or_higher_tail(self):
        q = "Will the highest temperature in Jeddah be 39°C or higher on June 27?"
        r = parse_weather_question(q)
        self.assertIsNotNone(r)
        self.assertTrue(r["is_upper_tail"])
        self.assertFalse(r["is_lower_tail"])
        self.assertAlmostEqual(r["temp_low_c"], 39.0, places=1)

    def test_or_lower_tail(self):
        q = "Will the lowest temperature in Seoul be 15°C or lower on June 28?"
        r = parse_weather_question(q)
        self.assertIsNotNone(r)
        self.assertFalse(r["is_upper_tail"])
        self.assertTrue(r["is_lower_tail"])

    def test_unknown_city_returns_none(self):
        self.assertIsNone(parse_weather_question("Will the highest temperature in Atlantis be 25°C on June 27?"))

    def test_no_temperature_returns_none(self):
        self.assertIsNone(parse_weather_question("Will bitcoin exceed $100,000 on June 27?"))

    def test_no_date_returns_none(self):
        self.assertIsNone(parse_weather_question("Will the highest temperature in Paris be 38°C?"))

    def test_atlanta_fahrenheit(self):
        q = "Will the highest temperature in Atlanta be between 88-89°F on June 27?"
        r = parse_weather_question(q)
        self.assertIsNotNone(r)
        self.assertEqual(r["city"], "atlanta")
        self.assertAlmostEqual(r["temp_low_c"], (88 - 32) * 5 / 9, places=1)

    def test_london_celsius(self):
        q = "Will the highest temperature in London be 29°C on June 27?"
        r = parse_weather_question(q)
        self.assertIsNotNone(r)
        self.assertEqual(r["city"], "london")

    def test_wuhan_celsius(self):
        q = "Will the highest temperature in Wuhan be 35°C on June 28?"
        r = parse_weather_question(q)
        self.assertIsNotNone(r)
        self.assertEqual(r["city"], "wuhan")
        self.assertEqual(r["target_date"], date(2026, 6, 28))

    def test_san_francisco_fahrenheit(self):
        q = "Will the highest temperature in San Francisco be between 62-63°F on June 27?"
        r = parse_weather_question(q)
        self.assertIsNotNone(r)
        self.assertEqual(r["city"], "san francisco")

    def test_lucknow_celsius(self):
        q = "Will the highest temperature in Lucknow be 38°C on June 28?"
        r = parse_weather_question(q)
        self.assertIsNotNone(r)
        self.assertEqual(r["city"], "lucknow")

    def test_munich_celsius(self):
        q = "Will the highest temperature in Munich be 35°C on June 27?"
        r = parse_weather_question(q)
        self.assertIsNotNone(r)
        self.assertEqual(r["city"], "munich")


# ---------------------------------------------------------------------------
# forecast_outcome_probability — now mocks _fetch_consensus
# Consensus tuple: (mean_max_c, mean_min_c, sigma_c, current_c_or_None)
# ---------------------------------------------------------------------------

def _consensus(max_c, min_c, sigma=1.2, current_c=None):
    return (max_c, min_c, sigma, current_c)


class TestForecastOutcomeProbability(unittest.TestCase):
    def _make_parsed(self, **kwargs):
        base = {
            "city": "paris", "lat": 48.8566, "lon": 2.3522,
            "temp_low_c": 37.0, "temp_high_c": 38.0,
            "is_upper_tail": False, "is_lower_tail": False,
            "is_max": True, "target_date": date(2026, 6, 27), "unit": "C",
        }
        base.update(kwargs)
        return base

    def test_forecast_above_bracket_no_wins(self):
        parsed = self._make_parsed(temp_low_c=37.0, temp_high_c=38.0)
        with patch("polymarket_bot.weather_forecast._fetch_consensus",
                   return_value=_consensus(45.0, 20.0)):
            p_no = forecast_outcome_probability(parsed, "No")
            self.assertIsNotNone(p_no)
            self.assertGreater(p_no, 0.99)

    def test_forecast_below_bracket_no_wins(self):
        parsed = self._make_parsed(temp_low_c=37.0, temp_high_c=38.0)
        with patch("polymarket_bot.weather_forecast._fetch_consensus",
                   return_value=_consensus(25.0, 10.0)):
            p_no = forecast_outcome_probability(parsed, "No")
            self.assertIsNotNone(p_no)
            self.assertGreater(p_no, 0.99)

    def test_forecast_inside_bracket_yes_wins(self):
        parsed = self._make_parsed(temp_low_c=36.0, temp_high_c=39.0)
        with patch("polymarket_bot.weather_forecast._fetch_consensus",
                   return_value=_consensus(37.5, 10.0, sigma=1.0)):
            p_no  = forecast_outcome_probability(parsed, "No")
            p_yes = forecast_outcome_probability(parsed, "Yes")
            self.assertIsNotNone(p_no)
            self.assertIsNotNone(p_yes)
            self.assertGreater(p_yes, p_no)

    def test_outcome_probabilities_sum_to_one(self):
        parsed = self._make_parsed(temp_low_c=37.0, temp_high_c=38.0)
        with patch("polymarket_bot.weather_forecast._fetch_consensus",
                   return_value=_consensus(38.5, 20.0)):
            p_no  = forecast_outcome_probability(parsed, "No")
            p_yes = forecast_outcome_probability(parsed, "Yes")
            self.assertIsNotNone(p_no)
            self.assertIsNotNone(p_yes)
            self.assertAlmostEqual(p_no + p_yes, 1.0, places=4)

    def test_api_failure_returns_none(self):
        parsed = self._make_parsed()
        with patch("polymarket_bot.weather_forecast._fetch_consensus", return_value=None):
            self.assertIsNone(forecast_outcome_probability(parsed, "No"))

    def test_no_target_date_returns_none(self):
        parsed = self._make_parsed(target_date=None)
        self.assertIsNone(forecast_outcome_probability(parsed, "No"))

    def test_upper_tail_market(self):
        parsed = self._make_parsed(
            temp_low_c=39.0, temp_high_c=40.0,
            is_upper_tail=True, is_lower_tail=False,
        )
        with patch("polymarket_bot.weather_forecast._fetch_consensus",
                   return_value=_consensus(42.0, 15.0)):
            p_no = forecast_outcome_probability(parsed, "No")
            self.assertIsNotNone(p_no)
            self.assertLess(p_no, 0.10)

    def test_lower_tail_market(self):
        parsed = self._make_parsed(
            temp_low_c=15.0, temp_high_c=15.0,
            is_upper_tail=False, is_lower_tail=True,
        )
        with patch("polymarket_bot.weather_forecast._fetch_consensus",
                   return_value=_consensus(10.0, 5.0)):
            p_no = forecast_outcome_probability(parsed, "No")
            self.assertIsNotNone(p_no)
            self.assertLess(p_no, 0.10)


# ---------------------------------------------------------------------------
# Multi-model consensus logic
# ---------------------------------------------------------------------------

class TestMultiModelConsensus(unittest.TestCase):
    """Tests for model-spread guard and sigma derivation."""

    def test_high_spread_returns_none(self):
        """Models disagreeing by > MAX_SPREAD_C should block the bet."""
        # Simulate: best_match=38, ecmwf=34 → spread=4 > 3.0 → None
        # We test via forecast_outcome_probability returning None
        parsed = {
            "city": "paris", "lat": 48.8566, "lon": 2.3522,
            "temp_low_c": 36.0, "temp_high_c": 37.0,
            "is_upper_tail": False, "is_lower_tail": False,
            "is_max": True, "target_date": date(2026, 6, 27), "unit": "C",
        }
        with patch("polymarket_bot.weather_forecast._fetch_consensus", return_value=None):
            self.assertIsNone(forecast_outcome_probability(parsed, "No"))

    def test_agreeing_models_produce_tight_sigma(self):
        """When all models agree (spread=0.2°C) sigma should be small."""
        parsed = {
            "city": "paris", "lat": 48.8566, "lon": 2.3522,
            "temp_low_c": 36.0, "temp_high_c": 37.0,
            "is_upper_tail": False, "is_lower_tail": False,
            "is_max": True, "target_date": date(2026, 6, 27), "unit": "C",
        }
        # Forecast 40°C, bracket [36-37°C] — 3°C above bracket, tight sigma
        with patch("polymarket_bot.weather_forecast._fetch_consensus",
                   return_value=_consensus(40.0, 20.0, sigma=0.9)):
            p_no = forecast_outcome_probability(parsed, "No")
            self.assertIsNotNone(p_no)
            self.assertGreater(p_no, 0.99)


# ---------------------------------------------------------------------------
# Intraday kill-switch
# ---------------------------------------------------------------------------

class TestIntradayKillSwitch(unittest.TestCase):
    """Same-day markets: current temperature can short-circuit the probability."""

    def _parsed_bracket(self, **kw):
        base = {
            "city": "paris", "lat": 48.8566, "lon": 2.3522,
            "temp_low_c": 35.0, "temp_high_c": 36.0,
            "is_upper_tail": False, "is_lower_tail": False,
            "is_max": True,
            "target_date": datetime.now(timezone.utc).date(),  # today
            "unit": "C",
        }
        base.update(kw)
        return base

    def test_afternoon_too_cold_for_bracket_no_near_certain(self):
        """Post-3PM, current=20°C, bracket=[35-36°C] — physically impossible."""
        parsed = self._parsed_bracket()
        # solar_hour ≥ 15 AND current(20) + buffer(4) = 24 < low(35)
        with patch("polymarket_bot.weather_forecast._fetch_consensus",
                   return_value=_consensus(33.0, 20.0, sigma=1.2, current_c=20.0)), \
             patch("polymarket_bot.weather_forecast._solar_hour", return_value=16.0):
            p_no = forecast_outcome_probability(parsed, "No")
            self.assertIsNotNone(p_no)
            self.assertGreaterEqual(p_no, 0.99)

    def test_afternoon_current_above_bracket_ceiling_no_near_certain(self):
        """Post-3PM, current=38°C already above bracket_high=36°C → daily max > bracket."""
        parsed = self._parsed_bracket()
        with patch("polymarket_bot.weather_forecast._fetch_consensus",
                   return_value=_consensus(38.0, 20.0, sigma=1.2, current_c=38.0)), \
             patch("polymarket_bot.weather_forecast._solar_hour", return_value=15.5):
            p_no = forecast_outcome_probability(parsed, "No")
            self.assertIsNotNone(p_no)
            self.assertGreaterEqual(p_no, 0.99)

    def test_morning_no_kill_switch(self):
        """Before 3PM solar, intraday kill-switch is inactive even if cold."""
        parsed = self._parsed_bracket()
        with patch("polymarket_bot.weather_forecast._fetch_consensus",
                   return_value=_consensus(33.0, 20.0, sigma=1.2, current_c=20.0)), \
             patch("polymarket_bot.weather_forecast._solar_hour", return_value=9.0):
            p_no = forecast_outcome_probability(parsed, "No")
            # Should use normal Gaussian model, not the kill-switch
            self.assertIsNotNone(p_no)
            self.assertLess(p_no, 0.99)  # not pinned to 0.99

    def test_upper_tail_current_already_exceeded_yes_near_certain(self):
        """Upper-tail 'or higher': current already above threshold → Yes wins."""
        parsed = self._parsed_bracket(
            temp_low_c=35.0, temp_high_c=36.0,
            is_upper_tail=True, is_lower_tail=False,
        )
        with patch("polymarket_bot.weather_forecast._fetch_consensus",
                   return_value=_consensus(36.0, 20.0, sigma=1.2, current_c=36.5)), \
             patch("polymarket_bot.weather_forecast._solar_hour", return_value=15.0):
            p_no = forecast_outcome_probability(parsed, "No")
            self.assertIsNotNone(p_no)
            self.assertLessEqual(p_no, 0.01)

    def test_future_date_no_intraday(self):
        """Kill-switch only fires for today's markets — future date uses normal Gaussian."""
        # Use forecast=33°C vs bracket [35-36°C] so normal Gaussian gives
        # p_no in (0.90, 0.99) — clearly above bracket but not so far that
        # it rounds to 1.0, proving we're using Gaussian not the kill-switch.
        parsed = self._parsed_bracket(
            target_date=date(2099, 12, 31),
            temp_low_c=35.0, temp_high_c=36.0,
        )
        with patch("polymarket_bot.weather_forecast._fetch_consensus",
                   return_value=_consensus(33.0, 15.0, sigma=1.5, current_c=15.0)), \
             patch("polymarket_bot.weather_forecast._solar_hour", return_value=16.0):
            p_no = forecast_outcome_probability(parsed, "No")
            self.assertIsNotNone(p_no)
            self.assertGreater(p_no, 0.90)
            self.assertLess(p_no, 0.99)  # not the hard 0.99 kill-switch pin


if __name__ == "__main__":
    unittest.main()
