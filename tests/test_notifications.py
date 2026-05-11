import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Callable
from unittest import mock
from unittest.mock import patch

from polymarket_bot import notifications


class NotificationsBaseTest(unittest.TestCase):
    """Base class qui isole les env vars et l'état entre tests."""

    def setUp(self) -> None:
        self._env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        for key in list(os.environ):
            if key.startswith("TELEGRAM_") or key == "POLYMARKET_DRY_RUN":
                os.environ.pop(key, None)
        notifications._reset_for_tests()

    def tearDown(self) -> None:
        self._env_patch.stop()
        notifications._reset_for_tests()


class TestDisabled(NotificationsBaseTest):
    def test_disabled_when_no_token(self) -> None:
        sent: list[dict] = []
        notifications.set_transport_for_test(lambda payload: sent.append(payload) or True)

        self.assertFalse(notifications.is_enabled())
        notifications.notify_trade_buy(
            market_title="Whatever",
            token_id="0xabc",
            price=0.5,
            size_usd=10.0,
            signal={"wallets": 0, "copied_usdc": 0},
        )
        notifications.notify_error("test", "should not send")
        notifications.notify_threshold("drawdown", {"pct": -10})
        notifications.notify_daily_summary({"equity": 90.0})

        self.assertEqual(sent, [])


class TestMdEscape(NotificationsBaseTest):
    def test_md_escape(self) -> None:
        # Les 18 caractères MarkdownV2 spéciaux: _*[]()~`>#+-=|{}.!
        raw = "Trump (2028)? +12% — risk! foo_bar [link]"
        escaped = notifications._md_escape(raw)
        for ch in "_*[]()~`>#+-=|{}.!":
            if ch in raw:
                self.assertIn("\\" + ch, escaped, f"char {ch!r} not escaped")
        # Texte sans caractères spéciaux passe inchangé
        self.assertEqual(notifications._md_escape("hello world"), "hello world")
        # Texte vide
        self.assertEqual(notifications._md_escape(""), "")
        # Single non-special char
        self.assertEqual(notifications._md_escape("a"), "a")


class TestChatIdRouting(NotificationsBaseTest):
    def _capture(self) -> tuple[Callable, list[dict]]:
        sent: list[dict] = []
        def transport(payload: dict) -> bool:
            sent.append(payload)
            return True
        return transport, sent

    def test_routes_to_live_chat_when_not_dry_run(self) -> None:
        os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
        os.environ["TELEGRAM_CHAT_ID_LIVE"] = "111"
        os.environ["TELEGRAM_CHAT_ID_DRY_RUN"] = "999"
        transport, sent = self._capture()
        notifications.set_transport_for_test(transport)

        self.assertTrue(notifications.is_enabled())
        notifications._post("hello")

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["chat_id"], "111")
        self.assertEqual(sent[0]["text"], "hello")
        self.assertEqual(sent[0]["parse_mode"], "MarkdownV2")

    def test_routes_to_dry_run_chat_when_dry_run(self) -> None:
        os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
        os.environ["TELEGRAM_CHAT_ID_LIVE"] = "111"
        os.environ["TELEGRAM_CHAT_ID_DRY_RUN"] = "999"
        os.environ["POLYMARKET_DRY_RUN"] = "1"
        transport, sent = self._capture()
        notifications.set_transport_for_test(transport)

        self.assertTrue(notifications.is_enabled())
        notifications._post("hi")

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["chat_id"], "999")


class TestHttpFailureSilent(NotificationsBaseTest):
    def test_transport_exception_does_not_propagate(self) -> None:
        os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
        os.environ["TELEGRAM_CHAT_ID_LIVE"] = "111"

        def boom(_payload: dict) -> bool:
            raise TimeoutError("boom")

        notifications.set_transport_for_test(boom)
        buf = io.StringIO()
        with redirect_stderr(buf):
            ok = notifications._post("ping")
        self.assertFalse(ok)
        self.assertIn("[notif] failed", buf.getvalue())


class TestStatePersistence(NotificationsBaseTest):
    def test_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "notif_state.json"
            state = notifications._State(
                equity_peak_usd=92.10,
                equity_floor_breached=False,
                last_daily_summary_date="2026-05-09",
                dedupe_seen={"order_rejected:0xtoken": 1715250400.0},
            )
            notifications._save_state(path, state)
            loaded = notifications._load_state(path)
            self.assertEqual(loaded.equity_peak_usd, 92.10)
            self.assertFalse(loaded.equity_floor_breached)
            self.assertEqual(loaded.last_daily_summary_date, "2026-05-09")
            self.assertEqual(loaded.dedupe_seen.get("order_rejected:0xtoken"), 1715250400.0)

    def test_state_path_routing(self) -> None:
        os.environ["POLYMARKET_DRY_RUN"] = "1"
        self.assertEqual(
            notifications._default_state_path().name, "dry_run_notifications_state.json"
        )
        os.environ.pop("POLYMARKET_DRY_RUN", None)
        self.assertEqual(notifications._default_state_path().name, "notifications_state.json")

    def test_load_missing_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "missing.json"
            state = notifications._load_state(path)
            self.assertIsNone(state.equity_peak_usd)
            self.assertFalse(state.equity_floor_breached)
            self.assertIsNone(state.last_daily_summary_date)
            self.assertEqual(state.dedupe_seen, {})


class TestDedupWindow(NotificationsBaseTest):
    def test_dedup_skips_repeats_within_window(self) -> None:
        os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
        os.environ["TELEGRAM_CHAT_ID_LIVE"] = "111"
        os.environ["TELEGRAM_DEDUPE_WINDOW_SEC"] = "300"

        sent: list[dict] = []
        notifications.set_transport_for_test(lambda p: sent.append(p) or True)

        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            with patch.object(notifications, "_default_state_path", return_value=state_path):
                base = 1_000_000.0
                with patch("time.time", return_value=base):
                    notifications.notify_error("order_rejected", "balance low", dedupe_key="t1")
                with patch("time.time", return_value=base + 100):
                    notifications.notify_error("order_rejected", "balance low", dedupe_key="t1")
                self.assertEqual(len(sent), 1, "second call within window must be skipped")

                with patch("time.time", return_value=base + 400):
                    notifications.notify_error("order_rejected", "balance low", dedupe_key="t1")
                self.assertEqual(len(sent), 2)

                with patch("time.time", return_value=base + 401):
                    notifications.notify_error("order_rejected", "msg2", dedupe_key="t2")
                self.assertEqual(len(sent), 3)

                with patch("time.time", return_value=base + 402):
                    notifications.notify_error("misc", "anything")
                with patch("time.time", return_value=base + 403):
                    notifications.notify_error("misc", "anything")
                self.assertEqual(len(sent), 5)


class TestTradeFormats(NotificationsBaseTest):
    def _setup_enabled(self) -> list[dict]:
        os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
        os.environ["TELEGRAM_CHAT_ID_LIVE"] = "111"
        sent: list[dict] = []
        notifications.set_transport_for_test(lambda p: sent.append(p) or True)
        return sent

    def test_buy_format_contains_key_fields(self) -> None:
        sent = self._setup_enabled()
        notifications.notify_trade_buy(
            market_title="Trump 2028 nominee",
            token_id="0xabc",
            price=0.42,
            size_usd=14.20,
            signal={"wallets": 4, "copied_usdc": 2100.0},
            market_url="https://polymarket.com/event/foo",
        )
        self.assertEqual(len(sent), 1)
        text = sent[0]["text"]
        self.assertIn("BUY", text)
        self.assertIn("14\\.20", text)  # MarkdownV2 escape du point
        self.assertIn("0\\.42", text)
        self.assertIn("Trump 2028 nominee", text)
        self.assertIn("4 wallets", text)

    def test_sell_format_contains_pnl(self) -> None:
        sent = self._setup_enabled()
        notifications.notify_trade_sell(
            market_title="Bitcoin EOY",
            token_id="0xabc",
            price=0.51,
            size_usd=18.50,
            realized_pnl_usd=4.30,
            realized_pnl_pct=30.3,
            reason="take_profit_ladder",
            held_seconds=8040,
        )
        self.assertEqual(len(sent), 1)
        text = sent[0]["text"]
        self.assertIn("SELL", text)
        self.assertIn("take_profit_ladder", text)
        self.assertIn("Bitcoin EOY", text)
        self.assertIn("\\+\\$4\\.30", text)

    def test_trades_disabled_flag_skips(self) -> None:
        sent = self._setup_enabled()
        os.environ["TELEGRAM_ALERT_TRADES"] = "0"
        notifications.notify_trade_buy(
            market_title="x", token_id="t", price=0.5, size_usd=1.0,
            signal={"wallets": 1, "copied_usdc": 100},
        )
        self.assertEqual(sent, [])


class TestBigWinLoss(NotificationsBaseTest):
    def _setup_enabled(self) -> list[dict]:
        os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
        os.environ["TELEGRAM_CHAT_ID_LIVE"] = "111"
        os.environ["TELEGRAM_BIG_WIN_USD"] = "10.0"
        os.environ["TELEGRAM_BIG_LOSS_USD"] = "5.0"
        sent: list[dict] = []
        notifications.set_transport_for_test(lambda p: sent.append(p) or True)
        return sent

    def test_big_win_above_threshold(self) -> None:
        sent = self._setup_enabled()
        notifications.notify_threshold("big_win", {
            "market_title": "BTC EOY", "pnl_usd": 12.40, "reason": "peak_protect",
            "held_seconds": 100000,
        })
        self.assertEqual(len(sent), 1)
        self.assertIn("BIG WIN", sent[0]["text"])
        self.assertIn("12\\.40", sent[0]["text"])

    def test_big_win_below_threshold_skips(self) -> None:
        sent = self._setup_enabled()
        notifications.notify_threshold("big_win", {
            "market_title": "x", "pnl_usd": 5.0, "reason": "tp",
        })
        self.assertEqual(sent, [])

    def test_big_loss_below_negative_threshold(self) -> None:
        sent = self._setup_enabled()
        notifications.notify_threshold("big_loss", {
            "market_title": "x", "pnl_usd": -7.0, "reason": "stop_loss",
        })
        self.assertEqual(len(sent), 1)
        self.assertIn("BIG LOSS", sent[0]["text"])


class TestDrawdownArming(NotificationsBaseTest):
    def _setup(self) -> list[dict]:
        os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
        os.environ["TELEGRAM_CHAT_ID_LIVE"] = "111"
        os.environ["TELEGRAM_DRAWDOWN_PCT"] = "10.0"
        sent: list[dict] = []
        notifications.set_transport_for_test(lambda p: sent.append(p) or True)
        return sent

    def test_drawdown_alerts_only_after_arming(self) -> None:
        sent = self._setup()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            with patch.object(notifications, "_default_state_path", return_value=path):
                # Premier appel: equity 100 → pic 100, pas de drawdown
                notifications.notify_threshold("drawdown", {"equity_usd": 100.0})
                self.assertEqual(len(sent), 0)

                # Equity tombe à 95 (-5%): sous le seuil 10%, pas d'alerte
                notifications.notify_threshold("drawdown", {"equity_usd": 95.0})
                self.assertEqual(len(sent), 0)

                # Equity tombe à 88 (-12%): alerte
                notifications.notify_threshold("drawdown", {"equity_usd": 88.0})
                self.assertEqual(len(sent), 1)
                self.assertIn("Drawdown", sent[0]["text"])

                # Encore à 85 (-15%): pas de re-alerte (déjà armé)
                notifications.notify_threshold("drawdown", {"equity_usd": 85.0})
                self.assertEqual(len(sent), 1)

                # Remonte à 102 (nouveau pic): re-arme
                notifications.notify_threshold("drawdown", {"equity_usd": 102.0})
                self.assertEqual(len(sent), 1)

                # Re-tombe à 89 (-12.7% du nouveau pic): re-alerte
                notifications.notify_threshold("drawdown", {"equity_usd": 89.0})
                self.assertEqual(len(sent), 2)


class TestEquityFloor(NotificationsBaseTest):
    def test_floor_one_shot_with_hysteresis(self) -> None:
        os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
        os.environ["TELEGRAM_CHAT_ID_LIVE"] = "111"
        os.environ["TELEGRAM_EQUITY_FLOOR_USD"] = "50.0"
        sent: list[dict] = []
        notifications.set_transport_for_test(lambda p: sent.append(p) or True)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            with patch.object(notifications, "_default_state_path", return_value=path):
                # Au-dessus du seuil: rien
                notifications.notify_threshold("equity_floor", {"equity_usd": 60.0, "open_positions": 5, "cash_usd": 10})
                self.assertEqual(sent, [])
                # Cassure: alerte
                notifications.notify_threshold("equity_floor", {"equity_usd": 48.0, "open_positions": 6, "cash_usd": 12})
                self.assertEqual(len(sent), 1)
                self.assertIn("Equity floor", sent[0]["text"])
                # Toujours en-dessous: pas de re-alerte
                notifications.notify_threshold("equity_floor", {"equity_usd": 47.0, "open_positions": 6, "cash_usd": 12})
                self.assertEqual(len(sent), 1)
                # Remonte juste au seuil (50): pas de re-arm (hystérésis × 1.05 = 52.5)
                notifications.notify_threshold("equity_floor", {"equity_usd": 51.0, "open_positions": 6, "cash_usd": 12})
                self.assertEqual(len(sent), 1)
                # Re-tombe à 48: pas de re-alerte (pas re-armé)
                notifications.notify_threshold("equity_floor", {"equity_usd": 48.0, "open_positions": 6, "cash_usd": 12})
                self.assertEqual(len(sent), 1)
                # Remonte au-dessus de 52.5: re-arme
                notifications.notify_threshold("equity_floor", {"equity_usd": 53.0, "open_positions": 6, "cash_usd": 12})
                self.assertEqual(len(sent), 1)
                # Re-tombe en-dessous: re-alerte
                notifications.notify_threshold("equity_floor", {"equity_usd": 47.0, "open_positions": 6, "cash_usd": 12})
                self.assertEqual(len(sent), 2)


class TestDailySummary(NotificationsBaseTest):
    def test_summary_sent_once_per_day(self) -> None:
        os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
        os.environ["TELEGRAM_CHAT_ID_LIVE"] = "111"
        sent: list[dict] = []
        notifications.set_transport_for_test(lambda p: sent.append(p) or True)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            with patch.object(notifications, "_default_state_path", return_value=path):
                snap = {
                    "equity_usd": 92.10, "equity_pct_24h": 2.3,
                    "cash_usd": 8.40, "open_positions": 7,
                    "trades_24h": 18, "wins_24h": 12, "losses_24h": 6,
                    "top_winner": {"title": "BTC EOY", "pnl_usd": 5.20},
                    "top_loser": {"title": "NBA Finals", "pnl_usd": -3.10},
                    "today": "2026-05-09",
                }
                notifications.notify_daily_summary(snap)
                self.assertEqual(len(sent), 1)
                self.assertIn("Daily summary", sent[0]["text"])
                # Second appel le même jour: skip
                notifications.notify_daily_summary(snap)
                self.assertEqual(len(sent), 1)
                # Lendemain: nouvel envoi
                snap_next = dict(snap, today="2026-05-10")
                notifications.notify_daily_summary(snap_next)
                self.assertEqual(len(sent), 2)


class TestAutoTuneDiff(NotificationsBaseTest):
    def _setup(self) -> list[dict]:
        os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
        os.environ["TELEGRAM_CHAT_ID_LIVE"] = "111"
        sent: list[dict] = []
        notifications.set_transport_for_test(lambda p: sent.append(p) or True)
        return sent

    def test_skips_when_no_changes(self) -> None:
        sent = self._setup()
        notifications.notify_threshold("auto_tune_change", {"changes": []})
        self.assertEqual(sent, [])

    def test_sends_with_changes(self) -> None:
        sent = self._setup()
        notifications.notify_threshold("auto_tune_change", {
            "changes": [
                {"param": "MIN_CONSENSUS", "old": 2, "new": 3},
                {"param": "MAX_CHASE_PREMIUM", "old": 0.13, "new": 0.104},
            ]
        })
        self.assertEqual(len(sent), 1)
        text = sent[0]["text"]
        self.assertIn("Auto\\-tune", text)
        self.assertIn("MIN_CONSENSUS", text)
        self.assertIn("MAX_CHASE_PREMIUM", text)
