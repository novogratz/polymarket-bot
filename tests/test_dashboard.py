import os

os.environ["POLYMARKET_SKIP_DOTENV"] = "1"
for _k in [k for k in os.environ if k.startswith("POLYMARKET_") and k != "POLYMARKET_SKIP_DOTENV"]:
    del os.environ[_k]

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from polymarket_bot.config import Settings
from polymarket_bot import dashboard
from polymarket_bot.dashboard_html import HTML
from polymarket_bot import tick_state


def _settings_in(tmp: Path, **extra) -> Settings:
    base = dict(
        state_path=tmp / "state.json",
        trade_journal_path=tmp / "journal.jsonl",
        strategy_overrides_path=tmp / "overrides.json",
        tick_state_path=tmp / "last_tick.json",
        tick_history_path=tmp / "tick_history.jsonl",
        paper_balance_usd=20.0,
    )
    base.update(extra)
    return Settings(**base)


class BuildLiveTests(unittest.TestCase):
    def test_returns_empty_payload_when_no_tick_yet(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = _settings_in(Path(tmp))
            payload = dashboard.build_live(s)
            self.assertIsNone(payload["last_tick"])
            self.assertEqual(payload["history"], [])
            self.assertEqual(payload["mode"], "live")

    def test_returns_last_tick_and_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = _settings_in(Path(tmp))
            for i in range(3):
                tick_state.write_tick(s, {"tick_id": i, "started_at": f"2026-01-0{i+1}T00:00:00Z", "mode": "live"})
            payload = dashboard.build_live(s)
            self.assertEqual(payload["last_tick"]["tick_id"], 2)
            self.assertEqual([t["tick_id"] for t in payload["history"]], [2, 1, 0])

    def test_dry_run_mode_reflected_in_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = _settings_in(Path(tmp), dry_run=True)
            payload = dashboard.build_live(s)
            self.assertEqual(payload["mode"], "dry_run")


class BuildStatsTests(unittest.TestCase):
    def test_returns_journal_stats_with_max_drawdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = _settings_in(Path(tmp))
            s.trade_journal_path.write_text("\n".join(
                json.dumps({"realized_pnl": v, "closed_at": f"2026-01-{i+1:02d}T00:00:00Z", "exit_reason": "tp"})
                for i, v in enumerate([1.0, -2.0, 0.5])
            ) + "\n")
            payload = dashboard.build_stats(s)
            self.assertEqual(payload["records"], 3)
            self.assertIn("max_drawdown", payload)


class BuildTuneTests(unittest.TestCase):
    def test_returns_disabled_state_when_auto_tune_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = _settings_in(Path(tmp), smart_auto_tune_enabled=False)
            payload = dashboard.build_tune(s)
            self.assertFalse(payload["enabled"])
            self.assertEqual(payload["overrides_active"], {})

    def test_reads_overrides_file_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = _settings_in(Path(tmp))
            s.strategy_overrides_path.write_text(json.dumps({
                "generated_at": "2026-05-08T22:00:42Z",
                "records_observed": 47,
                "min_trades_required": 30,
                "overrides": {"smart_max_chase_premium": 0.08},
            }))
            payload = dashboard.build_tune(s)
            self.assertTrue(payload["enabled"])
            self.assertEqual(payload["records_observed"], 47)
            self.assertEqual(payload["overrides_active"]["smart_max_chase_premium"], 0.08)
            self.assertIn("smart_max_chase_premium", payload["defaults"])

    def test_includes_structured_suggestions(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = _settings_in(Path(tmp))
            s.trade_journal_path.write_text(json.dumps({"realized_pnl": 1.0}) + "\n")
            payload = dashboard.build_tune(s)
            self.assertIsInstance(payload["suggestions"], list)
            self.assertTrue(payload["suggestions"])
            self.assertIsInstance(payload["suggestions"][0], dict)


class BuildStateTests(unittest.TestCase):
    def test_state_includes_existing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = _settings_in(Path(tmp))
            with patch.object(dashboard, "_live_available_balance", return_value=None):
                with patch("polymarket_bot.dashboard.GammaClient") as gamma:
                    gamma.return_value.get_markets.return_value = []
                    payload = dashboard.build_state(s)
            self.assertIn("summary", payload)
            self.assertIn("positions", payload)
            self.assertIn("recent_trades", payload)
            self.assertIn("closed_trades", payload)
            self.assertIn("candidates", payload)
            self.assertIn("dry_run", payload)
            self.assertIn("balance_source", payload)


class DashboardHtmlTests(unittest.TestCase):
    def test_page_reload_is_scheduled_every_thirty_minutes(self):
        self.assertIn("pageReloadMs: 30 * 60 * 1000", HTML)
        self.assertIn("window.location.reload()", HTML)
        self.assertIn("reload 30m", HTML)
