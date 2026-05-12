"""Tests for the mirror copy-trading mode (polymarket_bot/mirror.py)."""

from __future__ import annotations

import os

os.environ["POLYMARKET_SKIP_DOTENV"] = "1"
for _k in [k for k in os.environ if k.startswith("POLYMARKET_") and k != "POLYMARKET_SKIP_DOTENV"]:
    del os.environ[_k]

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from polymarket_bot import mirror
from polymarket_bot.config import Settings
from polymarket_bot.models import Candidate
from polymarket_bot.smart_money import SmartTrade

# Real 40-hex addresses so wallet_resolver.is_address() passes and we never
# hit the leaderboard API during tests.
TARGET_ADDR = "0x1111111111111111111111111111111111111111"
TARGET_ADDR_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TARGET_ADDR_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
TARGET_ADDR_OTHER = "0x9999999999999999999999999999999999999999"
TARGET_ADDR_NEW = "0x2222222222222222222222222222222222222222"


def _trade(
    *,
    asset: str = "tok-abc",
    side: str = "BUY",
    price: float = 0.40,
    size: float = 100.0,
    usdc_size: float = 40.0,
    timestamp: int = 1_700_000_000,
    title: str = "Will X happen?",
    outcome: str = "Yes",
    slug: str = "will-x-happen",
) -> SmartTrade:
    return SmartTrade(
        wallet=TARGET_ADDR,
        asset=asset,
        side=side,
        price=price,
        size=size,
        usdc_size=usdc_size,
        timestamp=timestamp,
        title=title,
        outcome=outcome,
        slug=slug,
    )


def _candidate(
    *,
    token_id: str = "tok-abc",
    best_ask: float | None = 0.41,
    best_bid: float | None = 0.40,
    market_id: str | None = None,
    event_slug: str | None = None,
) -> Candidate:
    # market_id and event_slug derived from token_id so multi-target tests
    # don't accidentally share an event identity (which would trigger
    # duplicate-position rejection in execute_live_trade).
    slug = event_slug or f"event-{token_id}"
    return Candidate(
        market_id=market_id or f"market-{token_id}",
        question=f"Will {token_id} happen?",
        slug=slug,
        end_date=None,
        hours_to_close=24.0,
        liquidity=1000.0,
        volume=5000.0,
        outcome="Yes",
        price=0.41,
        token_id=token_id,
        score=0.0,
        url=f"https://polymarket.com/event/{slug}",
        best_bid=best_bid,
        best_ask=best_ask,
        tick_size=0.01,
        accepts_orders=True,
        event_slug=slug,
    )


class MirrorBaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        self.state_path = self.tmpdir / "paper_state.json"
        self.mirror_state_path = self.tmpdir / "mirror_state.json"
        self._env = mock.patch.dict(
            os.environ,
            {
                "POLYMARKET_STATE_PATH": str(self.state_path),
                "POLYMARKET_MIRROR_STATE_PATH": str(self.mirror_state_path),
                "POLYMARKET_MIRROR_TARGET": TARGET_ADDR,
                "POLYMARKET_RUN_MODE": "mirror",
                "POLYMARKET_DRY_RUN": "1",
                "POLYMARKET_MIRROR_SIZE_USD": "5.0",
                "POLYMARKET_MIRROR_MIN_TARGET_STAKE_USD": "20.0",
                "POLYMARKET_MIRROR_MAX_CHASE_PREMIUM": "0.10",
                "POLYMARKET_MIRROR_MIN_BUY_PRICE": "0.05",
                "POLYMARKET_MIRROR_MAX_BUY_PRICE": "0.95",
                "POLYMARKET_PAPER_BALANCE_USD": "100.0",
            },
            clear=False,
        )
        self._env.start()
        self.settings = Settings()

    def tearDown(self) -> None:
        self._env.stop()
        self.tmp.cleanup()


class TestStateRoundTrip(MirrorBaseTest):
    def test_load_missing_returns_defaults(self) -> None:
        state = mirror._load_state(self.mirror_state_path)
        self.assertEqual(state["seen"], [])
        self.assertEqual(state["last_ts_by_target"], {})

    def test_save_then_load(self) -> None:
        mirror._save_state(
            self.mirror_state_path,
            {
                "seen": ["a", "b"],
                "last_ts_by_target": {TARGET_ADDR_A: 123, TARGET_ADDR_B: 456},
            },
        )
        loaded = mirror._load_state(self.mirror_state_path)
        self.assertEqual(
            loaded["last_ts_by_target"], {TARGET_ADDR_A: 123, TARGET_ADDR_B: 456}
        )
        self.assertEqual(set(loaded["seen"]), {"a", "b"})

    def test_load_caps_seen_to_max(self) -> None:
        big = [str(i) for i in range(5000)]
        mirror._save_state(
            self.mirror_state_path, {"seen": big, "last_ts_by_target": {}}
        )
        loaded = mirror._load_state(self.mirror_state_path)
        self.assertLessEqual(len(loaded["seen"]), mirror._MAX_SEEN)

    def test_legacy_last_ts_used_as_fallback(self) -> None:
        # Pre-multi-target state had a single global ``last_ts``. Should be
        # exposed via ``legacy_last_ts`` so the first multi-target tick on a
        # never-seen target doesn't replay history.
        self.mirror_state_path.write_text(
            json.dumps({"seen": [], "last_ts": 999}), encoding="utf-8"
        )
        loaded = mirror._load_state(self.mirror_state_path)
        self.assertEqual(loaded["legacy_last_ts"], 999)
        self.assertEqual(mirror._last_ts_for(loaded, TARGET_ADDR_NEW), 999)


class TestParseTargets(MirrorBaseTest):
    def test_single_address(self) -> None:
        self.assertEqual(mirror._parse_targets(TARGET_ADDR.upper()), [TARGET_ADDR])

    def test_csv_with_whitespace(self) -> None:
        self.assertEqual(
            mirror._parse_targets(f"{TARGET_ADDR_A.upper()}, {TARGET_ADDR_B} ,{TARGET_ADDR_OTHER}"),
            [TARGET_ADDR_A, TARGET_ADDR_B, TARGET_ADDR_OTHER],
        )

    def test_dedupe_preserves_order(self) -> None:
        self.assertEqual(
            mirror._parse_targets(f"{TARGET_ADDR_A},{TARGET_ADDR_B},{TARGET_ADDR_A.upper()}"),
            [TARGET_ADDR_A, TARGET_ADDR_B],
        )

    def test_empty(self) -> None:
        self.assertEqual(mirror._parse_targets(""), [])
        self.assertEqual(mirror._parse_targets("  ,  "), [])


class TestSelectEligible(MirrorBaseTest):
    TARGET = TARGET_ADDR

    def _eligible(
        self, trades: list[SmartTrade], *, last_ts: int = 0, seen: set[str] | None = None
    ) -> list[SmartTrade]:
        return mirror._select_eligible(
            trades,
            target=self.TARGET,
            last_ts=last_ts,
            seen=seen or set(),
            settings=self.settings,
        )

    def test_skips_trades_at_or_before_last_ts(self) -> None:
        trades = [
            _trade(timestamp=100, usdc_size=100.0),
            _trade(timestamp=200, usdc_size=100.0),
            _trade(timestamp=300, usdc_size=100.0),
        ]
        eligible = self._eligible(trades, last_ts=200)
        self.assertEqual([t.timestamp for t in eligible], [300])

    def test_skips_already_seen(self) -> None:
        trade = _trade(timestamp=500, usdc_size=100.0)
        seen = {mirror._trade_key(self.TARGET, trade)}
        eligible = self._eligible([trade], seen=seen)
        self.assertEqual(eligible, [])

    def test_seen_is_scoped_per_target(self) -> None:
        # A key registered for another target must NOT mask a trade on ours.
        trade = _trade(timestamp=500, usdc_size=100.0)
        seen = {mirror._trade_key(TARGET_ADDR_OTHER, trade)}
        eligible = self._eligible([trade], seen=seen)
        self.assertEqual(len(eligible), 1)

    def test_skips_below_min_target_stake(self) -> None:
        small = _trade(usdc_size=10.0)
        ok = _trade(usdc_size=50.0, timestamp=small.timestamp + 1)
        eligible = self._eligible([small, ok])
        self.assertEqual([t.usdc_size for t in eligible], [50.0])

    def test_skips_buy_outside_price_band(self) -> None:
        too_low = _trade(side="BUY", price=0.01, usdc_size=100.0, timestamp=1)
        too_high = _trade(side="BUY", price=0.99, usdc_size=100.0, timestamp=2)
        ok = _trade(side="BUY", price=0.50, usdc_size=100.0, timestamp=3)
        eligible = self._eligible([too_low, too_high, ok])
        self.assertEqual([t.price for t in eligible], [0.50])

    def test_sell_skips_price_band_check(self) -> None:
        sell_low = _trade(side="SELL", price=0.01, usdc_size=100.0, timestamp=1)
        eligible = self._eligible([sell_low])
        self.assertEqual(len(eligible), 1)

    def test_ignores_unknown_sides(self) -> None:
        weird = _trade(side="HEDGE", usdc_size=100.0)
        eligible = self._eligible([weird])
        self.assertEqual(eligible, [])


class TestMirrorOnce(MirrorBaseTest):
    def test_empty_target_returns_noop(self) -> None:
        os.environ["POLYMARKET_MIRROR_TARGET"] = ""
        settings = Settings()
        result = mirror.mirror_once(settings)
        self.assertEqual(result["scan_counts"]["polled"], 0)
        self.assertEqual(result["scan_counts"]["mirrored"], 0)

    def test_mirrors_eligible_buy_and_persists_state(self) -> None:
        trade = _trade(asset="tok-abc", side="BUY", price=0.40, usdc_size=100.0, timestamp=500)
        candidate = _candidate(token_id="tok-abc", best_ask=0.41)

        with mock.patch.object(
            mirror.DataApiClient, "trades", return_value=[trade]
        ), mock.patch.object(
            mirror, "_candidate_for_token", return_value=candidate
        ):
            result = mirror.mirror_once(self.settings)

        self.assertEqual(result["scan_counts"]["polled"], 1)
        self.assertEqual(result["scan_counts"]["eligible"], 1)
        self.assertEqual(result["scan_counts"]["mirrored"], 1)
        actions = result["actions"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "buy")
        self.assertEqual(actions[0]["token_id"], "tok-abc")

        # State persisted with this trade key + last_ts advanced for the target.
        state = mirror._load_state(self.mirror_state_path)
        self.assertEqual(state["last_ts_by_target"][TARGET_ADDR], 500)
        self.assertIn(mirror._trade_key(TARGET_ADDR, trade), state["seen"])

    def test_idempotent_across_ticks(self) -> None:
        trade = _trade(asset="tok-abc", side="BUY", price=0.40, usdc_size=100.0, timestamp=600)
        candidate = _candidate(token_id="tok-abc", best_ask=0.41)

        with mock.patch.object(
            mirror.DataApiClient, "trades", return_value=[trade]
        ), mock.patch.object(
            mirror, "_candidate_for_token", return_value=candidate
        ):
            r1 = mirror.mirror_once(self.settings)
            r2 = mirror.mirror_once(self.settings)

        self.assertEqual(r1["scan_counts"]["mirrored"], 1)
        self.assertEqual(r2["scan_counts"]["mirrored"], 0)
        self.assertEqual(r2["scan_counts"]["eligible"], 0)

    def test_chase_premium_blocks_buy(self) -> None:
        # Wallet bought at 0.40, current ask 0.50 → premium 25% > 10% threshold.
        trade = _trade(price=0.40, usdc_size=100.0, timestamp=700)
        candidate = _candidate(best_ask=0.50)

        with mock.patch.object(
            mirror.DataApiClient, "trades", return_value=[trade]
        ), mock.patch.object(
            mirror, "_candidate_for_token", return_value=candidate
        ):
            result = mirror.mirror_once(self.settings)

        self.assertEqual(result["scan_counts"]["mirrored"], 0)
        self.assertEqual(result["actions"][0]["reason"], "chase_premium")

    def test_skip_when_market_not_found(self) -> None:
        trade = _trade(asset="tok-missing", usdc_size=100.0, timestamp=800)

        with mock.patch.object(
            mirror.DataApiClient, "trades", return_value=[trade]
        ), mock.patch.object(
            mirror, "_candidate_for_token", return_value=None
        ):
            result = mirror.mirror_once(self.settings)

        self.assertEqual(result["scan_counts"]["mirrored"], 0)
        self.assertEqual(result["actions"][0]["reason"], "no_market")

    def test_sells_disabled_skips_sell(self) -> None:
        os.environ["POLYMARKET_MIRROR_MIRROR_SELLS"] = "0"
        settings = Settings()
        trade = _trade(side="SELL", usdc_size=100.0, timestamp=900)
        candidate = _candidate()

        with mock.patch.object(
            mirror.DataApiClient, "trades", return_value=[trade]
        ), mock.patch.object(
            mirror, "_candidate_for_token", return_value=candidate
        ):
            result = mirror.mirror_once(settings)

        self.assertEqual(result["scan_counts"]["mirrored"], 0)
        self.assertEqual(result["actions"][0]["reason"], "sells_disabled")


class TestMirrorOnceMultiTarget(MirrorBaseTest):
    def setUp(self) -> None:
        super().setUp()
        os.environ["POLYMARKET_MIRROR_TARGET"] = f"{TARGET_ADDR_A}, {TARGET_ADDR_B}"
        self.settings = Settings()

    def test_polls_both_targets_and_mirrors_each(self) -> None:
        trade_a = _trade(asset="tok-A", side="BUY", price=0.40, usdc_size=100.0, timestamp=500)
        trade_b = _trade(asset="tok-B", side="BUY", price=0.30, usdc_size=200.0, timestamp=600)
        cand_a = _candidate(token_id="tok-A", best_ask=0.41)
        cand_b = _candidate(token_id="tok-B", best_ask=0.31)

        def fake_trades(self, *, user, **kwargs):
            return [trade_a] if user.lower() == TARGET_ADDR_A else [trade_b]

        def fake_candidate(token_id, _gamma):
            return cand_a if token_id == "tok-A" else cand_b

        with mock.patch.object(
            mirror.DataApiClient, "trades", autospec=True, side_effect=fake_trades
        ), mock.patch.object(
            mirror, "_candidate_for_token", side_effect=fake_candidate
        ):
            result = mirror.mirror_once(self.settings)

        self.assertEqual(result["scan_counts"]["polled"], 2)
        self.assertEqual(result["scan_counts"]["eligible"], 2)
        self.assertEqual(result["scan_counts"]["mirrored"], 2)
        targets_in_actions = {a.get("target") for a in result["actions"]}
        self.assertEqual(len(targets_in_actions), 2)

        # last_ts advanced INDEPENDENTLY per target.
        state = mirror._load_state(self.mirror_state_path)
        self.assertEqual(state["last_ts_by_target"][TARGET_ADDR_A], 500)
        self.assertEqual(state["last_ts_by_target"][TARGET_ADDR_B], 600)

    def test_actions_ordered_chronologically_across_targets(self) -> None:
        trade_a = _trade(asset="tok-A", price=0.40, usdc_size=100.0, timestamp=900)
        trade_b = _trade(asset="tok-B", price=0.30, usdc_size=200.0, timestamp=100)
        cand_a = _candidate(token_id="tok-A", best_ask=0.41)
        cand_b = _candidate(token_id="tok-B", best_ask=0.31)

        def fake_trades(self, *, user, **kwargs):
            return [trade_a] if user.lower() == TARGET_ADDR_A else [trade_b]

        def fake_candidate(token_id, _gamma):
            return cand_a if token_id == "tok-A" else cand_b

        with mock.patch.object(
            mirror.DataApiClient, "trades", autospec=True, side_effect=fake_trades
        ), mock.patch.object(
            mirror, "_candidate_for_token", side_effect=fake_candidate
        ):
            result = mirror.mirror_once(self.settings)

        # B (ts=100) should be mirrored BEFORE A (ts=900) regardless of target order.
        token_order = [a["token_id"] for a in result["actions"]]
        self.assertEqual(token_order, ["tok-B", "tok-A"])


class TestFormatRecentTrades(MirrorBaseTest):
    def test_empty_trades_yields_placeholder(self) -> None:
        rows = mirror.format_recent_trades([], now_ts=1_700_000_000, limit=10)
        self.assertEqual(rows, ["(no recent trades found for this wallet)"])

    def test_rows_sorted_most_recent_first(self) -> None:
        trades = [
            _trade(timestamp=1_700_000_000, title="oldest"),
            _trade(timestamp=1_700_000_900, title="recent"),
            _trade(timestamp=1_700_000_500, title="middle"),
        ]
        rows = mirror.format_recent_trades(trades, now_ts=1_700_001_000, limit=10)
        body = rows[1:]
        self.assertIn("recent", body[0])
        self.assertIn("middle", body[1])
        self.assertIn("oldest", body[2])

    def test_limit_caps_rows(self) -> None:
        trades = [_trade(timestamp=i, title=f"t{i}") for i in range(50)]
        rows = mirror.format_recent_trades(trades, now_ts=100, limit=5)
        # header + 5 body rows = 6
        self.assertEqual(len(rows), 6)

    def test_age_column_uses_relative_format(self) -> None:
        trade = _trade(timestamp=1_700_000_000)
        rows = mirror.format_recent_trades([trade], now_ts=1_700_000_120, limit=10)
        self.assertIn("2m", rows[1])

    def test_age_clamped_when_now_before_trade(self) -> None:
        trade = _trade(timestamp=1_700_000_500)
        rows = mirror.format_recent_trades([trade], now_ts=1_700_000_000, limit=10)
        self.assertIn("0s", rows[1])

    def test_includes_side_stake_price_outcome(self) -> None:
        trade = _trade(side="SELL", usdc_size=1234.56, price=0.732, outcome="No", title="Some market")
        rows = mirror.format_recent_trades([trade], now_ts=trade.timestamp + 5, limit=10)
        body = rows[1]
        self.assertIn("SELL", body)
        self.assertIn("$1,235", body)
        self.assertIn("0.732", body)
        self.assertIn("No", body)
        self.assertIn("Some market", body)


class TestProfileSchema(MirrorBaseTest):
    def test_copy_wallet_profile_loads(self) -> None:
        from polymarket_bot.profiles import load_profile

        repo_root = Path(__file__).resolve().parent.parent
        profile_path = repo_root / "configs" / "profiles" / "copy-wallet.toml"
        loaded = load_profile(profile_path)
        self.assertEqual(loaded.values.get("POLYMARKET_RUN_MODE"), "mirror")
        self.assertIn("POLYMARKET_MIRROR_TARGET", loaded.values)
        self.assertEqual(loaded.values.get("POLYMARKET_MIRROR_SIZE_USD"), "5.0")


if __name__ == "__main__":
    unittest.main()
