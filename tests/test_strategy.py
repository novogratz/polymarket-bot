from datetime import timedelta
import unittest

from polymarket_bot.bitcoin import BtcModel, btc_signal, btc_terminal_probability, parse_btc_threshold
from polymarket_bot.config import Settings
from polymarket_bot.models import Candidate, utc_now
from polymarket_bot.polymarket import ApiCreds, PolymarketClient
from polymarket_bot.portfolio import Portfolio
from polymarket_bot.smart_money import SmartTrade, market_category, smart_money_signals
from polymarket_bot.strategy import rank_markets, stake_for_candidate
from polymarket_bot.trading import _is_filled_buy_response, execute_live_sell, execute_live_trade
from polymarket_bot.main import _is_unfilled_market_order_error, _max_trade_for_signal, _sell_plan, _smart_discovery_keywords


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

    def test_rank_markets_preserves_event_slug(self):
        end_date = (utc_now() + timedelta(hours=12)).isoformat().replace("+00:00", "Z")
        candidates = rank_markets(
            [
                {
                    "id": "1",
                    "question": "Will Team A FC win?",
                    "slug": "will-team-a-fc-win",
                    "eventSlug": "team-a-fc-vs-team-b-fc",
                    "endDate": end_date,
                    "liquidity": "1000",
                    "volume": "2000",
                    "outcomes": '["Yes","No"]',
                    "outcomePrices": '["0.55","0.45"]',
                }
            ],
            Settings(min_liquidity_usd=100, min_volume_usd=100, soon_hours=24),
        )

        self.assertEqual(candidates[0].event_slug, "team-a-fc-vs-team-b-fc")

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

    def test_smart_discovery_keywords_are_deduped(self):
        settings = Settings(smart_discovery_keywords=" election,weather,election, fed ")

        self.assertEqual(_smart_discovery_keywords(settings), ["election", "weather", "fed"])

    def test_live_trade_respects_minimum_share_size(self):
        class FakeClient:
            def live_available_balance(self):
                return 20.0

            def place_market_order(self, *, candidate, amount, side="BUY", price=0.0):
                return {"price": price, "amount": amount, "side": side}, {
                    "success": True,
                    "status": "matched",
                    "orderID": "order-1",
                    "makingAmount": str(amount),
                }

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

        self.assertGreaterEqual(portfolio.positions[0]["shares"], 5.0)
        self.assertLess(portfolio.cash, 20.0)

    def test_high_conviction_trade_can_use_balance_fraction(self):
        class FakeClient:
            def live_available_balance(self):
                return 100.0

            def place_market_order(self, *, candidate, amount, side="BUY", price=0.0):
                return {"price": price, "amount": amount, "side": side}, {
                    "success": True,
                    "status": "matched",
                    "orderID": "order-1",
                    "makingAmount": str(amount),
                }

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
            best_bid=0.49,
            best_ask=0.5,
            tick_size=0.01,
            accepts_orders=True,
        )
        signal = {
            "consensus": 4,
            "copied_usdc": 2000,
            "avg_copy_price": 0.5,
            "selection_metrics": {
                "profitable_wallet_count": 4,
                "copied_usdc": 2000,
                "avg_copy_price": 0.5,
                "value_score": 0,
                "value_discount_pct": 0,
            },
        }
        portfolio = Portfolio(cash=100.0, positions=[])
        result = execute_live_trade(
            FakeClient(),
            Settings(
                trade_fraction=1.0,
                max_position_usd=10,
                smart_high_conviction_balance_fraction=0.5,
            ),
            candidate,
            portfolio,
            min_trade_usd=1.0,
            max_trade_usd=10.0,
            signal=signal,
        )

        self.assertEqual(result.order["amount"], 50.0)
        self.assertEqual(portfolio.positions[0]["stake"], 50.0)

    def test_live_trade_rejects_second_position_in_same_sports_event(self):
        class FakeClient:
            def live_available_balance(self):
                return 100.0

            def place_market_order(self, *, candidate, amount, side="BUY", price=0.0):
                raise AssertionError("order should be blocked before submission")

        open_candidate = Candidate(
            market_id="gil-yes-no",
            question="Will Gil Vicente FC win on 2026-05-03?",
            slug="will-gil-vicente-fc-win-on-2026-05-03",
            event_slug="gil-vicente-fc-vs-sc-freiburg-2026-05-03",
            end_date=utc_now() + timedelta(hours=1),
            hours_to_close=1,
            liquidity=1000,
            volume=2000,
            outcome="No",
            price=0.45,
            token_id="gil-no",
            score=1,
            url="https://polymarket.com/event/gil-vicente-fc-vs-sc-freiburg-2026-05-03",
            best_bid=0.44,
            best_ask=0.45,
            tick_size=0.01,
            accepts_orders=True,
        )
        opposite_candidate = Candidate(
            market_id="freiburg-yes-no",
            question="Will SC Freiburg win on 2026-05-03?",
            slug="will-sc-freiburg-win-on-2026-05-03",
            event_slug="gil-vicente-fc-vs-sc-freiburg-2026-05-03",
            end_date=utc_now() + timedelta(hours=1),
            hours_to_close=1,
            liquidity=1000,
            volume=2000,
            outcome="Yes",
            price=0.45,
            token_id="freiburg-yes",
            score=1,
            url="https://polymarket.com/event/gil-vicente-fc-vs-sc-freiburg-2026-05-03",
            best_bid=0.44,
            best_ask=0.45,
            tick_size=0.01,
            accepts_orders=True,
        )
        portfolio = Portfolio(cash=100.0, positions=[])
        self.assertIsNotNone(portfolio.record_live_position(open_candidate, 10.0, entry_price=0.45))

        with self.assertRaisesRegex(ValueError, "duplicate_open_sports_event"):
            execute_live_trade(
                FakeClient(),
                Settings(trade_fraction=1.0, max_position_usd=10),
                opposite_candidate,
                portfolio,
                min_trade_usd=1.0,
                max_trade_usd=10.0,
            )

    def test_live_trade_counts_existing_invested_exposure(self):
        class FakeClient:
            def live_available_balance(self):
                return 52.0

            def place_market_order(self, *, candidate, amount, side="BUY", price=0.0):
                raise AssertionError("target exposure is already reached")

        open_candidate = Candidate(
            market_id="open",
            question="Q",
            slug="q-open",
            end_date=utc_now() + timedelta(hours=1),
            hours_to_close=1,
            liquidity=1000,
            volume=2000,
            outcome="Yes",
            price=0.5,
            token_id="open-token",
            score=1,
            url="https://polymarket.com/event/q-open",
            best_bid=0.49,
            best_ask=0.5,
            tick_size=0.01,
            accepts_orders=True,
        )
        next_candidate = Candidate(
            market_id="next",
            question="Next Q",
            slug="q-next",
            end_date=utc_now() + timedelta(hours=1),
            hours_to_close=1,
            liquidity=1000,
            volume=2000,
            outcome="Yes",
            price=0.5,
            token_id="next-token",
            score=1,
            url="https://polymarket.com/event/q-next",
            best_bid=0.49,
            best_ask=0.5,
            tick_size=0.01,
            accepts_orders=True,
        )
        portfolio = Portfolio(cash=52.0, positions=[])
        self.assertIsNotNone(portfolio.record_live_position(open_candidate, 80.0, entry_price=0.5))

        with self.assertRaisesRegex(ValueError, "target exposure already reached"):
            execute_live_trade(
                FakeClient(),
                Settings(trade_fraction=0.5, max_position_usd=30),
                next_candidate,
                portfolio,
                min_trade_usd=1.0,
                max_trade_usd=30.0,
            )

    def test_high_flow_two_wallet_trade_can_use_balance_fraction(self):
        class FakeClient:
            def live_available_balance(self):
                return 100.0

            def place_market_order(self, *, candidate, amount, side="BUY", price=0.0):
                return {"price": price, "amount": amount, "side": side}, {
                    "success": True,
                    "status": "matched",
                    "orderID": "order-1",
                    "makingAmount": str(amount),
                }

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
            best_bid=0.49,
            best_ask=0.5,
            tick_size=0.01,
            accepts_orders=True,
        )
        signal = {
            "consensus": 2,
            "copied_usdc": 1500,
            "avg_copy_price": 0.5,
            "total_trader_pnl": 500000,
            "selection_metrics": {
                "profitable_wallet_count": 2,
                "copied_usdc": 1500,
                "avg_copy_price": 0.5,
                "total_trader_pnl": 500000,
                "value_score": 0,
                "value_discount_pct": 0,
            },
        }
        portfolio = Portfolio(cash=100.0, positions=[])
        result = execute_live_trade(
            FakeClient(),
            Settings(
                trade_fraction=1.0,
                max_position_usd=10,
                smart_high_conviction_balance_fraction=0.5,
            ),
            candidate,
            portfolio,
            min_trade_usd=1.0,
            max_trade_usd=10.0,
            signal=signal,
        )

        self.assertEqual(result.order["amount"], 50.0)
        self.assertEqual(portfolio.positions[0]["stake"], 50.0)

    def test_live_trade_does_not_record_resting_buy_order(self):
        class FakeClient:
            def live_available_balance(self):
                return 20.0

            def place_market_order(self, *, candidate, amount, side="BUY", price=0.0):
                return {"price": price, "amount": amount, "side": side}, {
                    "success": True,
                    "status": "live",
                    "orderID": "order-live",
                    "makingAmount": "",
                    "takingAmount": "",
                }

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
        portfolio = Portfolio(cash=20.0, positions=[])

        execute_live_trade(
            FakeClient(),
            Settings(trade_fraction=0.10, min_order_shares=5.0),
            candidate,
            portfolio,
            min_trade_usd=1.0,
            max_trade_usd=1.0,
        )

        self.assertEqual(portfolio.positions, [])
        self.assertEqual(portfolio.pending_orders or [], [])

    def test_live_trade_does_not_record_unfilled_fok(self):
        class FakeClient:
            def live_available_balance(self):
                return 20.0

            def place_market_order(self, *, candidate, amount, side="BUY", price=0.0):
                return {"price": price, "amount": amount, "side": side}, {
                    "success": True,
                    "status": "unmatched",
                    "orderID": "order-unfilled",
                    "makingAmount": "",
                    "takingAmount": "",
                }

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
        portfolio = Portfolio(cash=20.0, positions=[])

        execute_live_trade(
            FakeClient(),
            Settings(trade_fraction=0.10, min_order_shares=5.0),
            candidate,
            portfolio,
            min_trade_usd=1.0,
            max_trade_usd=1.0,
        )

        self.assertEqual(portfolio.positions, [])
        self.assertEqual(portfolio.pending_orders or [], [])

    def test_fok_unfilled_error_is_skippable(self):
        message = (
            "PolyApiException[status_code=400, error_message={'error': "
            "\"order couldn't be fully filled. FOK orders are fully filled or killed.\"}]"
        )

        self.assertTrue(_is_unfilled_market_order_error(message))

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
        self.assertEqual(portfolio.cash, 20.0)

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

    def test_sell_plan_stop_loss_triggers_after_min_age(self):
        old_open = (utc_now() - timedelta(hours=2)).isoformat()
        position = {
            "shares": 100.0,
            "initial_shares": 100.0,
            "peak_pnl_pct": -0.10,
            "sell_tiers_hit": [],
            "exits": [],
            "opened_at": old_open,
        }
        settings = Settings(smart_stop_loss_pct=0.40, smart_stop_loss_min_age_minutes=15)
        plan = _sell_plan(position, -0.45, settings)
        self.assertEqual(plan["reason"], "stop_loss")
        self.assertEqual(plan["shares"], 100.0)

    def test_sell_plan_stop_loss_skipped_for_fresh_positions(self):
        fresh_open = utc_now().isoformat()
        position = {
            "shares": 100.0,
            "initial_shares": 100.0,
            "peak_pnl_pct": -0.10,
            "sell_tiers_hit": [],
            "exits": [],
            "opened_at": fresh_open,
        }
        settings = Settings(smart_stop_loss_pct=0.40, smart_stop_loss_min_age_minutes=15)
        plan = _sell_plan(position, -0.50, settings)
        self.assertIsNone(plan)

    def test_sell_plan_stop_loss_disabled_when_zero(self):
        old_open = (utc_now() - timedelta(hours=2)).isoformat()
        position = {
            "shares": 100.0,
            "initial_shares": 100.0,
            "peak_pnl_pct": -0.10,
            "sell_tiers_hit": [],
            "exits": [],
            "opened_at": old_open,
        }
        settings = Settings(smart_stop_loss_pct=0.0)
        plan = _sell_plan(position, -0.80, settings)
        self.assertIsNone(plan)

    def test_sell_plan_trailing_stop_locks_partial_gains(self):
        old_open = (utc_now() - timedelta(hours=2)).isoformat()
        position = {
            "shares": 100.0,
            "initial_shares": 100.0,
            "peak_pnl_pct": 0.50,
            "sell_tiers_hit": [],
            "exits": [],
            "opened_at": old_open,
        }
        settings = Settings(
            smart_trailing_stop_arm_pct=0.25,
            smart_trailing_stop_giveback_pct=0.50,
            smart_peak_protect_trigger=1.0,
        )
        plan = _sell_plan(position, 0.20, settings)
        self.assertEqual(plan["reason"], "trailing_stop")
        self.assertEqual(plan["shares"], 100.0)

    def test_sell_plan_trailing_stop_skips_when_pnl_negative(self):
        old_open = (utc_now() - timedelta(hours=2)).isoformat()
        position = {
            "shares": 100.0,
            "initial_shares": 100.0,
            "peak_pnl_pct": 0.30,
            "sell_tiers_hit": [],
            "exits": [],
            "opened_at": old_open,
        }
        settings = Settings(
            smart_stop_loss_pct=0.0,
            smart_trailing_stop_arm_pct=0.25,
            smart_trailing_stop_giveback_pct=0.50,
            smart_peak_protect_trigger=1.0,
        )
        plan = _sell_plan(position, -0.05, settings)
        self.assertIsNone(plan)

    def test_sell_plan_peak_protection_beats_stop_loss(self):
        old_open = (utc_now() - timedelta(hours=2)).isoformat()
        position = {
            "shares": 100.0,
            "initial_shares": 100.0,
            "peak_pnl_pct": 1.5,
            "sell_tiers_hit": ["1.0"],
            "exits": [],
            "opened_at": old_open,
        }
        plan = _sell_plan(position, -0.50, Settings(smart_stop_loss_pct=0.40))
        self.assertEqual(plan["reason"], "peak_profit_protection")

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

    def test_smart_money_rejects_high_relative_spread(self):
        candidate = Candidate(
            market_id="1",
            question="Will the cheap market resolve Yes?",
            slug="cheap-market",
            end_date=utc_now() + timedelta(hours=12),
            hours_to_close=12,
            liquidity=10000,
            volume=50000,
            outcome="Yes",
            price=0.10,
            token_id="cheap-token",
            score=10,
            url="https://polymarket.com/event/cheap-market",
            best_bid=0.06,
            best_ask=0.10,
            tick_size=0.01,
            accepts_orders=True,
        )
        trades = [
            SmartTrade("0x1", "cheap-token", "BUY", 0.09, 1000, 90, 1, "Cheap", "Yes", "cheap-market"),
            SmartTrade("0x2", "cheap-token", "BUY", 0.09, 1000, 90, 1, "Cheap", "Yes", "cheap-market"),
        ]

        result, details = smart_money_signals(
            [candidate],
            trades,
            Settings(
                smart_min_consensus=2,
                smart_min_trade_usd=25,
                smart_min_buy_price=0.01,
                smart_max_buy_price=0.99,
                smart_max_spread=0.05,
                smart_max_relative_spread=0.30,
                smart_max_chase_premium=0.50,
            ),
            include_details=True,
        )

        self.assertEqual(result, [])
        self.assertGreaterEqual(details["rejected"].get("spread_too_wide_relative", 0), 1)

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

    def test_smart_money_allows_single_wallet_when_consensus_set_to_one(self):
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

        signals = smart_money_signals(
            [candidate],
            trades,
            Settings(
                smart_min_consensus=1,
                smart_min_trade_usd=25,
                smart_min_buy_price=0.05,
                smart_max_buy_price=0.85,
                smart_max_spread=0.05,
            ),
        )
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].consensus, 1)

    def test_smart_money_default_floor_still_requires_two_wallets(self):
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
            smart_money_signals(
                [candidate],
                trades,
                Settings(smart_min_consensus=2, smart_min_trade_usd=25),
            ),
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

    def test_smart_money_rejects_too_far_to_expiry(self):
        candidate = Candidate(
            market_id="1",
            question="Will the long market resolve Yes?",
            slug="long-market",
            end_date=utc_now() + timedelta(hours=100),
            hours_to_close=100,
            liquidity=10000,
            volume=50000,
            outcome="Yes",
            price=0.5,
            token_id="yes-token",
            score=10,
            url="https://polymarket.com/event/long-market",
            best_bid=0.49,
            best_ask=0.50,
            tick_size=0.01,
            accepts_orders=True,
        )
        trades = [
            SmartTrade("0x1", "yes-token", "BUY", 0.49, 100, 49, 1, "Long market", "Yes", "long-market"),
            SmartTrade("0x2", "yes-token", "BUY", 0.50, 100, 50, 1, "Long market", "Yes", "long-market"),
        ]

        signals, details = smart_money_signals(
            [candidate],
            trades,
            Settings(smart_max_hours_to_close=24, smart_min_trade_usd=1),
            include_details=True,
        )

        self.assertEqual(signals, [])
        self.assertEqual(details["rejected"]["too_far_to_expiry"], 1)

    def test_smart_money_prefers_shorter_market_when_quality_matches(self):
        near = Candidate(
            market_id="1",
            question="Will the near market resolve Yes?",
            slug="near-market",
            end_date=utc_now() + timedelta(hours=3),
            hours_to_close=3,
            liquidity=10000,
            volume=50000,
            outcome="Yes",
            price=0.5,
            token_id="near-token",
            score=10,
            url="https://polymarket.com/event/near-market",
            best_bid=0.49,
            best_ask=0.50,
            tick_size=0.01,
            accepts_orders=True,
        )
        far = Candidate(
            market_id="2",
            question="Will the far market resolve Yes?",
            slug="far-market",
            end_date=utc_now() + timedelta(hours=48),
            hours_to_close=48,
            liquidity=10000,
            volume=50000,
            outcome="Yes",
            price=0.5,
            token_id="far-token",
            score=10,
            url="https://polymarket.com/event/far-market",
            best_bid=0.49,
            best_ask=0.50,
            tick_size=0.01,
            accepts_orders=True,
        )
        trades = [
            SmartTrade("0x1", "near-token", "BUY", 0.49, 100, 49, 1, "Near market", "Yes", "near-market"),
            SmartTrade("0x2", "near-token", "BUY", 0.50, 100, 50, 1, "Near market", "Yes", "near-market"),
            SmartTrade("0x1", "far-token", "BUY", 0.49, 100, 49, 1, "Far market", "Yes", "far-market"),
            SmartTrade("0x2", "far-token", "BUY", 0.50, 100, 50, 1, "Far market", "Yes", "far-market"),
        ]

        signals = smart_money_signals(
            [far, near],
            trades,
            Settings(smart_max_hours_to_close=72, smart_min_trade_usd=1),
        )

        self.assertEqual(signals[0].candidate.token_id, "near-token")
        self.assertGreater(signals[0].score, signals[1].score)

    def test_smart_money_prefers_priority_category_when_quality_matches(self):
        weather = Candidate(
            market_id="1",
            question="Will it rain in New York tomorrow?",
            slug="will-it-rain-in-new-york-tomorrow",
            end_date=utc_now() + timedelta(hours=12),
            hours_to_close=12,
            liquidity=10000,
            volume=50000,
            outcome="Yes",
            price=0.5,
            token_id="weather-token",
            score=10,
            url="https://polymarket.com/event/will-it-rain-in-new-york-tomorrow",
            best_bid=0.49,
            best_ask=0.50,
            tick_size=0.01,
            accepts_orders=True,
        )
        sports = Candidate(
            market_id="2",
            question="Will Team A FC win today?",
            slug="will-team-a-fc-win-today",
            end_date=utc_now() + timedelta(hours=12),
            hours_to_close=12,
            liquidity=10000,
            volume=50000,
            outcome="Yes",
            price=0.5,
            token_id="sports-token",
            score=10,
            url="https://polymarket.com/event/will-team-a-fc-win-today",
            best_bid=0.49,
            best_ask=0.50,
            tick_size=0.01,
            accepts_orders=True,
        )
        trades = [
            SmartTrade("0x1", "weather-token", "BUY", 0.5, 200, 100, 1, "Rain", "Yes", "rain"),
            SmartTrade("0x2", "weather-token", "BUY", 0.5, 200, 100, 1, "Rain", "Yes", "rain"),
            SmartTrade("0x1", "sports-token", "BUY", 0.5, 200, 100, 1, "Team A FC", "Yes", "team-a-fc"),
            SmartTrade("0x2", "sports-token", "BUY", 0.5, 200, 100, 1, "Team A FC", "Yes", "team-a-fc"),
        ]

        signals = smart_money_signals(
            [sports, weather],
            trades,
            Settings(smart_min_trade_usd=1, smart_min_copied_usdc=50),
        )
        payload = signals[0].to_dict()

        self.assertEqual(signals[0].candidate.token_id, "weather-token")
        self.assertEqual(payload["category"], "WEATHER")
        self.assertEqual(market_category(sports.question, sports.slug), "SPORTS")

    def test_smart_money_prefers_discount_to_smart_money_average(self):
        discounted = Candidate(
            market_id="1",
            question="NBA Playoffs: Who Will Win Series?",
            slug="nba-series-value",
            end_date=utc_now() + timedelta(hours=12),
            hours_to_close=12,
            liquidity=10000,
            volume=50000,
            outcome="Celtics",
            price=0.08,
            token_id="discount-token",
            score=10,
            url="https://polymarket.com/event/nba-series-value",
            best_bid=0.07,
            best_ask=0.08,
            tick_size=0.01,
            accepts_orders=True,
        )
        chased = Candidate(
            market_id="2",
            question="Will favorite win?",
            slug="favorite-chase",
            end_date=utc_now() + timedelta(hours=12),
            hours_to_close=12,
            liquidity=10000,
            volume=50000,
            outcome="Yes",
            price=0.90,
            token_id="chase-token",
            score=10,
            url="https://polymarket.com/event/favorite-chase",
            best_bid=0.89,
            best_ask=0.90,
            tick_size=0.01,
            accepts_orders=True,
        )
        trades = [
            SmartTrade("0x1", "discount-token", "BUY", 0.24, 100, 24, 1, "NBA series", "Celtics", "nba-series-value"),
            SmartTrade("0x2", "discount-token", "BUY", 0.26, 100, 26, 1, "NBA series", "Celtics", "nba-series-value"),
            SmartTrade("0x1", "chase-token", "BUY", 0.80, 100, 80, 1, "Favorite", "Yes", "favorite-chase"),
            SmartTrade("0x2", "chase-token", "BUY", 0.82, 100, 82, 1, "Favorite", "Yes", "favorite-chase"),
        ]

        signals = smart_money_signals(
            [chased, discounted],
            trades,
            Settings(smart_max_hours_to_close=24, smart_min_trade_usd=1),
        )
        payload = signals[0].to_dict()

        self.assertEqual(signals[0].candidate.token_id, "discount-token")
        self.assertGreater(payload["selection_metrics"]["value_discount_pct"], 0)
        self.assertGreater(payload["selection_metrics"]["value_score"], 0)

    def test_smart_money_rejects_low_total_copied_flow(self):
        candidate = Candidate(
            market_id="1",
            question="Will Team A win today?",
            slug="will-team-a-win-today",
            end_date=utc_now() + timedelta(hours=14),
            hours_to_close=14,
            liquidity=10000,
            volume=50000,
            outcome="Yes",
            price=0.03,
            token_id="low-flow-token",
            score=10,
            url="https://polymarket.com/event/will-team-a-win-today",
            best_bid=0.021,
            best_ask=0.03,
            tick_size=0.001,
            accepts_orders=True,
        )
        trades = [
            SmartTrade("0x1", "low-flow-token", "BUY", 0.025, 400, 10, 1, "Team A", "Yes", "team-a"),
            SmartTrade("0x2", "low-flow-token", "BUY", 0.026, 475.38, 12.36, 1, "Team A", "Yes", "team-a"),
        ]

        signals, details = smart_money_signals(
            [candidate],
            trades,
            Settings(smart_min_copied_usdc=50, smart_min_trade_usd=1),
            include_details=True,
        )

        self.assertEqual(signals, [])
        self.assertEqual(details["rejected"]["copied_usdc_too_small"], 1)

    def test_smart_money_rejects_high_chase_premium(self):
        candidate = Candidate(
            market_id="1",
            question="Will Team A win today?",
            slug="will-team-a-win-today",
            end_date=utc_now() + timedelta(hours=14),
            hours_to_close=14,
            liquidity=10000,
            volume=50000,
            outcome="Yes",
            price=0.03,
            token_id="chase-premium-token",
            score=10,
            url="https://polymarket.com/event/will-team-a-win-today",
            best_bid=0.021,
            best_ask=0.03,
            tick_size=0.001,
            accepts_orders=True,
        )
        trades = [
            SmartTrade("0x1", "chase-premium-token", "BUY", 0.025, 2000, 50, 1, "Team A", "Yes", "team-a"),
            SmartTrade("0x2", "chase-premium-token", "BUY", 0.026, 2000, 52, 1, "Team A", "Yes", "team-a"),
        ]

        signals, details = smart_money_signals(
            [candidate],
            trades,
            Settings(smart_max_chase_premium=0.10, smart_min_copied_usdc=50, smart_min_trade_usd=1),
            include_details=True,
        )

        self.assertEqual(signals, [])
        self.assertEqual(details["rejected"]["chase_premium_too_high"], 1)

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

    def test_crypto_markets_are_blocked_by_default(self):
        candidate = Candidate(
            market_id="1",
            question="Will Bitcoin reach $81,000 on May 3?",
            slug="will-bitcoin-reach-81k-on-may-3",
            end_date=utc_now() + timedelta(hours=14),
            hours_to_close=14,
            liquidity=10000,
            volume=50000,
            outcome="Yes",
            price=0.03,
            token_id="btc-token",
            score=10,
            url="https://polymarket.com/event/will-bitcoin-reach-81k-on-may-3",
            best_bid=0.029,
            best_ask=0.03,
            tick_size=0.001,
            accepts_orders=True,
        )
        trades = [
            SmartTrade("0x1", "btc-token", "BUY", 0.03, 10000, 300, 1, "BTC", "Yes", "btc"),
            SmartTrade("0x2", "btc-token", "BUY", 0.03, 10000, 300, 1, "BTC", "Yes", "btc"),
            SmartTrade("0x3", "btc-token", "BUY", 0.03, 10000, 300, 1, "BTC", "Yes", "btc"),
        ]

        signals, details = smart_money_signals(
            [candidate],
            trades,
            Settings(smart_min_copied_usdc=50, smart_min_trade_usd=1),
            include_details=True,
        )

        self.assertEqual(signals, [])
        self.assertEqual(details["rejected"]["crypto_signal_blocked"], 1)

    def test_crypto_can_be_explicitly_allowed_for_obvious_longer_signal(self):
        candidate = Candidate(
            market_id="1",
            question="Will Bitcoin be above $80,000 tomorrow?",
            slug="will-bitcoin-be-above-80000-tomorrow",
            end_date=utc_now() + timedelta(hours=24),
            hours_to_close=24,
            liquidity=10000,
            volume=50000,
            outcome="Yes",
            price=0.75,
            token_id="btc-token",
            score=10,
            url="https://polymarket.com/event/will-bitcoin-be-above-80000-tomorrow",
            best_bid=0.74,
            best_ask=0.75,
            tick_size=0.01,
            accepts_orders=True,
        )
        trades = [
            SmartTrade("0x1", "btc-token", "BUY", 0.75, 1000, 750, 1, "BTC", "Yes", "btc"),
            SmartTrade("0x2", "btc-token", "BUY", 0.75, 1000, 750, 1, "BTC", "Yes", "btc"),
            SmartTrade("0x3", "btc-token", "BUY", 0.75, 1000, 750, 1, "BTC", "Yes", "btc"),
        ]

        signals = smart_money_signals(
            [candidate],
            trades,
            Settings(
                smart_allow_crypto=True,
                smart_crypto_min_hours_to_close=6,
                smart_crypto_max_hours_to_close=48,
                smart_crypto_min_consensus=3,
                smart_crypto_min_copied_usdc=1000,
                smart_min_copied_usdc=50,
                smart_min_trade_usd=1,
            ),
        )

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].candidate.token_id, "btc-token")

    def test_crypto_allowed_requires_high_buy_price(self):
        candidate = Candidate(
            market_id="1",
            question="Bitcoin Up or Down - May 3, 9:00PM-9:15PM ET",
            slug="btc-updown-15m",
            end_date=utc_now() + timedelta(minutes=15),
            hours_to_close=0.25,
            liquidity=10000,
            volume=50000,
            outcome="Down",
            price=0.55,
            token_id="btc-token",
            score=10,
            url="https://polymarket.com/event/btc-updown-15m",
            best_bid=0.54,
            best_ask=0.55,
            tick_size=0.01,
            accepts_orders=True,
        )
        trades = [
            SmartTrade("0x1", "btc-token", "BUY", 0.55, 1000, 550, 1, "BTC", "Down", "btc"),
            SmartTrade("0x2", "btc-token", "BUY", 0.55, 1000, 550, 1, "BTC", "Down", "btc"),
            SmartTrade("0x3", "btc-token", "BUY", 0.55, 1000, 550, 1, "BTC", "Down", "btc"),
        ]

        signals, details = smart_money_signals(
            [candidate],
            trades,
            Settings(
                smart_allow_crypto=True,
                smart_crypto_min_hours_to_close=0,
                smart_crypto_max_hours_to_close=48,
                smart_crypto_min_consensus=3,
                smart_crypto_min_copied_usdc=1000,
                smart_crypto_min_buy_price=0.70,
                smart_min_copied_usdc=50,
                smart_min_trade_usd=1,
            ),
            include_details=True,
        )

        self.assertEqual(signals, [])
        self.assertEqual(details["rejected"]["crypto_signal_blocked"], 1)

    def test_max_trade_scales_with_signal_quality(self):
        settings = Settings(max_position_usd=20, smart_max_trade_usd=20)
        weak = {"consensus": 2, "selection_metrics": {"profitable_wallet_count": 2, "copied_usdc": 100}}
        high_flow_two_wallets = {
            "consensus": 2,
            "selection_metrics": {"profitable_wallet_count": 2, "copied_usdc": 2000},
        }
        strong = {"consensus": 4, "selection_metrics": {"profitable_wallet_count": 4, "copied_usdc": 2000}}
        micro = {
            "consensus": 4,
            "selection_metrics": {"profitable_wallet_count": 4, "copied_usdc": 2000, "is_crypto_micro": True},
        }

        self.assertEqual(_max_trade_for_signal(settings, weak, "smart_money"), 5.0)
        self.assertEqual(_max_trade_for_signal(settings, high_flow_two_wallets, "smart_money"), 20.0)
        self.assertEqual(_max_trade_for_signal(settings, strong, "smart_money"), 20.0)
        self.assertEqual(_max_trade_for_signal(settings, micro, "smart_money"), 5.0)

    def test_max_trade_percentage_sizing_scales_with_cash(self):
        settings = Settings(
            smart_position_pct=0.10,
            smart_max_position_ceiling_usd=50.0,
            smart_crypto_micro_max_trade_usd=5.0,
        )
        strong = {
            "consensus": 4,
            "selection_metrics": {"profitable_wallet_count": 4, "copied_usdc": 2000},
        }
        weak = {
            "consensus": 2,
            "selection_metrics": {"profitable_wallet_count": 2, "copied_usdc": 100},
        }
        micro = {
            "consensus": 4,
            "selection_metrics": {"profitable_wallet_count": 4, "copied_usdc": 2000, "is_crypto_micro": True},
        }

        self.assertEqual(
            _max_trade_for_signal(settings, strong, "smart_money", available_cash=90.0),
            9.0,
        )
        self.assertEqual(
            _max_trade_for_signal(settings, strong, "smart_money", available_cash=900.0),
            50.0,
        )
        self.assertEqual(
            _max_trade_for_signal(settings, weak, "smart_money", available_cash=90.0),
            5.85,
        )
        self.assertEqual(
            _max_trade_for_signal(settings, micro, "smart_money", available_cash=900.0),
            5.0,
        )

    def test_max_trade_percentage_sizing_falls_back_when_cash_missing(self):
        settings = Settings(
            smart_position_pct=0.10,
            max_position_usd=20,
            smart_max_trade_usd=20,
        )
        strong = {
            "consensus": 4,
            "selection_metrics": {"profitable_wallet_count": 4, "copied_usdc": 2000},
        }
        self.assertEqual(_max_trade_for_signal(settings, strong, "smart_money"), 20.0)


if __name__ == "__main__":
    unittest.main()
