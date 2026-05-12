"""Tests for the pmbot dry-run sub-commands."""

import os
os.environ["POLYMARKET_SKIP_DOTENV"] = "1"
for _k in [k for k in os.environ if k.startswith("POLYMARKET_") and k != "POLYMARKET_SKIP_DOTENV"]:
    del os.environ[_k]

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typer.testing import CliRunner
from unittest.mock import patch


class DryRunCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_data = Path(self._tmp.name) / "data"
        self._tmp_data.mkdir(parents=True)
        # Patch _data_dir to point at our tmp.
        patcher = patch("polymarket_bot.dry_run_cli._data_dir", return_value=self._tmp_data)
        self.addCleanup(patcher.stop)
        patcher.start()
        from polymarket_bot.main import app
        self.app = app
        try:
            self.runner = CliRunner(mix_stderr=False)
        except TypeError:
            self.runner = CliRunner()

    def tearDown(self):
        self._tmp.cleanup()

    def _seed_run(self, name: str, starting_cash: float = 100.0, ticks: int = 0):
        from polymarket_bot.dry_run_runs import ensure_run_directory, load_metadata, save_metadata
        paths = ensure_run_directory(self._tmp_data, name, starting_cash=starting_cash, profile_source="baseline.toml")
        if ticks:
            md = load_metadata(paths)
            md.total_ticks = ticks
            save_metadata(paths, md)
        return paths

    def test_list_empty(self):
        result = self.runner.invoke(self.app, ["dry-run", "list"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("no dry-run runs", result.stdout)

    def test_list_with_runs(self):
        # alpha has 0 ticks -> hidden by default; beta has 42 ticks -> shown.
        self._seed_run("alpha")
        self._seed_run("beta", starting_cash=50.0, ticks=42)
        result = self.runner.invoke(self.app, ["dry-run", "list"])
        self.assertEqual(result.exit_code, 0, msg=result.stdout + result.stderr)
        self.assertNotIn("alpha", result.stdout)
        self.assertIn("beta", result.stdout)
        self.assertIn("42", result.stdout)
        self.assertIn("hidden", result.stdout)

    def test_list_all_includes_idle_runs(self):
        # With --all, even 0-tick runs are shown.
        self._seed_run("alpha")
        self._seed_run("beta", starting_cash=50.0, ticks=42)
        result = self.runner.invoke(self.app, ["dry-run", "list", "--all"])
        self.assertEqual(result.exit_code, 0, msg=result.stdout + result.stderr)
        self.assertIn("alpha", result.stdout)
        self.assertIn("beta", result.stdout)
        self.assertNotIn("hidden", result.stdout)

    def test_list_empty_after_reset_hides_run(self):
        # A reset run keeps metadata but has total_ticks=0 + last_tick_at=None.
        # It must be hidden by default with an informative footer.
        from polymarket_bot.dry_run_runs import DryRunPaths, reset_run
        paths = self._seed_run("alpha", ticks=10)
        # Mark as having ticked.
        from polymarket_bot.dry_run_runs import load_metadata, save_metadata
        md = load_metadata(paths)
        md.last_tick_at = "2026-01-01T00:00:00+00:00"
        save_metadata(paths, md)
        # Reset wipes total_ticks back to 0 and last_tick_at to None.
        reset_run(paths)
        result = self.runner.invoke(self.app, ["dry-run", "list"])
        self.assertEqual(result.exit_code, 0, msg=result.stdout + result.stderr)
        self.assertNotIn("alpha", result.stdout)
        self.assertIn("hidden", result.stdout)

    def test_show_run(self):
        self._seed_run("alpha", starting_cash=100.0)
        result = self.runner.invoke(self.app, ["dry-run", "show", "alpha"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Run:           alpha", result.stdout)
        self.assertIn("Starting cash: 100.00$", result.stdout)

    def test_show_unknown(self):
        result = self.runner.invoke(self.app, ["dry-run", "show", "ghost"])
        self.assertNotEqual(result.exit_code, 0)

    def test_reset_run(self):
        paths = self._seed_run("alpha")
        paths.state.write_text('{"cash": 50}', encoding="utf-8")
        result = self.runner.invoke(self.app, ["dry-run", "reset", "alpha"])
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(paths.state.is_file())
        self.assertTrue(paths.metadata.is_file())

    def test_rm_requires_yes(self):
        self._seed_run("alpha")
        result = self.runner.invoke(self.app, ["dry-run", "rm", "alpha"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertTrue((self._tmp_data / "dry_runs" / "alpha").is_dir())

    def test_rm_with_yes(self):
        self._seed_run("alpha")
        result = self.runner.invoke(self.app, ["dry-run", "rm", "alpha", "--yes"])
        self.assertEqual(result.exit_code, 0)
        self.assertFalse((self._tmp_data / "dry_runs" / "alpha").exists())

    def test_compare_needs_two_runs(self):
        self._seed_run("alpha")
        result = self.runner.invoke(self.app, ["dry-run", "compare", "alpha"])
        self.assertNotEqual(result.exit_code, 0)

    def test_compare_two_runs(self):
        self._seed_run("alpha", starting_cash=100.0)
        self._seed_run("beta", starting_cash=50.0)
        result = self.runner.invoke(self.app, ["dry-run", "compare", "alpha", "beta"])
        self.assertEqual(result.exit_code, 0, msg=result.stdout + result.stderr)
        self.assertIn("alpha", result.stdout)
        self.assertIn("beta", result.stdout)
        self.assertIn("100.00$", result.stdout)
        self.assertIn("50.00$", result.stdout)


class ImportLegacyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_data = Path(self._tmp.name) / "data"
        self._tmp_data.mkdir(parents=True)
        patcher = patch("polymarket_bot.dry_run_cli._data_dir", return_value=self._tmp_data)
        self.addCleanup(patcher.stop)
        patcher.start()
        from polymarket_bot.main import app
        self.app = app
        try:
            self.runner = CliRunner(mix_stderr=False)
        except TypeError:
            self.runner = CliRunner()

    def tearDown(self):
        self._tmp.cleanup()

    def test_import_legacy_reconstructs_starting_cash(self):
        # Simulate the case from the conversation:
        # cash=70.05, open_stakes=486.59, closed_cost=1110.99, proceeds=1241.98 -> starting=425.65
        legacy_state = self._tmp_data / "dry_run_state.json"
        legacy_state.write_text(json.dumps({
            "cash": 70.05,
            "positions": [
                {"stake": 200.0, "shares": 100.0, "entry_price": 0.5, "current_price": 0.5},
                {"stake": 286.59, "shares": 100.0, "entry_price": 0.5, "current_price": 0.5},
            ],
            "pending_orders": [],
        }), encoding="utf-8")
        legacy_journal = self._tmp_data / "dry_run_journal.jsonl"
        # 2 closed trades summing to cost=1110.99 and realized=+130.99 (so proceeds=1241.98).
        legacy_journal.write_text(
            json.dumps({"cost_basis": 500.0, "realized_pnl": 50.0}) + "\n"
            + json.dumps({"cost_basis": 610.99, "realized_pnl": 80.99}) + "\n",
            encoding="utf-8",
        )
        result = self.runner.invoke(self.app, ["dry-run", "import-legacy"])
        self.assertEqual(result.exit_code, 0, msg=result.stdout + result.stderr)
        target = self._tmp_data / "dry_runs" / "legacy"
        self.assertTrue((target / "metadata.json").is_file())
        meta = json.loads((target / "metadata.json").read_text(encoding="utf-8"))
        # cash + open_stakes(486.59) + closed_cost(1110.99) - proceeds(1241.98) = starting
        # 70.05 + 486.59 + 1110.99 - 1241.98 = 425.65
        self.assertAlmostEqual(meta["starting_cash"], 425.65, places=2)
        self.assertTrue((target / "state.json").is_file())
        self.assertTrue((target / "journal.jsonl").is_file())

    def test_import_legacy_refuses_when_target_exists(self):
        legacy_state = self._tmp_data / "dry_run_state.json"
        legacy_state.write_text('{"cash": 100, "positions": [], "pending_orders": []}', encoding="utf-8")
        (self._tmp_data / "dry_run_journal.jsonl").write_text("", encoding="utf-8")
        # First import
        result = self.runner.invoke(self.app, ["dry-run", "import-legacy", "--name", "legacy"])
        self.assertEqual(result.exit_code, 0)
        # Second import should refuse
        result2 = self.runner.invoke(self.app, ["dry-run", "import-legacy", "--name", "legacy"])
        self.assertNotEqual(result2.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
