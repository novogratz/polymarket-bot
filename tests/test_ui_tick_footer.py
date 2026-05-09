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


if __name__ == "__main__":
    unittest.main()
