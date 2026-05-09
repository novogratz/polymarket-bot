"""Test d'intégration de la résolution du merge à 3 voies dans strategy_loop.

Vérouille la coordination entre les trois features quand `tick_fn` lève :
- l'exception est capturée dans un dict `error` (chore/uv-migration + dashboard-redesign)
- `notifications.notify_error("tick_failed", ...)` est appelé (worktree-telegram-alerts)
- `tick_state.write_tick(...)` reçoit un record avec `error` populé (feat/dashboard-redesign)
- la loop ne propage pas l'exception et termine normalement (auto_max_ticks=1)
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

os.environ["POLYMARKET_SKIP_DOTENV"] = "1"
for _k in [k for k in os.environ if k.startswith("POLYMARKET_") and k != "POLYMARKET_SKIP_DOTENV"]:
    del os.environ[_k]

from polymarket_bot import notifications  # noqa: E402
from polymarket_bot.config import Settings  # noqa: E402
from polymarket_bot.main import strategy_loop  # noqa: E402


class StrategyLoopErrorPathTests(unittest.TestCase):
    def setUp(self) -> None:
        for key in list(os.environ):
            if key.startswith("TELEGRAM_"):
                os.environ.pop(key, None)
        notifications._reset_for_tests()

    def tearDown(self) -> None:
        notifications._reset_for_tests()

    def _settings(self, tmp: Path) -> Settings:
        return Settings(
            auto_max_ticks=1,
            auto_interval_seconds=0,
            state_path=tmp / "state.json",
            trade_journal_path=tmp / "journal.jsonl",
            tick_state_path=tmp / "last_tick.json",
            tick_history_path=tmp / "tick_history.jsonl",
        )

    def test_tick_fn_exception_writes_error_and_notifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp))

            def boom(_settings: Settings) -> dict:
                raise RuntimeError("kaboom")

            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                mock.patch("polymarket_bot.main.tick_state.write_tick") as mock_write_tick,
                mock.patch("polymarket_bot.main.notifications.notify_error") as mock_notify_error,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                strategy_loop(settings, "test-strategy", boom)

            self.assertEqual(mock_write_tick.call_count, 1)
            args = mock_write_tick.call_args.args
            self.assertEqual(len(args), 2)
            record = args[1]
            self.assertIn("error", record)
            self.assertEqual(record["error"]["type"], "RuntimeError")
            self.assertIn("kaboom", record["error"]["message"])

            self.assertEqual(mock_notify_error.call_count, 1)
            notify_call = mock_notify_error.call_args
            self.assertEqual(notify_call.args[0], "tick_failed")
            self.assertIn("kaboom", notify_call.args[1])
            self.assertEqual(notify_call.kwargs.get("dedupe_key"), "tick_failed")

            self.assertIn("kaboom", stdout.getvalue())

    def test_quiet_mode_renders_error_footer_on_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp))
            settings = Settings(
                **{**settings.__dict__, "quiet": True},
            )

            def boom(_settings: Settings) -> dict:
                raise ValueError("disconnected")

            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                mock.patch("polymarket_bot.main.tick_state.write_tick"),
                mock.patch("polymarket_bot.main.notifications.notify_error"),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                strategy_loop(settings, "test-strategy", boom)

            output = stdout.getvalue()
            self.assertIn("error", output.lower())
            self.assertIn("ValueError", output)
            self.assertIn("disconnected", output)
            self.assertNotIn('"strategy"', output)

    def test_notify_error_failure_does_not_crash_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp))

            def boom(_settings: Settings) -> dict:
                raise RuntimeError("trade-fn boom")

            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                mock.patch("polymarket_bot.main.tick_state.write_tick") as mock_write_tick,
                mock.patch(
                    "polymarket_bot.main.notifications.notify_error",
                    side_effect=Exception("telegram unreachable"),
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                strategy_loop(settings, "test-strategy", boom)

            self.assertEqual(mock_write_tick.call_count, 1)
            self.assertIn("trade-fn boom", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
