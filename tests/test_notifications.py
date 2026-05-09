import os
import unittest
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
