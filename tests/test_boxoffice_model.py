"""Tests for polymarket_bot/boxoffice_model.py — the box-office lane's model.

Parser tests are pure/offline; probability tests patch the The Numbers fetch
layer with synthetic film data so nothing touches the network.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from polymarket_bot import boxoffice_model
from polymarket_bot.boxoffice_model import (
    boxoffice_outcome_probability,
    parse_boxoffice_question,
)


class TestParseBoxofficeQuestion(unittest.TestCase):
    def test_between_bracket(self):
        p = parse_boxoffice_question('Will "The Odyssey" 2nd Weekend Box Office be between 86m and 92m?')
        self.assertIsNotNone(p)
        self.assertEqual(p["title"], "The Odyssey")
        self.assertEqual(p["week"], 2)
        self.assertEqual((p["lo"], p["hi"]), (86e6, 92e6))

    def test_less_than_bracket(self):
        p = parse_boxoffice_question('Will "The Odyssey" 2nd Weekend Box Office be less than 74m?')
        self.assertEqual((p["lo"], p["hi"]), (0.0, 74e6))

    def test_at_least_and_greater_than_are_open_top(self):
        p1 = parse_boxoffice_question('Will "Minions & Monsters" 4th Weekend Box Office be at least 10m?')
        p2 = parse_boxoffice_question(
            'Will "Spider-Man: Brand New Day" Opening Weekend Box Office be greater than 280m?')
        self.assertEqual((p1["lo"], p1["hi"]), (10e6, None))
        self.assertEqual(p1["week"], 4)
        self.assertEqual((p2["lo"], p2["hi"]), (280e6, None))
        self.assertEqual(p2["week"], 1)

    def test_non_boxoffice_returns_none(self):
        self.assertIsNone(parse_boxoffice_question("Will Team A win on 2026-07-27?"))
        self.assertIsNone(parse_boxoffice_question(
            "Will Elon Musk post 240-259 tweets from July 21 to July 28, 2026?"))
        self.assertIsNone(parse_boxoffice_question(""))

    def test_inverted_between_returns_none(self):
        self.assertIsNone(parse_boxoffice_question(
            'Will "The Odyssey" 2nd Weekend Box Office be between 92m and 86m?'))


def _rows_week1_final_friday2_estimate():
    """Synthetic film: W1 weekend final $123.5M; W2 Friday estimate $25.8M."""
    w1_fri = datetime(2026, 7, 17, tzinfo=timezone.utc)
    return (
        ("daily", w1_fri, 50_977_435.0, False),
        ("weekend", datetime(2026, 7, 19, tzinfo=timezone.utc), 123_502_900.0, False),
        ("daily", datetime(2026, 7, 24, tzinfo=timezone.utc), 25_800_000.0, True),
    )


class TestBoxofficeOutcomeProbability(unittest.TestCase):
    def _prob(self, question, outcome, rows):
        parsed = parse_boxoffice_question(question)
        with patch.object(boxoffice_model, "_tn_slug_for_title", return_value="Odyssey-The-(2026)"), \
             patch.object(boxoffice_model, "_tn_movie_rows", return_value=rows):
            return boxoffice_outcome_probability(parsed, outcome)

    def test_mid_weekend_friday_multiplier(self):
        # W2 Friday $25.8M × 3.35 → μ ≈ $86.4M: the 86-92m bracket is live,
        # a far bracket is a near-certain No, and Yes+No sum to 1.
        rows = _rows_week1_final_friday2_estimate()
        q_near = 'Will "The Odyssey" 2nd Weekend Box Office be between 86m and 92m?'
        q_far = 'Will "The Odyssey" 2nd Weekend Box Office be between 20m and 30m?'
        p_near_yes = self._prob(q_near, "Yes", rows)
        p_near_no = self._prob(q_near, "No", rows)
        self.assertIsNotNone(p_near_yes)
        self.assertAlmostEqual(p_near_yes + p_near_no, 1.0, places=6)
        self.assertGreater(p_near_yes, 0.2)
        self.assertGreater(self._prob(q_far, "No", rows), 0.99)

    def test_published_weekend_tightens_the_distribution(self):
        # Once the W2 weekend estimate ($87.0M) is published, the containing
        # bracket concentrates hard.
        rows = _rows_week1_final_friday2_estimate() + (
            ("weekend", datetime(2026, 7, 26, tzinfo=timezone.utc), 87_000_000.0, True),
        )
        p = self._prob('Will "The Odyssey" 2nd Weekend Box Office be between 86m and 92m?', "Yes", rows)
        self.assertGreater(p, 0.6)

    def test_pre_weekend_uses_holdover_drop(self):
        # Only W1 known: μ = 123.5M × 0.70 ≈ 86.5M with wide σ.
        rows = tuple(r for r in _rows_week1_final_friday2_estimate() if r[1].day != 24)
        p = self._prob('Will "The Odyssey" 2nd Weekend Box Office be between 80m and 92m?', "Yes", rows)
        self.assertIsNotNone(p)
        self.assertGreater(p, 0.1)

    def test_opening_weekend_is_never_priced(self):
        parsed = parse_boxoffice_question(
            'Will "Spider-Man: Brand New Day" Opening Weekend Box Office be greater than 280m?')
        self.assertIsNone(boxoffice_outcome_probability(parsed, "Yes"))

    def test_unknown_film_returns_none(self):
        parsed = parse_boxoffice_question(
            'Will "The Odyssey" 2nd Weekend Box Office be between 86m and 92m?')
        with patch.object(boxoffice_model, "_tn_slug_for_title", return_value=None):
            self.assertIsNone(boxoffice_outcome_probability(parsed, "No"))

    def test_unknown_outcome_returns_none(self):
        rows = _rows_week1_final_friday2_estimate()
        self.assertIsNone(self._prob(
            'Will "The Odyssey" 2nd Weekend Box Office be between 86m and 92m?', "Over", rows))


if __name__ == "__main__":
    unittest.main()
