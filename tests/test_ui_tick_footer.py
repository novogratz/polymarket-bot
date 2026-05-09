"""Tests for the human-readable tick footer used in POLYMARKET_QUIET mode."""

from __future__ import annotations

import os
import unittest

from polymarket_bot._ui import _truncate_question


class TruncateQuestionTests(unittest.TestCase):
    def test_short_question_unchanged(self):
        self.assertEqual(_truncate_question("Trump wins NH"), "Trump wins NH")

    def test_exact_length_unchanged(self):
        s = "x" * 40
        self.assertEqual(_truncate_question(s), s)

    def test_long_question_truncated_with_ellipsis(self):
        s = "x" * 60
        result = _truncate_question(s)
        self.assertEqual(len(result), 40)
        self.assertTrue(result.endswith("…"))

    def test_empty_string_returns_dash(self):
        self.assertEqual(_truncate_question(""), "—")

    def test_none_returns_dash(self):
        self.assertEqual(_truncate_question(None), "—")

    def test_custom_max_len(self):
        self.assertEqual(_truncate_question("hello world", max_len=5), "hell…")


from polymarket_bot._ui import _format_time_hhmm


class FormatTimeHhmmTests(unittest.TestCase):
    def test_iso_with_offset(self):
        # "2026-05-09T18:03:42+00:00" -> "18:03" (UTC, no local conversion)
        self.assertEqual(_format_time_hhmm("2026-05-09T18:03:42+00:00"), "18:03")

    def test_iso_without_offset_treated_as_utc(self):
        self.assertEqual(_format_time_hhmm("2026-05-09T07:09:00"), "07:09")

    def test_invalid_returns_question_marks(self):
        self.assertEqual(_format_time_hhmm("not a date"), "??:??")

    def test_none_returns_question_marks(self):
        self.assertEqual(_format_time_hhmm(None), "??:??")

    def test_empty_returns_question_marks(self):
        self.assertEqual(_format_time_hhmm(""), "??:??")


if __name__ == "__main__":
    unittest.main()
