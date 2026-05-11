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


if __name__ == "__main__":
    unittest.main()
