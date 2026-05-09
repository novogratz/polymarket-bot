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


from polymarket_bot._ui import _format_summary_line


class FormatSummaryLineTests(unittest.TestCase):
    def setUp(self):
        os.environ["NO_COLOR"] = "1"

    def tearDown(self):
        os.environ.pop("NO_COLOR", None)

    def _payload(self, **overrides):
        base = {
            "tick": 42,
            "started_at": "2026-05-09T18:03:42+00:00",
            "result": {
                "summary": {
                    "cash": 5.10,
                    "invested": 84.33,
                    "unrealized_pnl": 3.40,
                    "equity": 89.43,
                    "open_positions": 8,
                },
                "scan_report": {"opportunities": [{"a": 1}, {"a": 2}]},
            },
        }
        base["result"].update(overrides.pop("result_overrides", {}))
        for k, v in overrides.items():
            base[k] = v
        return base

    def test_full_payload(self):
        line = _format_summary_line(self._payload())
        self.assertIn("#42", line)
        self.assertIn("18:03", line)
        self.assertIn("scan: 2 opps", line)
        self.assertIn("cash $5.10", line)
        self.assertIn("equity $89.43", line)
        self.assertIn("+3.40", line)
        self.assertIn("8 pos", line)

    def test_zero_opportunities(self):
        line = _format_summary_line(self._payload(result_overrides={"scan_report": {"opportunities": []}}))
        self.assertIn("scan: 0 opps", line)

    def test_missing_summary_uses_status_marker(self):
        payload = {
            "tick": 17,
            "started_at": "2026-05-09T07:09:00+00:00",
            "result": {"status": "waiting_for_funds"},
        }
        line = _format_summary_line(payload)
        self.assertIn("#17", line)
        self.assertIn("07:09", line)
        self.assertIn("waiting_for_funds", line)

    def test_missing_summary_and_status_uses_dash(self):
        payload = {
            "tick": 1,
            "started_at": "2026-05-09T07:09:00+00:00",
            "result": {},
        }
        line = _format_summary_line(payload)
        self.assertIn("#1", line)
        self.assertIn("07:09", line)
        self.assertTrue("(no summary)" in line or "—" in line)

    def test_negative_pnl_is_signed(self):
        payload = self._payload()
        payload["result"]["summary"]["unrealized_pnl"] = -2.50
        line = _format_summary_line(payload)
        self.assertIn("-2.50", line)


if __name__ == "__main__":
    unittest.main()
