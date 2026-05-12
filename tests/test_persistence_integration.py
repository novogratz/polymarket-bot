"""Intégration : pipeline complet smart_money + filtre persistance.

Vérifie que :
1. Le filtre se branche bien sans casser le pipeline
2. Le SmartMoneyData propage correctement les signals
3. Le bypass (persistence_enabled=False) restaure le comportement antérieur
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("POLYMARKET_SKIP_DOTENV", "1")

from polymarket_bot.config import Settings
from polymarket_bot.smart_money import (
    SmartTrade,
    SmartTrader,
    fetch_smart_money_data,
)


class FakeApiClient:
    """Stub DataApiClient pour les tests d'intégration."""

    def __init__(
        self,
        leaderboards: dict[tuple[str, str], list[SmartTrader]],
        trades_by_wallet: dict[str, list[SmartTrade]],
    ) -> None:
        self._leaderboards = leaderboards
        self._trades = trades_by_wallet

    def leaderboard(self, *, category: str, time_period: str, limit: int) -> list[SmartTrader]:
        return list(self._leaderboards.get((time_period, category), []))[:limit]

    def trades(self, *, user: str, start: int, limit: int = 100, side: str | None = "BUY") -> list[SmartTrade]:
        return list(self._trades.get(user.lower(), []))


class TestPersistenceIntegration(unittest.TestCase):
    def _trader(self, wallet: str, pnl: float = 5000.0, volume: float = 20000.0) -> SmartTrader:
        return SmartTrader(wallet=wallet, username=wallet, pnl=pnl, volume=volume, category="ALL")

    def test_filter_reduces_cohort(self) -> None:
        """3 traders, persistence ON, 2 qualifient (intersection ≥ 2/3)."""
        with TemporaryDirectory() as tmp:
            # 0xa dans 3/3, 0xb dans 1/3, 0xc dans 2/3
            traders_w = [self._trader("0xa"), self._trader("0xc")]
            traders_m = [self._trader("0xa"), self._trader("0xb"), self._trader("0xc")]
            traders_all = [self._trader("0xa")]
            leaderboards = {
                ("WEEK", "ALL"): traders_w,
                ("MONTH", "ALL"): traders_m,
                ("ALL", "ALL"): traders_all,
            }
            client = FakeApiClient(leaderboards, trades_by_wallet={})
            settings = Settings(
                smart_time_periods="WEEK,MONTH,ALL",
                smart_categories="ALL",
                smart_leaderboard_limit=10,
                persistence_enabled=True,
                persistence_cache_path=Path(tmp) / "history.json",
                persistence_window_days=30,
                persistence_intersect_min=2,
                quiet=True,
                # Désactive PnL/Vol/ROI pre-filters pour isoler le filtre persistance
                smart_min_trader_pnl=0.0,
                smart_min_trader_volume=0.0,
                smart_min_trader_roi=0.0,
            )
            data = fetch_smart_money_data(settings, client=client)
            self.assertEqual(data.cohort_before_persistence, 3)
            self.assertEqual(data.cohort_after_persistence, 2)
            qualified_wallets = {s.wallet for s in data.persistence_signals.values() if s.qualified}
            self.assertEqual(qualified_wallets, {"0xa", "0xc"})

    def test_bypass_disabled(self) -> None:
        """persistence_enabled=False : pas de signals, cohorte intacte."""
        with TemporaryDirectory() as tmp:
            traders_m = [self._trader("0xa"), self._trader("0xb"), self._trader("0xc")]
            leaderboards = {("MONTH", "ALL"): traders_m}
            client = FakeApiClient(leaderboards, trades_by_wallet={})
            settings = Settings(
                smart_time_periods="MONTH",
                smart_categories="ALL",
                smart_leaderboard_limit=10,
                persistence_enabled=False,
                persistence_cache_path=Path(tmp) / "history.json",
                quiet=True,
                smart_min_trader_pnl=0.0,
                smart_min_trader_volume=0.0,
                smart_min_trader_roi=0.0,
            )
            data = fetch_smart_money_data(settings, client=client)
            # Bypass : pas de signals, cohorte intacte
            self.assertEqual(data.persistence_signals, {})
            self.assertEqual(len(data.traders), 3)


if __name__ == "__main__":
    unittest.main()
