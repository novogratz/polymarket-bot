from datetime import timedelta
import unittest

from polymarket_bot.bitcoin import BtcModel, btc_signal, btc_terminal_probability, parse_btc_threshold
from polymarket_bot.config import Settings
from polymarket_bot.models import Candidate, utc_now
from polymarket_bot.polymarket import ApiCreds, PolymarketClient
from polymarket_bot.portfolio import Portfolio
from polymarket_bot.smart_money import SmartTrade, smart_money_signals
from polymarket_bot.strategy import rank_markets, stake_for_candidate
from polymarket_bot.trading import execute_live_sell, execute_live_trade
from polymarket_bot.main import _max_trade_for_signal, _sell_plan


class StrategyTests(unittest.TestCase):
    def test_rank_markets_filters_and_scores_soon_markets(self):
        end_date = (utc_now() + timedelta(hours=12)).isoformat().replace("+00:00", "Z")
        markets = [
            {
                "id": "1",
                "question": "Will test pass?",
                "slug": "will-test-pass",
                "endDate": end_date,
                "liquidity": "1000",
                "volume": "2000",
                "outcomes": '["Yes","No"]',
                "outcomePrices": '["0.62","0.38"]',
                "clobTokenIds": '["yes-token","no-token"]',
            }
        ]
        settings = Settings(min_liquidity_usd=100, min_volume_usd=100, soon_hours=24)
        candidates = rank_markets(markets, settings)

        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0].market_id, "1")
        self.assertIn(candidates[0].outcome, {"Yes", "No"})
        self.assertGreater(candidates[0].score, 0)

    def test_rank_markets_maps_binary_quotes_to_each_outcome(self):
        end_date = (utc_now() + timedelta(hours=12)).isoformat().replace("+00:00", "Z")
        candidates = rank_markets(
            [
                {
                    "id": "1",
                    "question": "Team A vs. Team B",
                    "slug": "team-a-team-b",
                    "endDate": end_date,
                    "liquidity": "1000",
                    "volume": "2000",
                    "bestBid": "0.11",
                    "bestAsk": "0.12",
                    "orderPriceMinTickSize": "0.01",
                    "acceptingOrders": True,
                    "outcomes": '["Team A","Team B"]',
                    "outcomePrices": '["0.115","0.885"]',
                    "clobTokenIds": '["team-a-token","team-b-token"]',
                }
            ],
            Settings(min_liquidity_usd=100, min_volume_usd=100, soon_hours=24),
        )

        by_outcome = {candidate.outcome: candidate for candidate in candidates}
        self.assertEqual(by_outcome["Team A"].best_bid, 0.11)
        self.assertEqual(by_outcome["Team A"].best_ask, 0.12)
        self.assertEqual(by_outcome["Team B"].best_bid, 0.88)
        self.assertEqual(by_outcome["Team B"].best_ask, 0.89)

    def test_stake_is_capped(self):
        end_date = (utc_now() + timedelta(hours=1)).isoformat()
        candidate = rank_markets(
            [
                {
                    "id": "1",
                    "question": "Q",
                    "slug": "q",
                    "endDate": end_date,
                    "liquidity": "1000",
                    "volume": "2000",
                    "outcomes": '["Yes"]',
                    "outcomePrices": '["0.50"]',
                }
            ],
            Settings(min_liquidity_usd=0, min_volume_usd=0),
        )[0]

        self.assertEqual(stake_for_candidate(candidate, 20.0, Settings(max_position_usd=5.0)), 5.0)

    def test_live_trade_respects_minimum_share_size(self):
        class FakeClient:
            def live_available_balance(self):
                return 20.0

            def place_live_order(self, *, candidate, price, size, side="BUY"):
                return {"price": price, "size": size, "side": side}, {"success": True, "orderID": "order-1"}

        candidate = Candidate(
            market_id="1",
            question="Q",
            slug="q",
            end_date=utc_now() + timedelta(hours=1),
            hours_to_close=1,
            liquidity=1000,
            volume=2000,
            outcome="Yes",
            price=0.5,
            token_id="token",
            score=1,
            url="https://polymarket.com/event/q",
            best_bid=0.39,
            best_ask=0.4,
            tick_size=0.01,
            accepts_orders=True,
        )
        portfolio = __import__("polymarket_bot.portfolio", fromlist=["Portfolio"]).Portfolio(cash=20.0, positions=[])
        result = execute_live_trade(
            FakeClient(),
            Settings(trade_fraction=0.10, min_order_shares=5.0),
            candidate,
            portfolio,
            min_trade_usd=1.0,
            max_trade_usd=1.0,
        )

        self.assertGreaterEqual(result.order["size"], 5.0)

    def test_live_sell_records_partial_exit(self):
        class FakeClient:
            def place_live_order(self, *, candidate, price, size, side="BUY"):
                return {"price": price, "size": size, "side": side}, {"success": True, "orderID": "sell-1"}

        candidate = Candidate(
            market_id="1",
            question="Q",
            slug="q",
            end_date=utc_now() + timedelta(hours=1),
            hours_to_close=1,
            liquidity=1000,
            volume=2000,
            outcome="Yes",
            price=0.5,
            token_id="token",
            score=1,
            url="https://polymarket.com/event/q",
            best_bid=0.4,
            best_ask=0.41,
            tick_size=0.01,
            accepts_orders=True,
        )
        position = {
            "status": "open",
            "live": True,
            "market_id": "1",
            "outcome": "Yes",
            "token_id": "token",
            "entry_price": 0.1,
            "stake": 10.0,
            "shares": 100.0,
            "initial_shares": 100.0,
        }
        portfolio = Portfolio(cash=0.0, positions=[position])

        result = execute_live_sell(
            FakeClient(),
            Settings(min_order_shares=5.0, smart_min_sell_usd=1.0),
            candidate,
            portfolio,
            position,
            shares=50.0,
            reason="take_profit_100pct",
        )

        self.assertEqual(result.order["side"], "SELL")
        self.assertEqual(position["shares"], 50.0)
        self.assertEqual(position["stake"], 5.0)
        self.assertEqual(position["realized_pnl"], 15.0)
        self.assertEqual(position["exits"][0]["order_id"], "sell-1")

    def test_sell_plan_uses_profit_tiers_and_peak_protection(self):
        position = {
            "shares": 100.0,
            "initial_shares": 100.0,
            "peak_pnl_pct": 1.2,
            "sell_tiers_hit": [],
            "exits": [],
        }
        plan = _sell_plan(position, 1.05, Settings())
        self.assertEqual(plan["reason"], "take_profit_100pct")
        self.assertEqual(plan["shares"], 50.0)

        protected = _sell_plan({**position, "sell_tiers_hit": ["1.0"]}, 0.35, Settings())
        self.assertEqual(protected["reason"], "peak_profit_protection")
        self.assertEqual(protected["shares"], 100.0)

    def test_build_limit_order_uses_expected_fields(self):
        client = PolymarketClient(
            "https://clob.polymarket.com",
            137,
            "0x" + "11" * 32,
            api_creds=ApiCreds("api", "c2VjcmV0", "pass"),
        )
        order = client.build_limit_order(
            token_id="123",
            price=0.5,
            size=10,
            side="BUY",
            maker="0x0000000000000000000000000000000000000001",
            signer="0x0000000000000000000000000000000000000001",
            neg_risk=False,
        )

        self.assertEqual(order["side"], "BUY")
        self.assertEqual(order["tokenId"], "123")
        self.assertEqual(order["makerAmount"], "5000000")
        self.assertEqual(order["takerAmount"], "10000000")
        self.assertIn("signature", order)

    def test_parse_btc_threshold(self):
        self.assertEqual(parse_btc_threshold("Will Bitcoin be above $100,000 on Friday?"), ("above", 100000.0))
        self.assertEqual(parse_btc_threshold("Will BTC be under 95k today?"), ("below", 95000.0))
        self.assertIsNone(parse_btc_threshold("Will Bitcoin hit $100,000 today?"))

    def test_btc_probability_moves_with_distance_to_strike(self):
        near = btc_terminal_probability(spot=100000, strike=99000, hours=6, annual_volatility=0.6, direction="above")
        far = btc_terminal_probability(spot=100000, strike=110000, hours=6, annual_volatility=0.6, direction="above")
        self.assertGreater(near, far)

    def test_btc_signal_requires_edge(self):
        end_date = (utc_now() + timedelta(hours=3)).isoformat()
        candidate = rank_markets(
            [
                {
                    "id": "1",
                    "question": "Will Bitcoin be above $100,000 today?",
                    "slug": "will-bitcoin-be-above-100000-today",
                    "endDate": end_date,
                    "liquidity": "10000",
                    "volume": "20000",
                    "bestBid": "0.79",
                    "bestAsk": "0.80",
                    "orderPriceMinTickSize": "0.01",
                    "acceptingOrders": True,
                    "outcomes": '["Yes","No"]',
                    "outcomePrices": '["0.80","0.20"]',
                    "clobTokenIds": '["yes-token","no-token"]',
                }
            ],
            Settings(min_liquidity_usd=0, min_volume_usd=0, soon_hours=24),
        )[0]
        signal = btc_signal(
            candidate,
            Settings(min_liquidity_usd=0, min_volume_usd=0, btc_min_model_probability=0.90),
            BtcModel(spot=105000, annual_volatility=0.4, fetched_at=utc_now()),
        )
        self.assertIsNotNone(signal)

    def test_btc_signal_requires_user_odds_band(self):
        end_date = (utc_now() + timedelta(hours=3)).isoformat()
        candidate = rank_markets(
            [
                {
                    "id": "1",
                    "question": "Will Bitcoin be above $100,000 today?",
                    "slug": "will-bitcoin-be-above-100000-today",
                    "endDate": end_date,
                    "liquidity": "10000",
                    "volume": "20000",
                    "bestBid": "0.59",
                    "bestAsk": "0.60",
                    "orderPriceMinTickSize": "0.01",
                    "acceptingOrders": True,
                    "outcomes": '["Yes","No"]',
                    "outcomePrices": '["0.60","0.40"]',
                    "clobTokenIds": '["yes-token","no-token"]',
                }
            ],
            Settings(min_liquidity_usd=0, min_volume_usd=0, soon_hours=24),
        )[0]
        signal = btc_signal(
            candidate,
            Settings(
                min_liquidity_usd=0,
                min_volume_usd=0,
                btc_min_buy_price=0.70,
                btc_max_buy_price=0.82,
            ),
            BtcModel(spot=105000, annual_volatility=0.4, fetched_at=utc_now()),
        )
        self.assertIsNone(signal)

    def test_smart_money_requires_consensus(self):
        candidate = Candidate(
            market_id="1",
            question="Will the test market resolve Yes?",
            slug="test-market",
            end_date=utc_now() + timedelta(hours=12),
            hours_to_close=12,
            liquidity=10000,
            volume=50000,
            outcome="Yes",
            price=0.75,
            token_id="yes-token",
            score=10,
            url="https://polymarket.com/event/test-market",
            best_bid=0.74,
            best_ask=0.76,
            tick_size=0.01,
            accepts_orders=True,
        )
        trades = [
            SmartTrade("0x1", "yes-token", "BUY", 0.75, 100, 75, 1, "Test market", "Yes", "test-market"),
            SmartTrade("0x2", "yes-token", "BUY", 0.76, 80, 60.8, 1, "Test market", "Yes", "test-market"),
        ]

        signals = smart_money_signals(
            [candidate],
            trades,
            Settings(
                smart_min_consensus=2,
                smart_min_trade_usd=25,
                smart_min_buy_price=0.05,
                smart_max_buy_price=0.85,
                smart_max_spread=0.05,
            ),
        )

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].consensus, 2)
        self.assertEqual(signals[0].candidate.token_id, "yes-token")
        payload = signals[0].to_dict()
        self.assertIn("selection_reason", payload)
        self.assertEqual(payload["selection_metrics"]["profitable_wallet_count"], 2)
        self.assertEqual(payload["selection_metrics"]["min_consensus"], 2)
        self.assertEqual(payload["selection_metrics"]["spread"], 0.02)

    def test_smart_money_rejects_single_wallet(self):
        candidate = Candidate(
            market_id="1",
            question="Will the test market resolve Yes?",
            slug="test-market",
            end_date=utc_now() + timedelta(hours=12),
            hours_to_close=12,
            liquidity=10000,
            volume=50000,
            outcome="Yes",
            price=0.75,
            token_id="yes-token",
            score=10,
            url="https://polymarket.com/event/test-market",
            best_bid=0.74,
            best_ask=0.76,
            tick_size=0.01,
            accepts_orders=True,
        )
        trades = [
            SmartTrade("0x1", "yes-token", "BUY", 0.75, 100, 75, 1, "Test market", "Yes", "test-market"),
        ]

        self.assertEqual(
            smart_money_signals([candidate], trades, Settings(smart_min_consensus=2, smart_min_trade_usd=25)),
            [],
        )

    def test_smart_money_clamps_consensus_to_multiple_wallets(self):
        candidate = Candidate(
            market_id="1",
            question="Will the test market resolve Yes?",
            slug="test-market",
            end_date=utc_now() + timedelta(hours=12),
            hours_to_close=12,
            liquidity=10000,
            volume=50000,
            outcome="Yes",
            price=0.75,
            token_id="yes-token",
            score=10,
            url="https://polymarket.com/event/test-market",
            best_bid=0.74,
            best_ask=0.76,
            tick_size=0.01,
            accepts_orders=True,
        )
        trades = [
            SmartTrade("0x1", "yes-token", "BUY", 0.75, 100, 75, 1, "Test market", "Yes", "test-market"),
        ]

        self.assertEqual(
            smart_money_signals([candidate], trades, Settings(smart_min_consensus=1, smart_min_trade_usd=25)),
            [],
        )

    def test_smart_money_rejects_too_close_to_expiry(self):
        candidate = Candidate(
            market_id="1",
            question="Bitcoin Up or Down - soon",
            slug="btc-updown-soon",
            end_date=utc_now() + timedelta(minutes=3),
            hours_to_close=0.05,
            liquidity=10000,
            volume=50000,
            outcome="Up",
            price=0.5,
            token_id="yes-token",
            score=10,
            url="https://polymarket.com/event/test-market",
            best_bid=0.49,
            best_ask=0.50,
            tick_size=0.01,
            accepts_orders=True,
        )
        trades = [
            SmartTrade("0x1", "yes-token", "BUY", 0.49, 100, 49, 1, "Test market", "Up", "test-market"),
            SmartTrade("0x2", "yes-token", "BUY", 0.50, 100, 50, 1, "Test market", "Up", "test-market"),
            SmartTrade("0x3", "yes-token", "BUY", 0.50, 100, 50, 1, "Test market", "Up", "test-market"),
        ]

        signals, details = smart_money_signals(
            [candidate],
            trades,
            Settings(smart_min_hours_to_close=0.25, smart_min_trade_usd=1),
            include_details=True,
        )

        self.assertEqual(signals, [])
        self.assertEqual(details["rejected"]["too_close_to_expiry"], 1)

    def test_crypto_micro_requires_higher_consensus(self):
        candidate = Candidate(
            market_id="1",
            question="Bitcoin Up or Down - May 3, 9:00PM-9:15PM ET",
            slug="btc-updown-15m",
            end_date=utc_now() + timedelta(hours=1),
            hours_to_close=1,
            liquidity=10000,
            volume=50000,
            outcome="Up",
            price=0.5,
            token_id="yes-token",
            score=10,
            url="https://polymarket.com/event/test-market",
            best_bid=0.49,
            best_ask=0.50,
            tick_size=0.01,
            accepts_orders=True,
        )
        trades = [
            SmartTrade("0x1", "yes-token", "BUY", 0.49, 100, 49, 1, "Test market", "Up", "test-market"),
            SmartTrade("0x2", "yes-token", "BUY", 0.50, 100, 50, 1, "Test market", "Up", "test-market"),
        ]

        self.assertEqual(
            smart_money_signals(
                [candidate],
                trades,
                Settings(smart_min_consensus=2, smart_crypto_micro_min_consensus=3, smart_min_trade_usd=1),
            ),
            [],
        )

    def test_max_trade_scales_with_signal_quality(self):
        settings = Settings(max_position_usd=20, smart_max_trade_usd=20)
        weak = {"consensus": 2, "selection_metrics": {"profitable_wallet_count": 2, "copied_usdc": 100}}
        strong = {"consensus": 4, "selection_metrics": {"profitable_wallet_count": 4, "copied_usdc": 2000}}
        micro = {
            "consensus": 4,
            "selection_metrics": {"profitable_wallet_count": 4, "copied_usdc": 2000, "is_crypto_micro": True},
        }

        self.assertEqual(_max_trade_for_signal(settings, weak, "smart_money"), 5.0)
        self.assertEqual(_max_trade_for_signal(settings, strong, "smart_money"), 20.0)
        self.assertEqual(_max_trade_for_signal(settings, micro, "smart_money"), 5.0)


if __name__ == "__main__":
    unittest.main()
