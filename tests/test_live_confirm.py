"""Tests for polymarket_bot.live_confirm : interactive yes/no prompt + recap."""

import os
os.environ["POLYMARKET_SKIP_DOTENV"] = "1"
for _k in [k for k in os.environ if k.startswith("POLYMARKET_") and k != "POLYMARKET_SKIP_DOTENV"]:
    del os.environ[_k]

import io
import json
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.config import Settings
from polymarket_bot.live_confirm import (
    build_live_recap,
    prompt_live_confirmation,
)


class _FakeTTY(io.StringIO):
    """StringIO that reports True from isatty()."""
    def isatty(self) -> bool:
        return True


class _FakeNonTTY(io.StringIO):
    def isatty(self) -> bool:
        return False


class PromptLiveConfirmationTests(unittest.TestCase):
    def test_skip_returns_true_without_prompting(self):
        stdin = _FakeNonTTY("")
        result = prompt_live_confirmation(
            recap_text="(no recap)", skip=True, stdin=stdin
        )
        self.assertTrue(result)
        # Did not consume stdin.
        self.assertEqual(stdin.read(), "")

    def test_accepts_yes(self):
        stdin = _FakeTTY("yes\n")
        result = prompt_live_confirmation(
            recap_text="recap", skip=False, stdin=stdin, stdout=io.StringIO()
        )
        self.assertTrue(result)

    def test_accepts_uppercase_yes(self):
        stdin = _FakeTTY("YES\n")
        result = prompt_live_confirmation(
            recap_text="recap", skip=False, stdin=stdin, stdout=io.StringIO()
        )
        self.assertTrue(result)

    def test_accepts_yes_with_trailing_whitespace(self):
        stdin = _FakeTTY("  yes  \n")
        result = prompt_live_confirmation(
            recap_text="recap", skip=False, stdin=stdin, stdout=io.StringIO()
        )
        self.assertTrue(result)

    def test_rejects_y(self):
        stdin = _FakeTTY("y\n")
        result = prompt_live_confirmation(
            recap_text="recap", skip=False, stdin=stdin, stdout=io.StringIO()
        )
        self.assertFalse(result)

    def test_rejects_empty(self):
        stdin = _FakeTTY("\n")
        result = prompt_live_confirmation(
            recap_text="recap", skip=False, stdin=stdin, stdout=io.StringIO()
        )
        self.assertFalse(result)

    def test_rejects_no(self):
        stdin = _FakeTTY("no\n")
        result = prompt_live_confirmation(
            recap_text="recap", skip=False, stdin=stdin, stdout=io.StringIO()
        )
        self.assertFalse(result)

    def test_aborts_when_stdin_not_tty(self):
        stdin = _FakeNonTTY("yes\n")  # even with "yes" piped in.
        result = prompt_live_confirmation(
            recap_text="recap", skip=False, stdin=stdin, stdout=io.StringIO()
        )
        self.assertFalse(result)

    def test_aborts_on_eof(self):
        stdin = _FakeTTY("")  # immediate EOF
        result = prompt_live_confirmation(
            recap_text="recap", skip=False, stdin=stdin, stdout=io.StringIO()
        )
        self.assertFalse(result)


class BuildLiveRecapTests(unittest.TestCase):
    def setUp(self):
        self._snapshot = dict(os.environ)
        for k in [k for k in os.environ if k.startswith("POLYMARKET_")]:
            if k != "POLYMARKET_SKIP_DOTENV":
                del os.environ[k]

    def tearDown(self):
        for k in list(os.environ.keys()):
            if k not in self._snapshot:
                del os.environ[k]
            elif os.environ[k] != self._snapshot[k]:
                os.environ[k] = self._snapshot[k]

    def test_recap_contains_critical_fields(self):
        os.environ["POLYMARKET_SMART_POSITION_PCT"] = "0.18"
        os.environ["POLYMARKET_SMART_MIN_CONSENSUS"] = "2"
        os.environ["POLYMARKET_SMART_STOP_LOSS_PCT"] = "0.40"
        os.environ["POLYMARKET_FUNDER_ADDRESS"] = "0xAbCdEf1234567890aBcDef1234567890AbCdEf12"
        settings = Settings()
        text = build_live_recap(settings, profile_label="live-90.toml")
        self.assertIn("LIVE TRADING", text)
        self.assertIn("live-90.toml", text)
        # Funder address must be redacted: first 6 chars + last 4 chars.
        self.assertIn("0xAbCd", text)
        self.assertIn("Ef12", text)
        # No full address leaked.
        self.assertNotIn("0xAbCdEf1234567890aBcDef1234567890AbCdEf12", text)
        self.assertIn("position_pct", text)
        self.assertIn("0.18", text)
        self.assertIn("min_consensus", text)
        self.assertIn("stop_loss", text)
        # normalize separators — the recap prints str(Path), backslashes on Windows
        self.assertIn("data/paper_state.json", text.replace("\\", "/"))

    def test_recap_without_funder_address(self):
        settings = Settings()
        text = build_live_recap(settings, profile_label="baseline.toml")
        self.assertIn("not configured", text.lower())

    def test_recap_includes_live_risk_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "cash": 10.0,
                        "positions": [
                            {
                                "status": "open",
                                "stake": 3.0,
                                "category": "SPORTS",
                                "entry_price": 0.60,
                                "current_price": 0.55,
                                "end_date": "2026-05-23T00:30:00+00:00",
                            }
                        ],
                    }
                )
            )
            settings = Settings(state_path=state_path)
            text = build_live_recap(settings, profile_label="baseline_tight.toml")
        self.assertIn("Live risk snapshot", text)
        self.assertIn("open_exposure", text)
        self.assertIn("$3.00 across 1 position", text)


if __name__ == "__main__":
    unittest.main()
