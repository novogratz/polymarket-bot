"""Tests for polymarket_bot.weather_forecast — parser + probability model."""
import math
import unittest
from datetime import date
from unittest.mock import patch

from polymarket_bot.weather_forecast import (
    _bracket_yes_prob,
    _normal_cdf,
    forecast_outcome_probability,
    parse_weather_question,
)


class TestNormalCdf(unittest.TestCase):
    def test_symmetry(self):
        self.assertAlmostEqual(_normal_cdf(0), 0.5, places=6)

    def test_positive_tail(self):
        # P(Z <= 1.96) ≈ 0.975
        self.assertAlmostEqual(_normal_cdf(1.96), 0.975, places=2)

    def test_negative_tail(self):
        self.assertAlmostEqual(_normal_cdf(-1.96), 0.025, places=2)


class TestBracketYesProb(unittest.TestCase):
    def test_forecast_at_centre(self):
        # Forecast exactly in the middle of a 2°C bracket, σ=2°C
        # P(38 <= N(39, 2) <= 40) — bracket centred on 39°C
        p = _bracket_yes_prob(forecast_c=39.0, sigma_c=2.0, low_c=38.0, high_c=40.0)
        self.assertGreater(p, 0.30)
        self.assertLess(p, 0.50)

    def test_forecast_far_above(self):
        # Forecast 10°C above the bracket → nearly zero probability of landing in it
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
        self.assertAlmostEqual(r["temp_high_c"], 39.0, places=1)  # +1°C for single bracket
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
        q = "Will the highest temperature in Atlantis be 25°C on June 27?"
        self.assertIsNone(parse_weather_question(q))

    def test_no_temperature_returns_none(self):
        q = "Will bitcoin exceed $100,000 on June 27?"
        self.assertIsNone(parse_weather_question(q))

    def test_no_date_returns_none(self):
        q = "Will the highest temperature in Paris be 38°C?"
        self.assertIsNone(parse_weather_question(q))

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
        # Forecast 45°C, bracket [37-38°C] → P(Yes) near 0 → P(No) near 1.0
        parsed = self._make_parsed(temp_low_c=37.0, temp_high_c=38.0)
        with patch("polymarket_bot.weather_forecast.fetch_forecast_temp",
                   return_value=(45.0, 20.0)):
            p_no = forecast_outcome_probability(parsed, "No")
            self.assertIsNotNone(p_no)
            self.assertGreater(p_no, 0.99)

    def test_forecast_below_bracket_no_wins(self):
        # Forecast 25°C, bracket [37-38°C] → P(Yes) near 0 → P(No) near 1.0
        parsed = self._make_parsed(temp_low_c=37.0, temp_high_c=38.0)
        with patch("polymarket_bot.weather_forecast.fetch_forecast_temp",
                   return_value=(25.0, 10.0)):
            p_no = forecast_outcome_probability(parsed, "No")
            self.assertIsNotNone(p_no)
            self.assertGreater(p_no, 0.99)

    def test_forecast_inside_bracket_yes_wins(self):
        # Forecast exactly 37.5°C inside [36-39°C] with tight σ → P(Yes) dominant
        parsed = self._make_parsed(temp_low_c=36.0, temp_high_c=39.0)
        with patch("polymarket_bot.weather_forecast.fetch_forecast_temp",
                   return_value=(37.5, 10.0)):
            p_no = forecast_outcome_probability(parsed, "No")
            p_yes = forecast_outcome_probability(parsed, "Yes")
            self.assertIsNotNone(p_no)
            self.assertIsNotNone(p_yes)
            self.assertGreater(p_yes, p_no)

    def test_outcome_probabilities_sum_to_one(self):
        parsed = self._make_parsed(temp_low_c=37.0, temp_high_c=38.0)
        with patch("polymarket_bot.weather_forecast.fetch_forecast_temp",
                   return_value=(38.5, 20.0)):
            p_no  = forecast_outcome_probability(parsed, "No")
            p_yes = forecast_outcome_probability(parsed, "Yes")
            self.assertIsNotNone(p_no)
            self.assertIsNotNone(p_yes)
            self.assertAlmostEqual(p_no + p_yes, 1.0, places=4)

    def test_api_failure_returns_none(self):
        parsed = self._make_parsed()
        with patch("polymarket_bot.weather_forecast.fetch_forecast_temp",
                   return_value=None):
            result = forecast_outcome_probability(parsed, "No")
            self.assertIsNone(result)

    def test_upper_tail_market(self):
        # "or higher" market: Yes = temp >= low_c
        parsed = self._make_parsed(
            temp_low_c=39.0, temp_high_c=40.0,
            is_upper_tail=True, is_lower_tail=False,
        )
        # Forecast 42°C → very likely to be >= 39 → P(Yes) high → P(No) low
        with patch("polymarket_bot.weather_forecast.fetch_forecast_temp",
                   return_value=(42.0, 15.0)):
            p_no = forecast_outcome_probability(parsed, "No")
            self.assertIsNotNone(p_no)
            self.assertLess(p_no, 0.10)

    def test_lower_tail_market(self):
        # "or lower" market: Yes = temp <= high_c
        parsed = self._make_parsed(
            temp_low_c=15.0, temp_high_c=15.0,
            is_upper_tail=False, is_lower_tail=True,
        )
        # Forecast 10°C → very likely to be <= 15 → P(Yes) high → P(No) low
        with patch("polymarket_bot.weather_forecast.fetch_forecast_temp",
                   return_value=(10.0, 20.0)):
            p_no = forecast_outcome_probability(parsed, "No")
            self.assertIsNotNone(p_no)
            self.assertLess(p_no, 0.10)

    def test_no_target_date_returns_none(self):
        parsed = self._make_parsed(target_date=None)
        result = forecast_outcome_probability(parsed, "No")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
