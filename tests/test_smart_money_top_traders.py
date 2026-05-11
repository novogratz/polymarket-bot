"""Test du refactor _top_traders → dict[period, list[SmartTrader]]."""
from __future__ import annotations

import unittest

from polymarket_bot.config import Settings
from polymarket_bot.smart_money import SmartTrader, _top_traders


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._responses: dict[tuple[str, str], list[SmartTrader]] = {}

    def add(self, period: str, category: str, traders: list[SmartTrader]) -> None:
        self._responses[(period, category)] = traders

    def leaderboard(self, *, category: str, time_period: str, limit: int) -> list[SmartTrader]:
        self.calls.append((time_period, category))
        return list(self._responses.get((time_period, category), []))


class TestTopTradersByPeriod(unittest.TestCase):
    def _trader(self, wallet: str) -> SmartTrader:
        return SmartTrader(wallet=wallet, username=wallet.upper(), pnl=100.0, volume=200.0, category="ALL")

    def test_returns_dict_per_period(self) -> None:
        client = FakeClient()
        client.add("WEEK", "ALL", [self._trader("0xa")])
        client.add("MONTH", "ALL", [self._trader("0xb")])
        client.add("ALL", "ALL", [self._trader("0xc")])

        settings = Settings(
            smart_time_periods="WEEK,MONTH,ALL",
            smart_categories="ALL",
            smart_leaderboard_limit=10,
            quiet=True,
        )
        out = _top_traders(client, settings)
        self.assertIsInstance(out, dict)
        self.assertEqual(set(out.keys()), {"WEEK", "MONTH", "ALL"})
        self.assertEqual([t.wallet for t in out["WEEK"]], ["0xa"])
        self.assertEqual([t.wallet for t in out["MONTH"]], ["0xb"])
        self.assertEqual([t.wallet for t in out["ALL"]], ["0xc"])

    def test_dedup_within_period(self) -> None:
        """Si une catégorie retourne le même wallet plusieurs fois, dedupe par période."""
        client = FakeClient()
        client.add("MONTH", "POLITICS", [self._trader("0xa"), self._trader("0xa")])
        client.add("MONTH", "SPORTS", [self._trader("0xa")])
        settings = Settings(
            smart_time_periods="MONTH",
            smart_categories="POLITICS,SPORTS",
            smart_leaderboard_limit=10,
            quiet=True,
        )
        out = _top_traders(client, settings)
        # Une seule entrée 0xa dans MONTH même après plusieurs apparitions
        self.assertEqual(len([t for t in out["MONTH"] if t.wallet.lower() == "0xa"]), 1)


if __name__ == "__main__":
    unittest.main()
