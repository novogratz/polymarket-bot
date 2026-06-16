"""Tests for polymarket_bot.profiles : TOML parsing, validation, env mapping."""

import os
os.environ["POLYMARKET_SKIP_DOTENV"] = "1"

import tempfile
import unittest
from pathlib import Path

from polymarket_bot.profiles import (
    ProfileConfig,
    ProfileValidationError,
    apply_profile_to_env,
    load_profile,
    snapshot_effective_env,
    write_snapshot_toml,
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

            [exits]
            entry_cooldown_after_loss_minutes = 180

            [deep_fallback]
            min_consensus = 2
            """
        )
        profile = load_profile(path)
        self.assertEqual(profile.starting_cash, 90.0)
        self.assertEqual(profile.values["POLYMARKET_SMART_POSITION_PCT"], "0.18")
        self.assertEqual(profile.values["POLYMARKET_SMART_MAX_POSITION_CEILING_USD"], "150.0")
        self.assertEqual(profile.values["POLYMARKET_MIN_OPEN_POSITIONS"], "7")
        self.assertEqual(profile.values["POLYMARKET_SMART_MIN_CONSENSUS"], "2")
        self.assertEqual(profile.values["POLYMARKET_SMART_MIN_COPIED_USDC"], "75.0")
        self.assertEqual(profile.values["POLYMARKET_SMART_ENTRY_COOLDOWN_AFTER_LOSS_MINUTES"], "180")
        self.assertEqual(profile.values["POLYMARKET_SMART_DEEP_FALLBACK_MIN_CONSENSUS"], "2")

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


class ApplyProfileTests(unittest.TestCase):
    def setUp(self):
        self._snapshot = dict(os.environ)

    def tearDown(self):
        for k in list(os.environ.keys()):
            if k not in self._snapshot:
                del os.environ[k]
            elif os.environ[k] != self._snapshot[k]:
                os.environ[k] = self._snapshot[k]

    def _make_profile(self, **values) -> ProfileConfig:
        return ProfileConfig(
            source_path=Path("dummy.toml"),
            starting_cash=100.0,
            values=values,
        )

    def test_apply_sets_env_when_missing(self):
        os.environ.pop("POLYMARKET_SMART_POSITION_PCT", None)
        profile = self._make_profile(POLYMARKET_SMART_POSITION_PCT="0.18")
        apply_profile_to_env(profile)
        self.assertEqual(os.environ["POLYMARKET_SMART_POSITION_PCT"], "0.18")

    def test_apply_preserves_existing_env_by_default(self):
        os.environ["POLYMARKET_SMART_POSITION_PCT"] = "0.25"
        profile = self._make_profile(POLYMARKET_SMART_POSITION_PCT="0.18")
        apply_profile_to_env(profile)
        self.assertEqual(os.environ["POLYMARKET_SMART_POSITION_PCT"], "0.25")

    def test_apply_overrides_when_requested(self):
        os.environ["POLYMARKET_SMART_POSITION_PCT"] = "0.25"
        profile = self._make_profile(POLYMARKET_SMART_POSITION_PCT="0.18")
        apply_profile_to_env(profile, override=True)
        self.assertEqual(os.environ["POLYMARKET_SMART_POSITION_PCT"], "0.18")

    def test_apply_overrides_empty_string(self):
        # Empty string env vars are treated as "missing" so the profile fills them.
        os.environ["POLYMARKET_SMART_POSITION_PCT"] = ""
        profile = self._make_profile(POLYMARKET_SMART_POSITION_PCT="0.18")
        apply_profile_to_env(profile)
        self.assertEqual(os.environ["POLYMARKET_SMART_POSITION_PCT"], "0.18")

    def test_snapshot_returns_only_polymarket_keys(self):
        os.environ["POLYMARKET_SMART_POSITION_PCT"] = "0.18"
        os.environ["POLYMARKET_FOO_BAR"] = "x"
        os.environ["UNRELATED_VAR"] = "ignore-me"
        snap = snapshot_effective_env()
        self.assertEqual(snap.get("POLYMARKET_SMART_POSITION_PCT"), "0.18")
        self.assertEqual(snap.get("POLYMARKET_FOO_BAR"), "x")
        self.assertNotIn("UNRELATED_VAR", snap)


class WriteSnapshotTests(unittest.TestCase):
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

    def test_writes_known_keys_grouped_by_section(self):
        os.environ["POLYMARKET_SMART_POSITION_PCT"] = "0.18"
        os.environ["POLYMARKET_SMART_MIN_CONSENSUS"] = "2"
        os.environ["POLYMARKET_PAPER_BALANCE_USD"] = "100.0"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "snapshot.toml"
            write_snapshot_toml(out, source_label="baseline.toml")
            text = out.read_text(encoding="utf-8")
        self.assertIn("# source: baseline.toml", text)
        self.assertIn("[run]", text)
        self.assertIn("starting_cash = 100.0", text)
        self.assertIn("[sizing]", text)
        self.assertIn("position_pct = 0.18", text)
        self.assertIn("[filters]", text)
        self.assertIn("min_consensus = 2", text)

    def test_writes_unknown_keys_to_extras_section(self):
        os.environ["POLYMARKET_NOT_IN_SCHEMA"] = "abc"
        os.environ["POLYMARKET_SMART_POSITION_PCT"] = "0.20"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "snapshot.toml"
            write_snapshot_toml(out, source_label="env")
            text = out.read_text(encoding="utf-8")
        self.assertIn("[extras]", text)
        self.assertIn('POLYMARKET_NOT_IN_SCHEMA = "abc"', text)

    def test_roundtrip_with_load_profile(self):
        os.environ["POLYMARKET_SMART_POSITION_PCT"] = "0.18"
        os.environ["POLYMARKET_PAPER_BALANCE_USD"] = "100.0"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "snapshot.toml"
            write_snapshot_toml(out, source_label="test")
            reloaded = load_profile(out)
        self.assertEqual(reloaded.starting_cash, 100.0)
        self.assertEqual(reloaded.values["POLYMARKET_SMART_POSITION_PCT"], "0.18")

    def test_creates_parent_directory(self):
        os.environ["POLYMARKET_SMART_POSITION_PCT"] = "0.18"
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "nested" / "sub" / "snapshot.toml"
            write_snapshot_toml(nested, source_label="test")
            self.assertTrue(nested.is_file())

    def test_handles_string_with_quotes_in_extras(self):
        os.environ["POLYMARKET_NOT_IN_SCHEMA"] = 'value with "quotes"'
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "snapshot.toml"
            write_snapshot_toml(out, source_label="test")
            text = out.read_text(encoding="utf-8")
        # Roundtrip should preserve via TOML escaping.
        self.assertIn(r'\"quotes\"', text)


class ProfilesDirectoryTests(unittest.TestCase):
    """Sanity check: configs/profiles/*.toml all load without error."""

    def test_all_shipped_profiles_load(self):
        repo_root = Path(__file__).resolve().parent.parent
        profiles_dir = repo_root / "configs" / "profiles"
        profiles = sorted(profiles_dir.glob("*.toml"))
        self.assertGreaterEqual(len(profiles), 4, f"expected 4+ profiles, got {profiles}")
        for path in profiles:
            with self.subTest(profile=path.name):
                profile = load_profile(path)
                self.assertGreater(len(profile.values), 0, f"{path.name} produced no values")

    def test_baseline_has_expected_keys(self):
        repo_root = Path(__file__).resolve().parent.parent
        path = repo_root / "configs" / "profiles" / "baseline.toml"
        profile = load_profile(path)
        self.assertEqual(profile.starting_cash, 20.0)
        self.assertIn("POLYMARKET_SMART_POSITION_PCT", profile.values)
        self.assertIn("POLYMARKET_SMART_MIN_CONSENSUS", profile.values)
        self.assertIn("POLYMARKET_SMART_STOP_LOSS_PCT", profile.values)
        self.assertIn("POLYMARKET_BTC_EDGE_INTEGRATED", profile.values)
        self.assertIn("POLYMARKET_SMART_NOISE_FALLBACK_ENABLED", profile.values)


class CopyLaneProfileTests(unittest.TestCase):
    """Bot 2's copy-trading profile (smart_b.toml) invariants.

    Bot 2 runs this; bots 1 & 3 run the grinder profiles. The key safety
    invariant is that the copy lane NEVER force-trades to fill cash — high
    utilisation comes from a broad cohort, not from loosening consensus.
    """

    def _smart_b(self):
        repo_root = Path(__file__).resolve().parent.parent
        return load_profile(repo_root / "configs" / "profiles" / "smart_b.toml")

    def test_runs_smart_money_mode(self):
        self.assertEqual(self._smart_b().values["POLYMARKET_RUN_MODE"], "smart_money")

    def test_never_force_trades(self):
        # min_open_positions = 0 and deep fallback OFF: the relaxed/deep
        # consensus-loosening passes (the documented crypto-coinflip
        # money-loser) can never run on this lane.
        values = self._smart_b().values
        self.assertEqual(values["POLYMARKET_MIN_OPEN_POSITIONS"], "0")
        self.assertEqual(values["POLYMARKET_SMART_DEEP_FALLBACK_ENABLED"], "0")
        self.assertEqual(values["POLYMARKET_SMART_NOISE_FALLBACK_ENABLED"], "0")
        self.assertGreaterEqual(int(values["POLYMARKET_SMART_MIN_CONSENSUS"]), 2)

    def test_broad_cohort_for_utilisation(self):
        # Utilisation lever: a wide cohort + small positions, not forcing.
        values = self._smart_b().values
        self.assertGreaterEqual(int(values["POLYMARKET_SMART_LEADERBOARD_LIMIT"]), 50)
        self.assertLessEqual(float(values["POLYMARKET_SMART_MAX_POSITION_CEILING_PCT"]), 0.10)

    def test_mirrors_cohort_exit(self):
        self.assertEqual(self._smart_b().values["POLYMARKET_SMART_COHORT_EXIT_ENABLED"], "1")

    def test_whale_copy_trigger_enabled(self):
        # Bot 2's SECOND trigger: copy any single wallet's >= $50k bet.
        values = self._smart_b().values
        self.assertEqual(values["POLYMARKET_SMART_WHALE_COPY_ENABLED"], "1")
        self.assertEqual(float(values["POLYMARKET_SMART_WHALE_MIN_USDC"]), 50000.0)

    def test_isolated_from_grinder_b(self):
        # The bot-3 isolation guarantee: smart_b must not be a grinder profile,
        # and the grinder_b profile must stay on the grinder mode.
        repo_root = Path(__file__).resolve().parent.parent
        grinder_b = load_profile(repo_root / "configs" / "profiles" / "grinder_b.toml")
        self.assertEqual(grinder_b.values["POLYMARKET_RUN_MODE"], "grinder")
        self.assertNotEqual(
            self._smart_b().values["POLYMARKET_RUN_MODE"],
            grinder_b.values["POLYMARKET_RUN_MODE"],
        )


class TestProfilesPersistenceSection(unittest.TestCase):
    def test_persistence_section_recognized(self) -> None:
        toml_content = """
[persistence]
enabled = true
window_days = 14
cache_threshold = 0.65
intersect_periods = "MONTH,ALL"
intersect_min = 1
"""
        import os, tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write(toml_content)
            path = Path(fh.name)
        try:
            profile = load_profile(path)
            self.assertEqual(profile.values["POLYMARKET_PERSISTENCE_ENABLED"], "1")
            self.assertEqual(profile.values["POLYMARKET_PERSISTENCE_WINDOW_DAYS"], "14")
            self.assertEqual(profile.values["POLYMARKET_PERSISTENCE_CACHE_THRESHOLD"], "0.65")
            self.assertEqual(profile.values["POLYMARKET_PERSISTENCE_INTERSECT_PERIODS"], "MONTH,ALL")
            self.assertEqual(profile.values["POLYMARKET_PERSISTENCE_INTERSECT_MIN"], "1")
        finally:
            os.unlink(path)

    def test_persistence_section_disabled(self) -> None:
        toml_content = """
[persistence]
enabled = false
"""
        import os, tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write(toml_content)
            path = Path(fh.name)
        try:
            profile = load_profile(path)
            self.assertEqual(profile.values["POLYMARKET_PERSISTENCE_ENABLED"], "0")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
