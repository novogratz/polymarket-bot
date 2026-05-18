"""Tests for the atomic write helper."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from polymarket_bot._atomic_io import atomic_write_text


class TestAtomicWriteText(unittest.TestCase):
    def test_writes_content_to_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.json"
            atomic_write_text(target, '{"a": 1}')
            self.assertEqual(target.read_text(), '{"a": 1}')

    def test_creates_missing_parent_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "deep" / "out.txt"
            atomic_write_text(target, "hello")
            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(), "hello")

    def test_overwrites_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.txt"
            target.write_text("old content")
            atomic_write_text(target, "new content")
            self.assertEqual(target.read_text(), "new content")

    def test_failure_leaves_previous_target_intact(self) -> None:
        """If os.replace fails after the tempfile is written, the original
        target must remain unchanged. This protects against a corrupt
        state file when a crash happens mid-write."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ledger.json"
            target.write_text('{"cash": 100.0}')

            with mock.patch(
                "polymarket_bot._atomic_io.os.replace",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaises(OSError):
                    atomic_write_text(target, '{"cash": 999.0}')

            self.assertEqual(target.read_text(), '{"cash": 100.0}')

    def test_failure_cleans_up_tempfile(self) -> None:
        """A failed write must not leave .tmp debris around the target."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ledger.json"
            target.write_text("original")

            with mock.patch(
                "polymarket_bot._atomic_io.os.replace",
                side_effect=OSError("boom"),
            ):
                with self.assertRaises(OSError):
                    atomic_write_text(target, "new")

            leftover = [
                p for p in Path(tmp).iterdir()
                if p.name.startswith(target.name + ".") and p.name.endswith(".tmp")
            ]
            self.assertEqual(leftover, [], f"tempfile not cleaned up: {leftover}")

    def test_replace_is_atomic_from_reader_view(self) -> None:
        """A concurrent reader either sees the old content or the new,
        never a half-written file. We simulate by checking the file is
        valid JSON before AND after the swap."""
        import json

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ledger.json"
            target.write_text(json.dumps({"version": 1, "cash": 100.0}))

            # Reader baseline: file is valid.
            self.assertEqual(json.loads(target.read_text())["version"], 1)

            atomic_write_text(target, json.dumps({"version": 2, "cash": 200.0}))

            # Reader post-swap: file is still valid, version bumped.
            self.assertEqual(json.loads(target.read_text())["version"], 2)


if __name__ == "__main__":
    unittest.main()
