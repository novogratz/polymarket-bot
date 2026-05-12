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
        wallet="0xtarget",
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
) -> Candidate:
    return Candidate(
        market_id="market-1",
        question="Will X happen?",
        slug="will-x-happen",
        end_date=None,
        hours_to_close=24.0,
        liquidity=1000.0,
        volume=5000.0,
        outcome="Yes",
        price=0.41,
        token_id=token_id,
        score=0.0,
        url="https://polymarket.com/event/will-x-happen",
        best_bid=best_bid,
        best_ask=best_ask,
        tick_size=0.01,
        accepts_orders=True,
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
                "POLYMARKET_MIRROR_TARGET": "0xTARGET",
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
        self.assertEqual(state, {"seen": [], "last_ts": 0})

    def test_save_then_load(self) -> None:
        mirror._save_state(self.mirror_state_path, {"seen": ["a", "b"], "last_ts": 123})
        loaded = mirror._load_state(self.mirror_state_path)
        self.assertEqual(loaded["last_ts"], 123)
        self.assertEqual(set(loaded["seen"]), {"a", "b"})

    def test_load_caps_seen_to_max(self) -> None:
        big = [str(i) for i in range(5000)]
        mirror._save_state(self.mirror_state_path, {"seen": big, "last_ts": 0})
        loaded = mirror._load_state(self.mirror_state_path)
        self.assertLessEqual(len(loaded["seen"]), mirror._MAX_SEEN)


class TestSelectEligible(MirrorBaseTest):
    def test_skips_trades_at_or_before_last_ts(self) -> None:
        trades = [
            _trade(timestamp=100, usdc_size=100.0),
            _trade(timestamp=200, usdc_size=100.0),
            _trade(timestamp=300, usdc_size=100.0),
        ]
        eligible = mirror._select_eligible(
            trades, last_ts=200, seen=set(), settings=self.settings
        )
        self.assertEqual([t.timestamp for t in eligible], [300])

    def test_skips_already_seen(self) -> None:
        trade = _trade(timestamp=500, usdc_size=100.0)
        seen = {mirror._trade_key(trade)}
        eligible = mirror._select_eligible(
            [trade], last_ts=0, seen=seen, settings=self.settings
        )
        self.assertEqual(eligible, [])

    def test_skips_below_min_target_stake(self) -> None:
        # min_target_stake_usd = 20 in setUp
        small = _trade(usdc_size=10.0)
        ok = _trade(usdc_size=50.0, timestamp=small.timestamp + 1)
        eligible = mirror._select_eligible(
            [small, ok], last_ts=0, seen=set(), settings=self.settings
        )
        self.assertEqual([t.usdc_size for t in eligible], [50.0])

    def test_skips_buy_outside_price_band(self) -> None:
        too_low = _trade(side="BUY", price=0.01, usdc_size=100.0, timestamp=1)
        too_high = _trade(side="BUY", price=0.99, usdc_size=100.0, timestamp=2)
        ok = _trade(side="BUY", price=0.50, usdc_size=100.0, timestamp=3)
        eligible = mirror._select_eligible(
            [too_low, too_high, ok], last_ts=0, seen=set(), settings=self.settings
        )
        self.assertEqual([t.price for t in eligible], [0.50])

    def test_sell_skips_price_band_check(self) -> None:
        # SELL trades aren't price-band filtered (we always want to follow the exit).
        sell_low = _trade(side="SELL", price=0.01, usdc_size=100.0, timestamp=1)
        eligible = mirror._select_eligible(
            [sell_low], last_ts=0, seen=set(), settings=self.settings
        )
        self.assertEqual(len(eligible), 1)

    def test_ignores_unknown_sides(self) -> None:
        weird = _trade(side="HEDGE", usdc_size=100.0)
        eligible = mirror._select_eligible(
            [weird], last_ts=0, seen=set(), settings=self.settings
        )
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

        # State persisted with this trade key + last_ts advanced.
        state = mirror._load_state(self.mirror_state_path)
        self.assertEqual(state["last_ts"], 500)
        self.assertIn(mirror._trade_key(trade), state["seen"])

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
