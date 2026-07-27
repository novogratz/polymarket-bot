"""Tests for polymarket_bot/tweet_model.py — the tweet-count lane's edge model.

The parser tests are pure/offline. The probability tests patch the xtracker
fetch layer with a synthetic constant-rate account, so nothing touches the
network and the expected counts are known in closed form.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from polymarket_bot import tweet_model
from polymarket_bot.tweet_model import (
    _bracket_prob,
    _nb_pmf,
    parse_tweet_question,
    tweet_outcome_probability,
)


class TestParseTweetQuestion(unittest.TestCase):
    def test_range_bracket(self):
        p = parse_tweet_question(
            "Will Elon Musk post 240-259 tweets from July 21 to July 28, 2026?")
        self.assertIsNotNone(p)
        self.assertEqual(p["person"], "elon musk")
        self.assertEqual((p["lo"], p["hi"]), (240, 259))
        self.assertEqual(p["dates"], [(7, 21), (7, 28)])
        self.assertIsNone(p["month_only"])
        self.assertEqual(p["year"], 2026)

    def test_plus_bracket(self):
        p = parse_tweet_question(
            "Will Elon Musk post 500+ tweets from July 24 to July 31, 2026?")
        self.assertEqual((p["lo"], p["hi"]), (500, None))

    def test_less_than_bracket(self):
        p = parse_tweet_question(
            "Will Elon Musk post <40 tweets from July 25 to July 27, 2026?")
        self.assertEqual((p["lo"], p["hi"]), (0, 39))

    def test_monthly_window(self):
        p = parse_tweet_question("Will Elon Musk post 780-799 tweets in August 2026?")
        self.assertEqual(p["month_only"], 8)
        self.assertEqual((p["lo"], p["hi"]), (780, 799))

    def test_posts_phrasing(self):
        p = parse_tweet_question(
            "Will Zelenskyy make 120-139 posts from July 24 to July 31, 2026?")
        self.assertIsNotNone(p)
        self.assertEqual(p["person"], "zelenskyy")
        self.assertEqual((p["lo"], p["hi"]), (120, 139))

    def test_non_tweet_questions_return_none(self):
        self.assertIsNone(parse_tweet_question("Will Team A win on 2026-07-27?"))
        self.assertIsNone(parse_tweet_question("Will Trump tweet about the Fed this week?"))
        self.assertIsNone(parse_tweet_question(""))

    def test_inverted_range_returns_none(self):
        self.assertIsNone(parse_tweet_question(
            "Will Elon Musk post 259-240 tweets from July 21 to July 28, 2026?"))


class TestNbPmf(unittest.TestCase):
    def test_sums_to_one_and_matches_mean(self):
        pmf = _nb_pmf(mean=50.0, r=10.0, nmax=800)
        self.assertAlmostEqual(sum(pmf), 1.0, places=6)
        mean = sum(k * p for k, p in enumerate(pmf))
        self.assertAlmostEqual(mean, 50.0, delta=0.1)

    def test_zero_mean_is_point_mass(self):
        pmf = _nb_pmf(mean=0.0, r=10.0, nmax=5)
        self.assertEqual(pmf[0], 1.0)
        self.assertEqual(sum(pmf[1:]), 0.0)

    def test_bracket_prob_bounds(self):
        # Already past the bracket: impossible.
        self.assertEqual(_bracket_prob(current=300, mean_rem=10.0, r=20.0, lo=240, hi=259), 0.0)
        # Whole line: certain.
        self.assertAlmostEqual(
            _bracket_prob(current=0, mean_rem=50.0, r=20.0, lo=0, hi=None), 1.0, places=6)
        # Open-ended bracket far above the mean: near zero.
        self.assertLess(_bracket_prob(current=0, mean_rem=50.0, r=20.0, lo=500, hi=None), 0.01)


class _SyntheticFeed:
    """Constant-rate account: one post every 36 min (40/day) for 90 days."""

    def __init__(self):
        self.now = datetime.now(timezone.utc)
        start = self.now - timedelta(days=90)
        step = timedelta(minutes=36)
        stamps = []
        t = start
        while t < self.now:
            stamps.append(t.timestamp())
            t += step
        self.stamps = tuple(stamps)
        # Window: started 2 days ago at 16:00 UTC, runs 7 days.
        ws = (self.now - timedelta(days=2)).replace(hour=16, minute=0, second=0, microsecond=0)
        self.window_start = ws
        self.window_end = ws + timedelta(days=7) - timedelta(seconds=1)
        self.user = {
            "name": "Elon Musk", "handle": "elonmusk",
            "trackings": [{
                "startDate": self.window_start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "endDate": self.window_end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            }],
        }

    def question(self, lo, hi):
        s, e = self.window_start, self.window_end
        months = ("January", "February", "March", "April", "May", "June", "July",
                  "August", "September", "October", "November", "December")
        return (f"Will Elon Musk post {lo}-{hi} tweets from {months[s.month - 1]} {s.day} "
                f"to {months[e.month - 1]} {e.day}, {e.year}?")


class TestTweetOutcomeProbability(unittest.TestCase):
    def setUp(self):
        self.feed = _SyntheticFeed()
        tweet_model._users_cached.cache_clear()
        tweet_model._posts_cached.cache_clear()
        tweet_model._fitted_model.cache_clear()
        self._patchers = [
            patch.object(tweet_model, "_fetch_json", side_effect=self._fetch_json),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        tweet_model._users_cached.cache_clear()
        tweet_model._posts_cached.cache_clear()
        tweet_model._fitted_model.cache_clear()

    def _fetch_json(self, url):
        if url.endswith("/users"):
            return {"success": True, "data": [self.feed.user]}
        if url.endswith("/users/elonmusk/posts"):
            return {"success": True, "data": [
                {"createdAt": datetime.fromtimestamp(ts, tz=timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%S.000Z")}
                for ts in self.feed.stamps
            ]}
        raise AssertionError(f"unexpected fetch: {url}")

    def test_probabilities_are_coherent(self):
        # 40/day constant rate: ~80 posted so far, ~200 more expected → the
        # final count concentrates near 280. A far-away bracket is a
        # near-certain "No"; Yes+No always sum to 1.
        far = parse_tweet_question(self.feed.question(500, 519))
        p_no = tweet_outcome_probability(far, "No")
        p_yes = tweet_outcome_probability(far, "Yes")
        self.assertIsNotNone(p_no)
        self.assertGreater(p_no, 0.95)
        self.assertAlmostEqual(p_no + p_yes, 1.0, places=6)
        # The bracket around the expected count is far more likely than the
        # far bracket.
        near = parse_tweet_question(self.feed.question(260, 299))
        self.assertGreater(tweet_outcome_probability(near, "Yes"), p_yes)

    def test_unmatched_window_returns_none(self):
        # Dates that match no tracking → unpriceable → None (skip).
        parsed = parse_tweet_question(
            "Will Elon Musk post 240-259 tweets from January 1 to January 8, 2020?")
        self.assertIsNone(tweet_outcome_probability(parsed, "No"))

    def test_unknown_outcome_returns_none(self):
        parsed = parse_tweet_question(self.feed.question(240, 259))
        self.assertIsNone(tweet_outcome_probability(parsed, "Over"))

    def test_fetch_failure_returns_none(self):
        parsed = parse_tweet_question(self.feed.question(240, 259))
        with patch.object(tweet_model, "_fetch_json", side_effect=OSError("down")):
            tweet_model._users_cached.cache_clear()
            tweet_model._posts_cached.cache_clear()
            self.assertIsNone(tweet_outcome_probability(parsed, "No"))


class TestRegimeMultiplier(unittest.TestCase):
    def test_missing_file_is_neutral(self):
        with patch.object(tweet_model, "REGIME_FILE", "/nonexistent/regime.json"):
            self.assertEqual(
                tweet_model._regime_multiplier("elonmusk", datetime.now(timezone.utc)), 1.0)

    def _write(self, entry):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump({"elonmusk": entry}, fh)
        return path

    def test_fresh_file_is_used_and_clamped(self):
        now = datetime.now(timezone.utc)
        path = self._write({"multiplier": 5.0, "updated": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
        try:
            with patch.object(tweet_model, "REGIME_FILE", path):
                # 5.0 clamps to the 2.0 ceiling — a bad LLM answer can never
                # more than double the intensity prior.
                self.assertEqual(tweet_model._regime_multiplier("elonmusk", now), 2.0)
        finally:
            os.unlink(path)

    def test_stale_file_is_ignored(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        path = self._write({"multiplier": 1.7, "updated": old})
        try:
            with patch.object(tweet_model, "REGIME_FILE", path):
                self.assertEqual(tweet_model._regime_multiplier("elonmusk", now), 1.0)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
