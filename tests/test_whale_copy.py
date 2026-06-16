"""Tests for the whale-copy pass (bot 2's second trigger).

fetch_whale_signals copies ANY single wallet's large buy, leaderboard or not,
but only on tokens in the already-vetted eligible universe.
"""

import os
os.environ["POLYMARKET_SKIP_DOTENV"] = "1"

import unittest
from dataclasses import replace

from polymarket_bot.config import Settings
from polymarket_bot.models import Candidate
from polymarket_bot.smart_money import SmartTrade, fetch_whale_signals


def _candidate(token_id: str, question: str = "Will X happen?") -> Candidate:
    return Candidate(
        market_id="m-" + token_id,
        question=question,
        slug="will-x",
        end_date=None,
        hours_to_close=10.0,
        liquidity=5000.0,
        volume=4000.0,
        outcome="Yes",
        price=0.6,
        token_id=token_id,
        score=0.0,
        url="https://polymarket.com",
        best_bid=0.58,
        best_ask=0.60,
        tick_size=0.01,
        accepts_orders=True,
    )


class _FakeClient:
    """Stands in for DataApiClient.recent_trades."""

    def __init__(self, trades):
        self._trades = trades
        self.calls = []

    def recent_trades(self, *, start, limit, side, min_usdc):
        self.calls.append({"start": start, "limit": limit, "side": side, "min_usdc": min_usdc})
        return list(self._trades)


def _trade(wallet, asset, usdc, ts=1_000_000, size=None, side="BUY"):
    px = 0.6
    size = size if size is not None else usdc / px
    return SmartTrade(
        wallet=wallet, asset=asset, side=side, price=px, size=size,
        usdc_size=usdc, timestamp=ts, title="Big Game", outcome="Yes", slug="g",
    )


class WhaleCopyTests(unittest.TestCase):
    def _settings(self, **over):
        base = dict(
            smart_whale_copy_enabled=True,
            smart_whale_min_usdc=50000.0,
            smart_whale_lookback_minutes=60,
            smart_whale_max_orders_per_tick=2,
            smart_whale_fetch_limit=500,
        )
        base.update(over)
        return replace(Settings(), **base)

    def test_disabled_returns_empty(self):
        s = self._settings(smart_whale_copy_enabled=False)
        client = _FakeClient([_trade("w1", "tokA", 80000)])
        self.assertEqual(fetch_whale_signals(s, [_candidate("tokA")], client=client), [])

    def test_single_big_buy_qualifies(self):
        s = self._settings()
        client = _FakeClient([_trade("w1", "tokA", 80000, ts=999_000)])
        sigs = fetch_whale_signals(s, [_candidate("tokA")], client=client, now_ts=1_000_000)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].source, "whale")
        self.assertEqual(sigs[0].consensus, 1)
        self.assertEqual(sigs[0].wallets, ["w1"])
        self.assertAlmostEqual(sigs[0].copied_usdc, 80000.0)

    def test_below_threshold_dropped(self):
        s = self._settings()
        client = _FakeClient([_trade("w1", "tokA", 49999)])
        self.assertEqual(fetch_whale_signals(s, [_candidate("tokA")], client=client), [])

    def test_multiple_buys_same_wallet_aggregate(self):
        # Two $30k buys by one wallet on one token = $60k >= threshold.
        s = self._settings()
        client = _FakeClient([_trade("w1", "tokA", 30000), _trade("w1", "tokA", 30000)])
        sigs = fetch_whale_signals(s, [_candidate("tokA")], client=client)
        self.assertEqual(len(sigs), 1)
        self.assertAlmostEqual(sigs[0].copied_usdc, 60000.0)

    def test_token_not_in_eligible_universe_skipped(self):
        # Whale bought a token we did NOT scan / that failed exclusions: skip.
        s = self._settings()
        client = _FakeClient([_trade("w1", "tokB", 90000)])
        self.assertEqual(fetch_whale_signals(s, [_candidate("tokA")], client=client), [])

    def test_largest_wallet_wins_per_token(self):
        s = self._settings()
        client = _FakeClient([_trade("w1", "tokA", 60000), _trade("w2", "tokA", 90000)])
        sigs = fetch_whale_signals(s, [_candidate("tokA")], client=client)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].wallets, ["w2"])
        self.assertAlmostEqual(sigs[0].copied_usdc, 90000.0)

    def test_sells_ignored(self):
        s = self._settings()
        client = _FakeClient([_trade("w1", "tokA", 90000, side="SELL")])
        self.assertEqual(fetch_whale_signals(s, [_candidate("tokA")], client=client), [])

    def test_lookback_window_passed_to_client(self):
        s = self._settings(smart_whale_lookback_minutes=30)
        client = _FakeClient([])
        fetch_whale_signals(s, [_candidate("tokA")], client=client, now_ts=1_000_000)
        self.assertEqual(client.calls[0]["start"], 1_000_000 - 30 * 60)
        self.assertEqual(client.calls[0]["min_usdc"], 50000.0)


if __name__ == "__main__":
    unittest.main()
