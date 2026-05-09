import os
import unittest
from typing import Callable
from unittest import mock

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
