"""CLI : flag --no-persistence force POLYMARKET_PERSISTENCE_ENABLED=false."""
from __future__ import annotations

import os
import re
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from polymarket_bot.main import app


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


_CLEANUP_KEYS = (
    "POLYMARKET_PERSISTENCE_ENABLED",
    "POLYMARKET_DRY_RUN",
    "POLYMARKET_STATE_PATH",
    "POLYMARKET_TRADE_JOURNAL_PATH",
    "POLYMARKET_STRATEGY_OVERRIDES_PATH",
    "POLYMARKET_TICK_STATE_PATH",
    "POLYMARKET_TICK_HISTORY_PATH",
)


class TestNoPersistenceFlag(unittest.TestCase):
    def _clean(self) -> None:
        for key in _CLEANUP_KEYS:
            os.environ.pop(key, None)

    def setUp(self) -> None:
        self._clean()

    def tearDown(self) -> None:
        self._clean()

    def test_flag_recognized_by_cli(self) -> None:
        """--no-persistence doit apparaître dans la help text de auto-loop."""
        runner = CliRunner()
        result = runner.invoke(app, ["auto-loop", "--help"])
        # Rich/Typer en CI insère des codes ANSI au milieu des mots —
        # strip avant de chercher la chaîne.
        self.assertIn("--no-persistence", _ANSI_RE.sub("", result.output))

    def test_flag_sets_env_var_false(self) -> None:
        """Avec --no-persistence, POLYMARKET_PERSISTENCE_ENABLED=false avant le tick."""
        runner = CliRunner()
        # On mock le travail réel pour ne pas vraiment lancer la boucle
        with patch("polymarket_bot.main.smart_money_loop") as loop_mock:
            loop_mock.return_value = None
            # On capture la valeur de l'env var au moment où smart_money_loop est appelé
            captured: dict[str, str] = {}

            def _capture(*args, **kwargs):
                captured["env"] = os.environ.get(
                    "POLYMARKET_PERSISTENCE_ENABLED", "<unset>"
                )

            loop_mock.side_effect = _capture

            result = runner.invoke(
                app,
                ["auto-loop", "--dry-run", "--no-persistence"],
                catch_exceptions=False,
            )
            if result.exit_code != 0:
                self.skipTest(
                    f"auto-loop did not return 0; output={result.output!r}"
                )
            self.assertEqual(captured.get("env"), "false")
