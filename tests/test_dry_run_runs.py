"""Tests for polymarket_bot.dry_run_runs."""

import os
os.environ["POLYMARKET_SKIP_DOTENV"] = "1"
for _k in [k for k in os.environ if k.startswith("POLYMARKET_") and k != "POLYMARKET_SKIP_DOTENV"]:
    del os.environ[_k]

import json
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.dry_run_runs import (
    DryRunPaths,
    RunMetadata,
    ensure_run_directory,
    load_metadata,
    save_metadata,
    update_tick_metadata,
)


class DryRunPathsTests(unittest.TestCase):
    def test_paths_layout(self):
        root = Path("/tmp/base")
        paths = DryRunPaths.for_run(root, "baseline")
        self.assertEqual(paths.root, Path("/tmp/base/dry_runs/baseline"))
        self.assertEqual(paths.metadata, Path("/tmp/base/dry_runs/baseline/metadata.json"))
        self.assertEqual(paths.state, Path("/tmp/base/dry_runs/baseline/state.json"))
        self.assertEqual(paths.journal, Path("/tmp/base/dry_runs/baseline/journal.jsonl"))
        self.assertEqual(paths.tick_state, Path("/tmp/base/dry_runs/baseline/last_tick.json"))
        self.assertEqual(paths.tick_history, Path("/tmp/base/dry_runs/baseline/tick_history.jsonl"))
        self.assertEqual(paths.overrides, Path("/tmp/base/dry_runs/baseline/overrides.json"))
        self.assertEqual(paths.config_snapshot, Path("/tmp/base/dry_runs/baseline/config_snapshot.toml"))
        self.assertEqual(paths.equity_curve, Path("/tmp/base/dry_runs/baseline/equity_curve.jsonl"))
        self.assertEqual(paths.decisions, Path("/tmp/base/dry_runs/baseline/decisions.jsonl"))


class EnsureRunDirectoryTests(unittest.TestCase):
    def test_creates_directory_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = ensure_run_directory(
                Path(tmp),
                "sim1",
                starting_cash=100.0,
                profile_source="baseline.toml",
            )
            self.assertTrue(paths.root.is_dir())
            self.assertTrue(paths.metadata.is_file())
            metadata = load_metadata(paths)
            self.assertEqual(metadata.run_name, "sim1")
            self.assertEqual(metadata.starting_cash, 100.0)
            self.assertEqual(metadata.profile_source, "baseline.toml")
            self.assertEqual(metadata.total_ticks, 0)
            self.assertIsNotNone(metadata.started_at)

    def test_idempotent_does_not_overwrite_existing_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = ensure_run_directory(
                Path(tmp), "sim2", starting_cash=100.0, profile_source="baseline.toml"
            )
            original = load_metadata(paths)
            # Second call should leave metadata untouched (run already exists).
            paths2 = ensure_run_directory(
                Path(tmp), "sim2", starting_cash=999.0, profile_source="other.toml"
            )
            second = load_metadata(paths2)
            self.assertEqual(second.starting_cash, original.starting_cash)
            self.assertEqual(second.profile_source, original.profile_source)
            self.assertEqual(second.started_at, original.started_at)

    def test_rejects_invalid_run_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("..", "with/slash", "with\\backslash", "", " "):
                with self.subTest(name=bad):
                    with self.assertRaises(ValueError):
                        ensure_run_directory(
                            Path(tmp), bad, starting_cash=100.0, profile_source="x.toml"
                        )


class UpdateTickMetadataTests(unittest.TestCase):
    def test_update_increments_total_ticks_and_last_tick_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = ensure_run_directory(
                Path(tmp), "sim3", starting_cash=100.0, profile_source="baseline.toml"
            )
            initial = load_metadata(paths)
            self.assertEqual(initial.total_ticks, 0)
            self.assertIsNone(initial.last_tick_at)

            update_tick_metadata(paths)
            update_tick_metadata(paths)

            after = load_metadata(paths)
            self.assertEqual(after.total_ticks, 2)
            self.assertIsNotNone(after.last_tick_at)
            self.assertEqual(after.started_at, initial.started_at)


from polymarket_bot.dry_run_runs import list_runs, reset_run, remove_run


class ListRunsTests(unittest.TestCase):
    def test_list_returns_metadata_for_each_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ensure_run_directory(base, "alpha", starting_cash=100.0, profile_source="a.toml")
            ensure_run_directory(base, "beta", starting_cash=50.0, profile_source="b.toml")
            runs = list_runs(base)
            names = sorted(r.run_name for r in runs)
            self.assertEqual(names, ["alpha", "beta"])

    def test_list_empty_when_no_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(list_runs(Path(tmp)), [])

    def test_list_skips_dirs_without_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ensure_run_directory(base, "good", starting_cash=100.0, profile_source="x.toml")
            (base / "dry_runs" / "stray").mkdir()  # no metadata.json
            runs = list_runs(base)
            self.assertEqual([r.run_name for r in runs], ["good"])


class ResetRunTests(unittest.TestCase):
    def test_reset_clears_state_journal_equity_decisions_but_preserves_metadata_and_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = ensure_run_directory(
                Path(tmp), "to-reset", starting_cash=100.0, profile_source="x.toml"
            )
            # Simulate accumulated files.
            paths.state.write_text('{"cash": 50.0}', encoding="utf-8")
            paths.journal.write_text('{"trade": 1}\n', encoding="utf-8")
            paths.equity_curve.write_text('{"equity": 100}\n', encoding="utf-8")
            paths.decisions.write_text('{"tick": 1}\n', encoding="utf-8")
            paths.tick_state.write_text('{}', encoding="utf-8")
            paths.tick_history.write_text('{}\n', encoding="utf-8")
            paths.overrides.write_text('{}', encoding="utf-8")
            paths.config_snapshot.write_text('# snapshot', encoding="utf-8")
            original_metadata = load_metadata(paths)

            reset_run(paths)

            # Volatile files gone.
            self.assertFalse(paths.state.is_file())
            self.assertFalse(paths.journal.is_file())
            self.assertFalse(paths.equity_curve.is_file())
            self.assertFalse(paths.decisions.is_file())
            self.assertFalse(paths.tick_state.is_file())
            self.assertFalse(paths.tick_history.is_file())
            self.assertFalse(paths.overrides.is_file())
            # Metadata preserved BUT total_ticks reset to 0.
            metadata = load_metadata(paths)
            self.assertEqual(metadata.total_ticks, 0)
            self.assertIsNone(metadata.last_tick_at)
            self.assertEqual(metadata.run_name, original_metadata.run_name)
            self.assertEqual(metadata.started_at, original_metadata.started_at)
            # Config snapshot preserved.
            self.assertTrue(paths.config_snapshot.is_file())


class RemoveRunTests(unittest.TestCase):
    def test_remove_deletes_entire_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = ensure_run_directory(
                Path(tmp), "to-remove", starting_cash=100.0, profile_source="x.toml"
            )
            paths.state.write_text('{}', encoding="utf-8")
            self.assertTrue(paths.root.is_dir())
            remove_run(paths)
            self.assertFalse(paths.root.exists())


if __name__ == "__main__":
    unittest.main()
