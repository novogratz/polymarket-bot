import os

os.environ["POLYMARKET_SKIP_DOTENV"] = "1"
for _k in [k for k in os.environ if k.startswith("POLYMARKET_") and k != "POLYMARKET_SKIP_DOTENV"]:
    del os.environ[_k]

import json
import tempfile
import unittest
from pathlib import Path

from polymarket_bot.config import Settings
from polymarket_bot import tick_state


def _settings_in(tmp: Path, dry_run: bool = False) -> Settings:
    return Settings(
        dry_run=dry_run,
        tick_state_path=tmp / "last_tick.json",
        tick_history_path=tmp / "tick_history.jsonl",
    )


def _record(tick_id: int) -> dict:
    return {
        "tick_id": tick_id,
        "started_at": "2026-05-08T14:31:55Z",
        "finished_at": "2026-05-08T14:31:59Z",
        "duration_s": 4.2,
        "mode": "live",
        "scan_counts": {"strict": 0, "relaxed": 2, "deep": 0, "candidates_total": 32},
        "actions": [],
        "tuner_changes": {"applied": False, "journal_size": 0, "overrides_active": {}},
        "next_tick_at": "2026-05-08T14:32:18Z",
    }


class TickStateTests(unittest.TestCase):
    def test_read_last_tick_returns_none_when_file_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = _settings_in(Path(tmp))
            self.assertIsNone(tick_state.read_last_tick(s))

    def test_read_history_returns_empty_when_file_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = _settings_in(Path(tmp))
            self.assertEqual(tick_state.read_tick_history(s), [])

    def test_write_then_read_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = _settings_in(Path(tmp))
            tick_state.write_tick(s, _record(1))
            self.assertEqual(tick_state.read_last_tick(s)["tick_id"], 1)
            self.assertEqual(len(tick_state.read_tick_history(s)), 1)

    def test_history_appends_in_chronological_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = _settings_in(Path(tmp))
            for i in range(5):
                tick_state.write_tick(s, _record(i))
            history = tick_state.read_tick_history(s, limit=10)
            self.assertEqual([r["tick_id"] for r in history], [4, 3, 2, 1, 0])

    def test_history_caps_at_200_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = _settings_in(Path(tmp))
            for i in range(250):
                tick_state.write_tick(s, _record(i))
            raw = (Path(tmp) / "tick_history.jsonl").read_text().splitlines()
            self.assertEqual(len(raw), 200)
            history = tick_state.read_tick_history(s, limit=300)
            self.assertEqual(len(history), 200)
            self.assertEqual(history[0]["tick_id"], 249)
            self.assertEqual(history[-1]["tick_id"], 50)

    def test_read_tick_history_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = _settings_in(Path(tmp))
            for i in range(10):
                tick_state.write_tick(s, _record(i))
            self.assertEqual(len(tick_state.read_tick_history(s, limit=3)), 3)

    def test_write_tick_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "sub" / "dir"
            s = Settings(
                tick_state_path=nested / "last.json",
                tick_history_path=nested / "history.jsonl",
            )
            tick_state.write_tick(s, _record(1))
            self.assertTrue((nested / "last.json").exists())
            self.assertTrue((nested / "history.jsonl").exists())

    def test_dry_run_paths_do_not_collide_with_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path.cwd()
            os.chdir(tmp)
            try:
                Path("data").mkdir(exist_ok=True)
                live = Settings(dry_run=False)
                dry = Settings(dry_run=True)
                tick_state.write_tick(live, _record(1))
                tick_state.write_tick(dry, _record(99))
                self.assertEqual(tick_state.read_last_tick(live)["tick_id"], 1)
                self.assertEqual(tick_state.read_last_tick(dry)["tick_id"], 99)
            finally:
                os.chdir(cwd)

    def test_corrupt_history_line_is_skipped_on_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = _settings_in(Path(tmp))
            s.tick_history_path.parent.mkdir(parents=True, exist_ok=True)
            s.tick_history_path.write_text(
                json.dumps(_record(1)) + "\n"
                + "not-json-at-all\n"
                + json.dumps(_record(2)) + "\n"
            )
            history = tick_state.read_tick_history(s)
            self.assertEqual([r["tick_id"] for r in history], [2, 1])
