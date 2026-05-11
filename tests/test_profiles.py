"""Tests for polymarket_bot.profiles : TOML parsing, validation, env mapping."""

import os
os.environ["POLYMARKET_SKIP_DOTENV"] = "1"

import tempfile
import unittest
from pathlib import Path

from polymarket_bot.profiles import (
    ProfileConfig,
    ProfileValidationError,
    load_profile,
)


class LoadProfileTests(unittest.TestCase):
    def _write_profile(self, content: str) -> Path:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False, encoding="utf-8"
        )
        tmp.write(content)
        tmp.close()
        path = Path(tmp.name)
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_loads_minimal_profile(self):
        path = self._write_profile(
            """
            [run]
            starting_cash = 100.0
            """
        )
        profile = load_profile(path)
        self.assertIsInstance(profile, ProfileConfig)
        self.assertEqual(profile.starting_cash, 100.0)

    def test_loads_full_profile(self):
        path = self._write_profile(
            """
            [run]
            starting_cash = 90.0

            [sizing]
            position_pct = 0.18
            max_position_ceiling_usd = 150.0
            min_open_positions = 7

            [filters]
            min_consensus = 2
            min_copied_usdc = 75.0
            """
        )
        profile = load_profile(path)
        self.assertEqual(profile.starting_cash, 90.0)
        self.assertEqual(profile.values["POLYMARKET_SMART_POSITION_PCT"], "0.18")
        self.assertEqual(profile.values["POLYMARKET_SMART_MAX_POSITION_CEILING_USD"], "150.0")
        self.assertEqual(profile.values["POLYMARKET_MIN_OPEN_POSITIONS"], "7")
        self.assertEqual(profile.values["POLYMARKET_SMART_MIN_CONSENSUS"], "2")
        self.assertEqual(profile.values["POLYMARKET_SMART_MIN_COPIED_USDC"], "75.0")

    def test_rejects_unknown_section(self):
        path = self._write_profile(
            """
            [unknown_section]
            foo = "bar"
            """
        )
        with self.assertRaises(ProfileValidationError) as ctx:
            load_profile(path)
        self.assertIn("unknown_section", str(ctx.exception))

    def test_rejects_unknown_key(self):
        path = self._write_profile(
            """
            [sizing]
            nonexistent_key = 0.5
            """
        )
        with self.assertRaises(ProfileValidationError) as ctx:
            load_profile(path)
        self.assertIn("nonexistent_key", str(ctx.exception))

    def test_rejects_invalid_toml(self):
        path = self._write_profile("not valid toml = =")
        with self.assertRaises(ProfileValidationError):
            load_profile(path)

    def test_rejects_missing_file(self):
        with self.assertRaises(ProfileValidationError) as ctx:
            load_profile(Path("/tmp/does-not-exist.toml"))
        self.assertIn("not found", str(ctx.exception).lower())

    def test_rejects_bool_for_int_field(self):
        # min_open_positions expects int — true should be rejected, not silently coerced
        path = self._write_profile(
            """
            [sizing]
            min_open_positions = true
            """
        )
        with self.assertRaises(ProfileValidationError) as ctx:
            load_profile(path)
        self.assertIn("min_open_positions", str(ctx.exception))

    def test_rejects_bool_for_float_field(self):
        path = self._write_profile(
            """
            [sizing]
            position_pct = true
            """
        )
        with self.assertRaises(ProfileValidationError) as ctx:
            load_profile(path)
        self.assertIn("position_pct", str(ctx.exception))

    def test_rejects_string_for_float_field(self):
        path = self._write_profile(
            """
            [sizing]
            position_pct = "0.18"
            """
        )
        with self.assertRaises(ProfileValidationError) as ctx:
            load_profile(path)
        self.assertIn("position_pct", str(ctx.exception))

    def test_rejects_int_for_bool_field(self):
        # btc_edge.enabled expects bool — 1 should be rejected, not silently coerced
        path = self._write_profile(
            """
            [btc_edge]
            enabled = 1
            """
        )
        with self.assertRaises(ProfileValidationError) as ctx:
            load_profile(path)
        self.assertIn("enabled", str(ctx.exception))

    def test_accepts_int_for_float_field(self):
        # TOML 150 should be accepted as float
        path = self._write_profile(
            """
            [sizing]
            max_position_ceiling_usd = 150
            """
        )
        profile = load_profile(path)
        self.assertEqual(profile.values["POLYMARKET_SMART_MAX_POSITION_CEILING_USD"], "150.0")

    def test_starting_cash_defaults_when_run_section_absent(self):
        # Profile without [run] -> starting_cash falls back to module default (100.0).
        path = self._write_profile(
            """
            [filters]
            min_consensus = 2
            """
        )
        profile = load_profile(path)
        self.assertEqual(profile.starting_cash, 100.0)


if __name__ == "__main__":
    unittest.main()
