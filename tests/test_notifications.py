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
            if key.startswith("TELEGRAM_") or key in ("POLYMARKET_DRY_RUN", "POLYMARKET_RUN_NAME"):
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
        notifications.notify_heartbeat({"equity_usd": 90.0})

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


class TestFmtAmount(NotificationsBaseTest):
    def test_below_1000_uses_two_decimals(self) -> None:
        self.assertEqual(notifications._fmt_amount(0), "$0.00")
        self.assertEqual(notifications._fmt_amount(12.5), "$12.50")
        self.assertEqual(notifications._fmt_amount(999.99), "$999.99")

    def test_at_or_above_1000_uses_k_suffix(self) -> None:
        self.assertEqual(notifications._fmt_amount(1000), "$1.0k")
        self.assertEqual(notifications._fmt_amount(1234), "$1.2k")
        self.assertEqual(notifications._fmt_amount(12500), "$12.5k")

    def test_negative_below_1000(self) -> None:
        self.assertEqual(notifications._fmt_amount(-50.25), "$-50.25")

    def test_negative_above_1000(self) -> None:
        # Le signe est porté par |amount| >= 1000 → format -X.Yk
        self.assertEqual(notifications._fmt_amount(-2500), "$-2.5k")


class TestTruncate(NotificationsBaseTest):
    def test_short_string_unchanged(self) -> None:
        self.assertEqual(notifications._truncate("hello", 40), "hello")

    def test_exact_length_unchanged(self) -> None:
        text = "x" * 40
        self.assertEqual(notifications._truncate(text, 40), text)

    def test_long_string_truncated_with_ellipsis(self) -> None:
        text = "x" * 50
        result = notifications._truncate(text, 40)
        self.assertEqual(len(result), 40)
        self.assertTrue(result.endswith("…"))

    def test_empty_string(self) -> None:
        self.assertEqual(notifications._truncate("", 40), "")

    def test_default_max_len_40(self) -> None:
        text = "y" * 50
        self.assertEqual(notifications._truncate(text), notifications._truncate(text, 40))


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

    def test_prefixes_run_name_when_env_set(self) -> None:
        os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
        os.environ["TELEGRAM_CHAT_ID_LIVE"] = "111"
        os.environ["POLYMARKET_RUN_NAME"] = "baseline-A"
        transport, sent = self._capture()
        notifications.set_transport_for_test(transport)

        notifications._post("hello")

        self.assertEqual(len(sent), 1)
        # Run name sur sa propre ligne, brackets/hyphen escapés MarkdownV2.
        self.assertEqual(sent[0]["text"], "*\\[baseline\\-A\\]*\nhello")

    def test_no_prefix_when_run_name_absent(self) -> None:
        os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
        os.environ["TELEGRAM_CHAT_ID_LIVE"] = "111"
        # POLYMARKET_RUN_NAME is unset by setUp.
        transport, sent = self._capture()
        notifications.set_transport_for_test(transport)

        notifications._post("hello")

        self.assertEqual(sent[0]["text"], "hello")

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
                last_heartbeat_ts=1715250500.0,
                dedupe_seen={
                    "order_rejected:0xtoken": {
                        "first_ts": 1715250400.0,
                        "last_ts": 1715250400.0,
                        "count": 1,
                        "last_message": "balance low",
                    },
                },
            )
            notifications._save_state(path, state)
            loaded = notifications._load_state(path)
            self.assertEqual(loaded.equity_peak_usd, 92.10)
            self.assertFalse(loaded.equity_floor_breached)
            self.assertEqual(loaded.last_heartbeat_ts, 1715250500.0)
            entry = loaded.dedupe_seen.get("order_rejected:0xtoken")
            self.assertIsNotNone(entry)
            self.assertEqual(entry["first_ts"], 1715250400.0)
            self.assertEqual(entry["last_ts"], 1715250400.0)
            self.assertEqual(entry["count"], 1)
            self.assertEqual(entry["last_message"], "balance low")

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
            self.assertIsNone(state.last_heartbeat_ts)
            self.assertEqual(state.dedupe_seen, {})


class TestErrorCounter(NotificationsBaseTest):
    def _setup(self) -> list[dict]:
        os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
        os.environ["TELEGRAM_CHAT_ID_LIVE"] = "111"
        os.environ["TELEGRAM_DEDUPE_WINDOW_SEC"] = "300"
        sent: list[dict] = []
        notifications.set_transport_for_test(lambda payload: sent.append(payload) or True)
        return sent

    def test_first_error_no_suffix(self) -> None:
        sent = self._setup()
        with tempfile.TemporaryDirectory() as tmpd:
            path = Path(tmpd) / "s.json"
            with patch.object(notifications, "_default_state_path", return_value=path):
                notifications.notify_error("clob_balance", "balance not enough", dedupe_key="k1")
        self.assertEqual(len(sent), 1)
        text = sent[0]["text"]
        self.assertIn("❌", text)
        # NB : "clob_balance" et "balance not enough" sont escapés via _md_escape
        self.assertIn("clob", text)
        self.assertIn("balance", text)
        self.assertNotIn("×", text)
        self.assertNotIn("\n", text)

    def test_repeats_in_window_are_silenced(self) -> None:
        sent = self._setup()
        with tempfile.TemporaryDirectory() as tmpd:
            path = Path(tmpd) / "s.json"
            with patch.object(notifications, "_default_state_path", return_value=path):
                notifications.notify_error("cat", "msg", dedupe_key="k1")
                notifications.notify_error("cat", "msg", dedupe_key="k1")
                notifications.notify_error("cat", "msg", dedupe_key="k1")
        self.assertEqual(len(sent), 1)

    def test_after_window_emits_with_count_suffix(self) -> None:
        sent = self._setup()
        with tempfile.TemporaryDirectory() as tmpd:
            path = Path(tmpd) / "s.json"
            t0 = 1700000000.0
            with patch.object(notifications, "_default_state_path", return_value=path):
                with patch.object(notifications.time, "time", side_effect=[t0, t0 + 1, t0 + 2]):
                    notifications.notify_error("cat", "msg", dedupe_key="k1")
                    notifications.notify_error("cat", "msg", dedupe_key="k1")
                    notifications.notify_error("cat", "msg", dedupe_key="k1")
                with patch.object(notifications.time, "time", return_value=t0 + 3600):
                    notifications.notify_error("cat", "msg", dedupe_key="k1")
        self.assertEqual(len(sent), 2)
        self.assertIn("×3", sent[1]["text"])
        self.assertIn("min", sent[1]["text"])

    def test_no_dedupe_key_always_emits(self) -> None:
        sent = self._setup()
        with tempfile.TemporaryDirectory() as tmpd:
            path = Path(tmpd) / "s.json"
            with patch.object(notifications, "_default_state_path", return_value=path):
                notifications.notify_error("cat", "msg1")
                notifications.notify_error("cat", "msg2")
        self.assertEqual(len(sent), 2)


class TestTradeBuyOneLine(NotificationsBaseTest):
    def _setup(self) -> list[dict]:
        os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
        os.environ["TELEGRAM_CHAT_ID_LIVE"] = "111"
        sent: list[dict] = []
        notifications.set_transport_for_test(lambda payload: sent.append(payload) or True)
        return sent

    def test_buy_one_line_with_signal(self) -> None:
        sent = self._setup()
        notifications.notify_trade_buy(
            market_title="Trump 2028 GOP",
            token_id="0xabc",
            price=0.34,
            size_usd=12.50,
            signal={"wallets": 3, "copied_usdc": 1200},
            market_url="https://polymarket.com/event/x",
        )
        self.assertEqual(len(sent), 1)
        text = sent[0]["text"]
        # Format carte multi-lignes : header, montant, marché, footer.
        self.assertIn("\n", text)
        self.assertIn("🛒", text)
        self.assertIn("BUY", text)
        # Montants et prix : escapés MdV2 (point devient \.).
        self.assertIn("12\\.50", text)
        self.assertIn("0\\.34", text)
        self.assertIn("Trump 2028 GOP", text)
        self.assertIn("3w", text)
        self.assertIn("$1\\.2k", text)
        self.assertIn("🔗", text)
        self.assertIn("polymarket.com", text)

    def test_buy_with_tag(self) -> None:
        sent = self._setup()
        notifications.notify_trade_buy(
            market_title="BTC ≥ $120k Dec",
            token_id="0xabc",
            price=0.52,
            size_usd=5.0,
            signal={"tag": "btc_edge"},
        )
        self.assertEqual(len(sent), 1)
        text = sent[0]["text"]
        # Format carte : plusieurs lignes.
        self.assertIn("\n", text)
        # tag\=btc\_edge en MdV2 (= et _ échappés).
        self.assertIn("tag\\=btc\\_edge", text)

    def test_buy_does_not_truncate_long_title(self) -> None:
        sent = self._setup()
        notifications.notify_trade_buy(
            market_title="X" * 60,
            token_id="0xabc",
            price=0.5,
            size_usd=10.0,
            signal={"wallets": 2, "copied_usdc": 250},
        )
        text = sent[0]["text"]
        # Telegram autorise 4096 chars/msg ; on n'a aucune raison de tronquer.
        self.assertIn("X" * 60, text)
        self.assertNotIn("…", text)

    def test_buy_disabled_by_toggle(self) -> None:
        sent = self._setup()
        os.environ["TELEGRAM_ALERT_TRADES"] = "0"
        notifications.notify_trade_buy(
            market_title="x", token_id="0xabc",
            price=0.5, size_usd=10.0, signal={"wallets": 2, "copied_usdc": 250},
        )
        self.assertEqual(sent, [])


class TestTradeSellOneLine(NotificationsBaseTest):
    def _setup(self) -> list[dict]:
        os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
        os.environ["TELEGRAM_CHAT_ID_LIVE"] = "111"
        sent: list[dict] = []
        notifications.set_transport_for_test(lambda payload: sent.append(payload) or True)
        return sent

    def test_standard_sell_one_line(self) -> None:
        sent = self._setup()
        notifications.notify_trade_sell(
            market_title="Trump 2028 GOP",
            token_id="0xabc",
            price=0.41,
            size_usd=14.20,
            realized_pnl_usd=1.70,
            realized_pnl_pct=13.6,
            reason="tp_ladder",
            held_seconds=4920,
        )
        self.assertEqual(len(sent), 1)
        text = sent[0]["text"]
        # Format carte multi-lignes.
        self.assertIn("\n", text)
        # Profit positif (1.70 USD) sous le seuil BIG WIN → SELL vert.
        self.assertIn("🟢", text)
        self.assertIn("SELL", text)
        self.assertIn("Trump 2028 GOP", text)
        self.assertIn("13\\.6%", text)
        self.assertIn("1h22m", text)
        self.assertIn("tp\\_ladder", text)

    def test_big_win_replaces_sell_when_above_threshold(self) -> None:
        sent = self._setup()
        os.environ["TELEGRAM_BIG_WIN_USD"] = "10"
        notifications.notify_trade_sell(
            market_title="Trump 2028 GOP",
            token_id="0xabc",
            price=0.41,
            size_usd=14.20,
            realized_pnl_usd=12.40,
            realized_pnl_pct=87.3,
            reason="peak_protect",
            held_seconds=14400,
        )
        self.assertEqual(len(sent), 1, "un seul message, pas deux")
        text = sent[0]["text"]
        self.assertIn("💰", text)
        self.assertIn("BIG WIN", text)
        self.assertNotIn("🔴", text)
        self.assertNotIn("SELL", text)

    def test_big_win_message_drips_green_and_money(self) -> None:
        sent = self._setup()
        os.environ["TELEGRAM_BIG_WIN_USD"] = "10"
        notifications.notify_trade_sell(
            market_title="Trump 2028 GOP",
            token_id="0xabc",
            price=0.41,
            size_usd=14.20,
            realized_pnl_usd=42.50,
            realized_pnl_pct=180.0,
            reason="peak_protect",
            held_seconds=14400,
        )
        text = sent[0]["text"]
        self.assertIn("BIG WINZZZZ", text)
        self.assertGreaterEqual(text.count("💰"), 4)
        self.assertGreaterEqual(text.count("🟢"), 4)
        self.assertIn("💚", text)
        self.assertIn("🤑", text)
        # Pas de "SELL" ni de rouge dans un BIG WIN.
        self.assertNotIn("SELL", text)
        self.assertNotIn("🔴", text)
        self.assertNotIn("📉", text)

    def test_big_loss_replaces_sell_when_below_threshold(self) -> None:
        sent = self._setup()
        os.environ["TELEGRAM_BIG_LOSS_USD"] = "5"
        notifications.notify_trade_sell(
            market_title="NFL Chiefs",
            token_id="0xabc",
            price=0.22,
            size_usd=7.80,
            realized_pnl_usd=-6.10,
            realized_pnl_pct=-43.9,
            reason="stop_loss",
            held_seconds=1080,
        )
        self.assertEqual(len(sent), 1)
        text = sent[0]["text"]
        self.assertIn("💸", text)
        self.assertIn("BIG LOSS", text)
        self.assertNotIn("🔴", text)
        self.assertNotIn("SELL", text)

    def test_below_threshold_uses_standard_sell(self) -> None:
        sent = self._setup()
        os.environ["TELEGRAM_BIG_WIN_USD"] = "10"
        notifications.notify_trade_sell(
            market_title="x", token_id="0xabc",
            price=0.5, size_usd=10.0,
            realized_pnl_usd=2.50, realized_pnl_pct=25.0,
            reason="tp_ladder",
        )
        text = sent[0]["text"]
        # Profit positif sous seuil → SELL vert, pas BIG WIN.
        self.assertIn("🟢", text)
        self.assertNotIn("💰", text)

    def test_thresholds_disabled_falls_back_to_sell(self) -> None:
        sent = self._setup()
        os.environ["TELEGRAM_BIG_WIN_USD"] = "10"
        os.environ["TELEGRAM_ALERT_THRESHOLDS"] = "0"
        notifications.notify_trade_sell(
            market_title="x", token_id="0xabc",
            price=0.5, size_usd=10.0,
            realized_pnl_usd=20.0, realized_pnl_pct=200.0,
            reason="peak_protect",
        )
        text = sent[0]["text"]
        # Seuils désactivés : on retombe sur SELL coloré par PnL (profit → vert).
        self.assertIn("🟢", text)
        self.assertNotIn("💰", text)

    def test_trades_disabled_no_send(self) -> None:
        sent = self._setup()
        os.environ["TELEGRAM_ALERT_TRADES"] = "0"
        notifications.notify_trade_sell(
            market_title="x", token_id="0xabc",
            price=0.5, size_usd=10.0,
            realized_pnl_usd=20.0, realized_pnl_pct=200.0,
            reason="peak_protect",
        )
        self.assertEqual(sent, [])


class TestThresholdOneLine(NotificationsBaseTest):
    def _setup(self) -> list[dict]:
        os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
        os.environ["TELEGRAM_CHAT_ID_LIVE"] = "111"
        sent: list[dict] = []
        notifications.set_transport_for_test(lambda payload: sent.append(payload) or True)
        return sent

    def test_drawdown_one_line(self) -> None:
        sent = self._setup()
        with tempfile.TemporaryDirectory() as tmpd:
            path = Path(tmpd) / "s.json"
            with patch.object(notifications, "_default_state_path", return_value=path):
                notifications.notify_threshold("drawdown", {"equity_usd": 100.0})
                notifications.notify_threshold("drawdown", {"equity_usd": 85.0})
        self.assertEqual(len(sent), 1)
        text = sent[0]["text"]
        self.assertNotIn("\n", text)
        self.assertIn("⚠️", text)
        self.assertIn("DD", text)
        self.assertIn("\\-15\\.0%", text)

    def test_equity_floor_one_line(self) -> None:
        sent = self._setup()
        with tempfile.TemporaryDirectory() as tmpd:
            path = Path(tmpd) / "s.json"
            with patch.object(notifications, "_default_state_path", return_value=path):
                notifications.notify_threshold(
                    "equity_floor",
                    {"equity_usd": 48.0, "cash_usd": 4.0, "open_positions": 5},
                )
        self.assertEqual(len(sent), 1)
        text = sent[0]["text"]
        self.assertNotIn("\n", text)
        self.assertIn("🚨", text)
        self.assertIn("Floor", text)
        self.assertIn("5pos", text)

    def test_auto_tune_one_line(self) -> None:
        sent = self._setup()
        notifications.notify_threshold(
            "auto_tune_change",
            {
                "changes": [
                    {"param": "MIN_CONSENSUS", "old": 2, "new": 3},
                    {"param": "POSITION_PCT", "old": 0.18, "new": 0.14},
                ],
            },
        )
        self.assertEqual(len(sent), 1)
        text = sent[0]["text"]
        self.assertNotIn("\n", text)
        self.assertIn("🛠", text)
        self.assertIn("Tune", text)
        self.assertIn("MIN\\_CONSENSUS", text)
        self.assertIn("2→3", text)
        self.assertIn("POSITION\\_PCT", text)

    def test_big_win_threshold_kind_is_noop(self) -> None:
        sent = self._setup()
        notifications.notify_threshold(
            "big_win",
            {"market_title": "x", "pnl_usd": 100.0, "reason": "tp_ladder"},
        )
        self.assertEqual(sent, [])

    def test_big_loss_threshold_kind_is_noop(self) -> None:
        sent = self._setup()
        notifications.notify_threshold(
            "big_loss",
            {"market_title": "x", "pnl_usd": -100.0, "reason": "stop_loss"},
        )
        self.assertEqual(sent, [])


class TestHeartbeat(NotificationsBaseTest):
    def _setup(self) -> list[dict]:
        os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
        os.environ["TELEGRAM_CHAT_ID_LIVE"] = "111"
        sent: list[dict] = []
        notifications.set_transport_for_test(lambda payload: sent.append(payload) or True)
        return sent

    def _snapshot(self, **overrides) -> dict:
        base: dict = {
            "equity_usd": 87.40,
            "cash_usd": 12.0,
            "unrealized_pnl_usd": 3.40,
            "open_positions": 7,
            "trades_24h": 4,
            "wins_24h": 3,
            "losses_24h": 1,
            "realized_pnl_24h_usd": 3.30,
            "top_winner": {"pnl_usd": 4.20, "title": "Trump 2028 GOP nominee"},
            "top_loser": {"pnl_usd": -1.10, "title": "BTC >120k Friday"},
        }
        base.update(overrides)
        return base

    def test_heartbeat_multiline_with_all_fields(self) -> None:
        sent = self._setup()
        with tempfile.TemporaryDirectory() as tmpd:
            path = Path(tmpd) / "s.json"
            with patch.object(notifications, "_default_state_path", return_value=path):
                notifications.notify_heartbeat(self._snapshot())
        self.assertEqual(len(sent), 1)
        text = sent[0]["text"]
        # Header + 3 lignes corps + ligne vide + 2 lignes top/flop = 6 sauts
        self.assertGreaterEqual(text.count("\n"), 5)
        self.assertIn("💓 *Bilan*", text)
        # Convention FR : signe `$` après le nombre
        self.assertIn("87\\.40$", text)  # equity
        self.assertIn("12\\.00$", text)  # cash
        self.assertIn("14%", text)        # cash_pct = 12/87.4 = 13.7 → 14%
        self.assertIn("\\+3\\.40$", text) # non-réalisé
        self.assertIn("7 positions", text)
        self.assertIn("\\+3\\.30$", text) # réalisé 24h
        self.assertIn("3W/1L", text)
        self.assertIn("75%", text)
        # Top / flop avec titre
        self.assertIn("🏆", text)
        self.assertIn("💸", text)
        self.assertIn("Trump 2028 GOP nominee", text)
        self.assertIn("BTC \\>120k Friday", text)
        self.assertIn("\\+4\\.20$", text)
        self.assertIn("\\-1\\.10$", text)

    def test_heartbeat_no_positions(self) -> None:
        sent = self._setup()
        with tempfile.TemporaryDirectory() as tmpd:
            path = Path(tmpd) / "s.json"
            with patch.object(notifications, "_default_state_path", return_value=path):
                notifications.notify_heartbeat(
                    self._snapshot(open_positions=0, unrealized_pnl_usd=0.0),
                )
        text = sent[0]["text"]
        self.assertIn("aucune position ouverte", text)
        self.assertNotIn("non\\-réalisé", text)

    def test_heartbeat_no_loser_keeps_winner(self) -> None:
        sent = self._setup()
        with tempfile.TemporaryDirectory() as tmpd:
            path = Path(tmpd) / "s.json"
            with patch.object(notifications, "_default_state_path", return_value=path):
                notifications.notify_heartbeat(self._snapshot(top_loser={}))
        text = sent[0]["text"]
        self.assertIn("🏆", text)
        self.assertNotIn("💸", text)

    def test_heartbeat_truncates_long_title(self) -> None:
        sent = self._setup()
        long_title = "A" * 80
        with tempfile.TemporaryDirectory() as tmpd:
            path = Path(tmpd) / "s.json"
            with patch.object(notifications, "_default_state_path", return_value=path):
                notifications.notify_heartbeat(
                    self._snapshot(top_winner={"pnl_usd": 5.0, "title": long_title}),
                )
        text = sent[0]["text"]
        self.assertIn("…", text)
        self.assertNotIn("A" * 80, text)

    def test_heartbeat_respects_interval(self) -> None:
        sent = self._setup()
        os.environ["TELEGRAM_HEARTBEAT_MINUTES"] = "30"
        with tempfile.TemporaryDirectory() as tmpd:
            path = Path(tmpd) / "s.json"
            t0 = 1700000000.0
            with patch.object(notifications, "_default_state_path", return_value=path):
                with patch.object(notifications.time, "time", return_value=t0):
                    notifications.notify_heartbeat(self._snapshot())
                with patch.object(notifications.time, "time", return_value=t0 + 600):
                    notifications.notify_heartbeat(self._snapshot())
                with patch.object(notifications.time, "time", return_value=t0 + 31 * 60):
                    notifications.notify_heartbeat(self._snapshot())
        self.assertEqual(len(sent), 2)

    def test_heartbeat_disabled_by_toggle(self) -> None:
        sent = self._setup()
        os.environ["TELEGRAM_ALERT_HEARTBEAT"] = "0"
        with tempfile.TemporaryDirectory() as tmpd:
            path = Path(tmpd) / "s.json"
            with patch.object(notifications, "_default_state_path", return_value=path):
                notifications.notify_heartbeat(self._snapshot())
        self.assertEqual(sent, [])

    def test_heartbeat_no_trades_clean_format(self) -> None:
        sent = self._setup()
        with tempfile.TemporaryDirectory() as tmpd:
            path = Path(tmpd) / "s.json"
            with patch.object(notifications, "_default_state_path", return_value=path):
                notifications.notify_heartbeat(
                    self._snapshot(
                        trades_24h=0,
                        wins_24h=0,
                        losses_24h=0,
                        realized_pnl_24h_usd=0.0,
                        top_winner={},
                        top_loser={},
                    ),
                )
        self.assertEqual(len(sent), 1)
        text = sent[0]["text"]
        self.assertIn("aucun trade clôturé", text)
        # Pas de section top/flop quand aucun trade
        self.assertNotIn("🏆", text)
        self.assertNotIn("💸", text)
        self.assertNotIn("W/", text)


class TestDailySummaryRemoved(NotificationsBaseTest):
    def test_function_removed(self) -> None:
        self.assertFalse(hasattr(notifications, "notify_daily_summary"))


class TestStateMigration(NotificationsBaseTest):
    def test_loads_old_dedupe_seen_float_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmpd:
            path = Path(tmpd) / "state.json"
            path.write_text(json.dumps({
                "equity_peak_usd": 100.0,
                "dedupe_seen": {"err_a": 1700000000.0, "err_b": 1700000050.5},
            }))
            state = notifications._load_state(path)
        self.assertEqual(state.equity_peak_usd, 100.0)
        self.assertIn("err_a", state.dedupe_seen)
        self.assertEqual(state.dedupe_seen["err_a"]["count"], 1)
        self.assertEqual(state.dedupe_seen["err_a"]["first_ts"], 1700000000.0)
        self.assertEqual(state.dedupe_seen["err_a"]["last_ts"], 1700000000.0)
        self.assertEqual(state.dedupe_seen["err_a"]["last_message"], "")

    def test_loads_new_dedupe_seen_dict_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmpd:
            path = Path(tmpd) / "state.json"
            path.write_text(json.dumps({
                "dedupe_seen": {
                    "err_a": {
                        "first_ts": 1.0, "last_ts": 5.0,
                        "count": 3, "last_message": "boom",
                    },
                },
            }))
            state = notifications._load_state(path)
        entry = state.dedupe_seen["err_a"]
        self.assertEqual(entry["count"], 3)
        self.assertEqual(entry["last_message"], "boom")
        self.assertEqual(entry["first_ts"], 1.0)
        self.assertEqual(entry["last_ts"], 5.0)

    def test_ignores_legacy_last_daily_summary_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmpd:
            path = Path(tmpd) / "state.json"
            path.write_text(json.dumps({"last_daily_summary_date": "2024-01-01"}))
            state = notifications._load_state(path)
        self.assertFalse(hasattr(state, "last_daily_summary_date"))

    def test_loads_last_heartbeat_ts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpd:
            path = Path(tmpd) / "state.json"
            path.write_text(json.dumps({"last_heartbeat_ts": 1700000000.0}))
            state = notifications._load_state(path)
        self.assertEqual(state.last_heartbeat_ts, 1700000000.0)

    def test_save_round_trip_preserves_new_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpd:
            path = Path(tmpd) / "state.json"
            state = notifications._State(
                equity_peak_usd=200.0,
                last_heartbeat_ts=1700000123.0,
                dedupe_seen={
                    "err_a": {"first_ts": 1.0, "last_ts": 2.0, "count": 4, "last_message": "x"},
                },
            )
            notifications._save_state(path, state)
            loaded = notifications._load_state(path)
        self.assertEqual(loaded.equity_peak_usd, 200.0)
        self.assertEqual(loaded.last_heartbeat_ts, 1700000123.0)
        self.assertEqual(loaded.dedupe_seen["err_a"]["count"], 4)

    def test_prune_dedupe_removes_old_entries(self) -> None:
        state = notifications._State(
            dedupe_seen={
                "old": {"first_ts": 0.0, "last_ts": 0.0, "count": 1, "last_message": ""},
                "new": {"first_ts": 1500.0, "last_ts": 1500.0, "count": 1, "last_message": ""},
            },
        )
        # window=300, window*4=1200, cutoff = now - 1200 = 800
        # "old" last_ts=0 < 800 → supprimé ; "new" last_ts=1500 ≥ 800 → gardé
        notifications._prune_dedupe(state, now=2000.0, window=300.0)
        self.assertNotIn("old", state.dedupe_seen)
        self.assertIn("new", state.dedupe_seen)
