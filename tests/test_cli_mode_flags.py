"""Tests for the --dry-run / --live / --profile / --yes CLI flags on auto-loop."""

import os
os.environ["POLYMARKET_SKIP_DOTENV"] = "1"
for _k in [k for k in os.environ if k.startswith("POLYMARKET_") and k != "POLYMARKET_SKIP_DOTENV"]:
    del os.environ[_k]

import unittest
from pathlib import Path
from typer.testing import CliRunner
from unittest.mock import patch


def _clean_env():
    for k in [k for k in os.environ if k.startswith("POLYMARKET_") and k != "POLYMARKET_SKIP_DOTENV"]:
        del os.environ[k]


class CliAutoLoopFlagsTests(unittest.TestCase):
    def setUp(self):
        self._snapshot = dict(os.environ)
        _clean_env()
        from polymarket_bot.main import app
        self.app = app
        # mix_stderr=False is unsupported on newer typer/click; without it stderr is
        # merged into stdout, which our assertions tolerate.
        try:
            self.runner = CliRunner(mix_stderr=False)
        except TypeError:
            self.runner = CliRunner()

    def tearDown(self):
        _clean_env()
        for k, v in self._snapshot.items():
            os.environ[k] = v
        # Cleanup any snapshot files created during the test.
        for name in ("dry_run_config_snapshot.toml", "live_config_snapshot.toml"):
            p = Path("data") / name
            if p.exists():
                p.unlink()
        # Cleanup default run directory possibly created.
        import shutil
        default_run = Path("data") / "dry_runs" / "default"
        if default_run.exists():
            shutil.rmtree(default_run, ignore_errors=True)

    def test_no_mode_flag_rejects(self):
        result = self.runner.invoke(self.app, ["auto-loop"])
        self.assertNotEqual(result.exit_code, 0)
        combined = (result.stderr or "") + (result.stdout or "")
        self.assertIn("--dry-run or --live", combined)

    def test_both_modes_rejects(self):
        result = self.runner.invoke(self.app, ["auto-loop", "--dry-run", "--live"])
        self.assertNotEqual(result.exit_code, 0)
        combined = (result.stderr or "") + (result.stdout or "")
        self.assertIn("mutually exclusive", combined)

    def test_unknown_profile_rejects(self):
        result = self.runner.invoke(
            self.app, ["auto-loop", "--dry-run", "--profile", "does-not-exist"]
        )
        self.assertNotEqual(result.exit_code, 0)
        combined = (result.stderr or "") + (result.stdout or "")
        self.assertIn("not found", combined.lower())

    def test_dry_run_loads_baseline_profile(self):
        with patch("polymarket_bot.mirror.mirror_loop") as loop_mock:
            result = self.runner.invoke(
                self.app, ["auto-loop", "--dry-run", "--profile", "baseline"]
            )
        self.assertEqual(result.exit_code, 0, msg=(result.stderr or "") + (result.stdout or ""))
        loop_mock.assert_called_once()
        settings = loop_mock.call_args.args[0]
        self.assertEqual(settings.run_mode, "mirror")
        self.assertIn("bossoskil1", settings.mirror_target)
        self.assertAlmostEqual(settings.mirror_copy_ratio, 0.20)
        # baseline.toml sets starting_cash = 100.0 (paper_balance_usd)
        self.assertAlmostEqual(settings.paper_balance_usd, 100.0)

    def test_live_without_yes_aborts_on_non_tty(self):
        with patch("polymarket_bot.mirror.mirror_loop") as loop_mock:
            result = self.runner.invoke(
                self.app, ["auto-loop", "--live", "--profile", "live-90"]
            )
        # CliRunner provides a non-TTY stdin -> confirmation refuses.
        self.assertNotEqual(result.exit_code, 0)
        loop_mock.assert_not_called()

    def test_live_with_yes_skips_prompt(self):
        with patch("polymarket_bot.mirror.mirror_loop") as loop_mock:
            result = self.runner.invoke(
                self.app,
                ["auto-loop", "--live", "--profile", "live-90", "--yes"],
            )
        self.assertEqual(result.exit_code, 0, msg=(result.stderr or "") + (result.stdout or ""))
        loop_mock.assert_called_once()
        settings = loop_mock.call_args.args[0]
        self.assertEqual(settings.run_mode, "mirror")
        self.assertAlmostEqual(settings.mirror_max_position_pct, 0.02)

    def test_dry_run_does_not_apply_live_value_from_profile(self):
        # In dry-run mode the assumed_live_balance_usd from live-90 should still apply,
        # but starting_cash governs the ledger initial cash.
        with patch("polymarket_bot.mirror.mirror_loop") as loop_mock:
            result = self.runner.invoke(
                self.app, ["auto-loop", "--dry-run", "--profile", "baseline"]
            )
        self.assertEqual(result.exit_code, 0)
        settings = loop_mock.call_args.args[0]
        self.assertTrue(settings.dry_run)
        self.assertTrue(str(settings.state_path).endswith("data/dry_runs/default/state.json"))


import tempfile


class CliAutoLoopIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._snapshot = dict(os.environ)
        _clean_env()
        from polymarket_bot.main import app
        self.app = app
        try:
            self.runner = CliRunner(mix_stderr=False)
        except TypeError:
            self.runner = CliRunner()

    def tearDown(self):
        _clean_env()
        for k, v in self._snapshot.items():
            os.environ[k] = v
        import shutil
        default_run = Path("data") / "dry_runs" / "default"
        if default_run.exists():
            shutil.rmtree(default_run, ignore_errors=True)

    def test_dry_run_baseline_writes_snapshot(self):
        with patch("polymarket_bot.mirror.mirror_loop"):
            result = self.runner.invoke(
                self.app, ["auto-loop", "--dry-run", "--profile", "baseline"]
            )
        self.assertEqual(result.exit_code, 0, msg=(result.stderr or "") + (result.stdout or ""))
        # Snapshot lives in the named-run directory (default).
        snap = Path("data") / "dry_runs" / "default" / "config_snapshot.toml"
        self.assertTrue(snap.is_file(), f"snapshot not found at {snap}")
        content = snap.read_text(encoding="utf-8")
        self.assertIn("# source: baseline.toml", content)
        self.assertIn("[mirror]", content)
        self.assertIn("copy_ratio = 0.2", content)

    def test_dry_run_override_env_wins_over_profile(self):
        # Profile sets position_pct = 0.0 ; we override via env.
        os.environ["POLYMARKET_MIRROR_COPY_RATIO"] = "0.33"
        with patch("polymarket_bot.mirror.mirror_loop") as loop_mock:
            result = self.runner.invoke(
                self.app, ["auto-loop", "--dry-run", "--profile", "baseline"]
            )
        self.assertEqual(result.exit_code, 0, msg=(result.stderr or "") + (result.stdout or ""))
        settings = loop_mock.call_args.args[0]
        self.assertAlmostEqual(settings.mirror_copy_ratio, 0.33)

    def test_dry_run_starting_cash_from_profile(self):
        # baseline.toml sets starting_cash = 100.0
        with patch("polymarket_bot.mirror.mirror_loop") as loop_mock:
            result = self.runner.invoke(
                self.app, ["auto-loop", "--dry-run", "--profile", "baseline"]
            )
        self.assertEqual(result.exit_code, 0)
        settings = loop_mock.call_args.args[0]
        self.assertAlmostEqual(settings.paper_balance_usd, 100.0)


class CliAutoLoopNamedRunTests(unittest.TestCase):
    def setUp(self):
        self._snapshot = dict(os.environ)
        _clean_env()
        from polymarket_bot.main import app
        self.app = app
        try:
            self.runner = CliRunner(mix_stderr=False)
        except TypeError:
            self.runner = CliRunner()

    def tearDown(self):
        _clean_env()
        for k, v in self._snapshot.items():
            os.environ[k] = v
        # Cleanup any run directories created by the tests.
        import shutil
        for d in ("sim1", "sim2", "default"):
            p = Path("data") / "dry_runs" / d
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)

    def test_default_run_name_creates_default_dir(self):
        with patch("polymarket_bot.mirror.mirror_loop"):
            result = self.runner.invoke(self.app, ["auto-loop", "--dry-run", "--profile", "baseline"])
        self.assertEqual(result.exit_code, 0, msg=(result.stderr or "") + (result.stdout or ""))
        self.assertTrue((Path("data") / "dry_runs" / "default" / "metadata.json").is_file())

    def test_explicit_run_name_creates_named_dir(self):
        with patch("polymarket_bot.mirror.mirror_loop"):
            result = self.runner.invoke(
                self.app, ["auto-loop", "--dry-run", "--run", "sim1", "--profile", "baseline"]
            )
        self.assertEqual(result.exit_code, 0)
        run_dir = Path("data") / "dry_runs" / "sim1"
        self.assertTrue((run_dir / "metadata.json").is_file())
        self.assertTrue((run_dir / "config_snapshot.toml").is_file())

    def test_run_name_validated(self):
        with patch("polymarket_bot.mirror.mirror_loop"):
            result = self.runner.invoke(
                self.app, ["auto-loop", "--dry-run", "--run", "bad/name", "--profile", "baseline"]
            )
        self.assertNotEqual(result.exit_code, 0)

    def test_run_rejected_in_live_mode(self):
        with patch("polymarket_bot.mirror.mirror_loop") as loop_mock:
            result = self.runner.invoke(
                self.app, ["auto-loop", "--live", "--run", "sim2", "--profile", "live-90", "--yes"]
            )
        self.assertNotEqual(result.exit_code, 0)
        loop_mock.assert_not_called()
        combined = (result.stderr or "") + (result.stdout or "")
        self.assertIn("dry-run only", combined)


if __name__ == "__main__":
    unittest.main()
