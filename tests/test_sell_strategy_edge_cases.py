"""Regression tests for ``_execute_sell_strategy`` edge cases.

These cover scenarios where the position is technically un-sellable
(resolved-loss market with no bid, missing market metadata) and verify
the function skips them quietly instead of raising and spamming
``notify_error`` on every tick.
"""

import os

os.environ["POLYMARKET_SKIP_DOTENV"] = "1"
for _k in [k for k in os.environ if k.startswith("POLYMARKET_") and k != "POLYMARKET_SKIP_DOTENV"]:
    del os.environ[_k]

import unittest
from datetime import timedelta

from polymarket_bot.config import Settings
from polymarket_bot.main import _execute_sell_strategy
from polymarket_bot.models import Candidate, utc_now
from polymarket_bot.portfolio import Portfolio


def _bid_zero_candidate() -> Candidate:
    return Candidate(
        market_id="1",
        question="Counter-Strike: Liquid vs Astralis - Map 2 Winner",
        slug="cs-liquid-astralis",
        end_date=utc_now() + timedelta(hours=1),
        hours_to_close=1.0,
        liquidity=0.0,
        volume=0.0,
        outcome="Liquid",
        price=0.0,
        token_id="tok-loser",
        score=0.0,
        url="https://polymarket.com",
        best_bid=0.0,
        best_ask=0.01,
        tick_size=0.01,
        neg_risk=False,
        accepts_orders=True,
    )


class _SilentClient:
    """A client that should never be touched: the candidate is unsellable."""

    def cancel_active_orders_for_token(self, token_id):  # pragma: no cover
        raise AssertionError(f"cancel called for {token_id} — should not happen")


class ExecuteSellStrategyEdgeCases(unittest.TestCase):
    def test_skip_silently_when_best_bid_is_zero(self):
        """Resolved-loss markets have no bid: skip without raising / notifying."""
        position = {
            "status": "open",
            "live": True,
            "market_id": "1",
            "outcome": "Liquid",
            "token_id": "tok-loser",
            "entry_price": 0.50,
            "stake": 50.0,
            "shares": 100.0,
            "initial_shares": 100.0,
            "peak_pnl_pct": 0.30,
        }
        portfolio = Portfolio(cash=0.0, positions=[position])
        candidates = [_bid_zero_candidate()]

        report = _execute_sell_strategy(
            _SilentClient(),
            Settings(),
            portfolio,
            candidates,
        )

        self.assertEqual(report, [])
        # Position still open, untouched.
        self.assertEqual(position["status"], "open")
        self.assertEqual(position["shares"], 100.0)


if __name__ == "__main__":
    unittest.main()
