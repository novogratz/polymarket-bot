import os

# Skip .env load and strip any leaked POLYMARKET_* env vars BEFORE importing
# polymarket_bot.config — Settings field defaults are evaluated at class
# definition time, so any runtime override present at import would freeze into
# the test fixtures.
os.environ["POLYMARKET_SKIP_DOTENV"] = "1"
for _k in [k for k in os.environ if k.startswith("POLYMARKET_") and k != "POLYMARKET_SKIP_DOTENV"]:
    del os.environ[_k]

from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from polymarket_bot.auto_tuner import compute_overrides
from polymarket_bot.bitcoin import BtcModel, btc_signal, btc_terminal_probability, parse_btc_threshold
from polymarket_bot.config import Settings
from polymarket_bot.models import Candidate, is_excluded_market, utc_now
from polymarket_bot.polymarket import ApiCreds, PolymarketClient
from polymarket_bot.portfolio import Portfolio
from polymarket_bot.smart_money import SmartTrade, market_category, smart_money_signals
from polymarket_bot.strategy import rank_markets, stake_for_candidate
from polymarket_bot.trading import (
    _is_filled_buy_response,
    build_client,
    execute_live_sell,
    execute_live_trade,
)
from polymarket_bot.main import (
    _is_unfilled_market_order_error,
    _max_trade_for_signal,
    _sell_plan,
    _smart_discovery_keywords,
    _token_in_loss_cooldown,
    load_btc_candidates,
)
from polymarket_bot.profiles import load_profile


class StrategyTests(unittest.TestCase):
    def test_baseline_tight_profile_is_capital_guarded(self):
        profile = load_profile(
            Path(__file__).resolve().parent.parent
            / "configs"
            / "profiles"
            / "baseline_tight.toml"
        )

        self.assertEqual(profile.starting_cash, 20.0)
        self.assertEqual(profile.values["POLYMARKET_MIN_OPEN_POSITIONS"], "0")
        self.assertEqual(profile.values["POLYMARKET_SMART_MIN_CONSENSUS"], "3")
        self.assertEqual(profile.values["POLYMARKET_SMART_FALLBACK_CONSENSUS"], "3")
        self.assertEqual(profile.values["POLYMARKET_SMART_MIN_COPIED_USDC"], "250.0")
        self.assertEqual(profile.values["POLYMARKET_SMART_MAX_CHASE_PREMIUM"], "0.0")
        self.assertEqual(profile.values["POLYMARKET_SMART_MAX_ORDERS_PER_TICK"], "1")
        self.assertEqual(profile.values["POLYMARKET_SMART_DEEP_FALLBACK_ENABLED"], "0")

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

    def test_live_buy_stake_capped_to_book_depth(self):
        # Regression (2026-06-10): a $380 FOK buy on PPI bounced with
        # "FOK orders are fully filled or killed" because the ask side
        # couldn't fill the whole stake within the price guard. The stake
        # must be capped to the executable depth so the order fills.
        placed = {}

        class FakeClient:
            def live_available_balance(self):
                return 900.0

            def get_order_book(self, token_id):
                # $150.40 of executable depth within the 0.954 guard
                return {
                    "asks": [
                        {"price": "0.953", "size": "100"},  # $95.30
                        {"price": "0.954", "size": "57.75"},  # $55.10
                        {"price": "0.97", "size": "5000"},  # above guard — ignored
                    ],
                    "bids": [{"price": "0.92", "size": "50"}],
                }

            def place_market_order(self, *, candidate, amount, side="BUY", price=0.0):
                placed["amount"] = amount
                return {"price": price, "amount": amount, "side": side}, {
                    "success": True,
                    "status": "matched",
                    "orderID": "order-1",
                    "makingAmount": str(amount),
                }

        candidate = Candidate(
            market_id="ppi",
            question="Will PPI YoY be between 7.0% and 7.9% in May?",
            slug="ppi-yoy",
            end_date=utc_now() + timedelta(hours=6),
            hours_to_close=6,
            liquidity=1300,
            volume=2000,
            outcome="No",
            price=0.953,
            token_id="tok-ppi",
            score=1,
            url="https://polymarket.com/event/ppi",
            best_bid=0.921,
            best_ask=0.953,
            tick_size=0.001,
            accepts_orders=True,
        )
        portfolio = Portfolio(cash=900.0, positions=[])
        result = execute_live_trade(
            FakeClient(),
            Settings(trade_fraction=0.95, min_order_shares=5.0),
            candidate,
            portfolio,
            min_trade_usd=1.0,
            max_trade_usd=500.0,
        )

        depth = 0.953 * 100 + 0.954 * 57.75  # $150.40 within the guard
        self.assertLessEqual(placed["amount"], round(depth * 0.90, 2))
        self.assertGreater(placed["amount"], 0)
        self.assertTrue(result.response.get("success"))
        self.assertAlmostEqual(portfolio.positions[0]["stake"], placed["amount"], places=2)

    def test_partial_depth_buy_leaves_cash_for_next_market(self):
        # End-to-end of the 2026-06-10 scenario: the book only offers ~$30
        # on the first market, so the bot must take the $30 AND keep the
        # remaining cash deployable into the next opportunity (different
        # event) instead of bouncing the whole stake or locking up.
        placed = []

        class FakeClient:
            def __init__(self):
                self.balance = 900.0

            def live_available_balance(self):
                return self.balance

            def get_order_book(self, token_id):
                if token_id == "tok-thin":
                    return {"asks": [{"price": "0.95", "size": "35"}], "bids": []}  # $33.25 depth
                return {"asks": [{"price": "0.95", "size": "5000"}], "bids": []}  # deep

            def place_market_order(self, *, candidate, amount, side="BUY", price=0.0):
                self.balance -= amount
                placed.append((candidate.token_id, amount))
                return {"price": price, "amount": amount, "side": side}, {
                    "success": True,
                    "status": "matched",
                    "orderID": f"order-{len(placed)}",
                    "makingAmount": str(amount),
                }

        def _cand(market_id, token_id, event_slug):
            return Candidate(
                market_id=market_id,
                question=f"Will {market_id} resolve Yes?",
                slug=market_id,
                end_date=utc_now() + timedelta(hours=5),
                hours_to_close=5,
                liquidity=1300,
                volume=2000,
                outcome="No",
                price=0.949,
                token_id=token_id,
                score=1,
                url=f"https://polymarket.com/event/{event_slug}",
                best_bid=0.92,
                best_ask=0.949,
                tick_size=0.001,
                accepts_orders=True,
                event_slug=event_slug,
            )

        client = FakeClient()
        portfolio = Portfolio(cash=900.0, positions=[])
        settings = Settings(trade_fraction=0.95, min_order_shares=5.0)

        thin = _cand("ppi", "tok-thin", "ppi-yoy-may-2026")
        other = _cand("cpi", "tok-deep", "cpi-yoy-may-2026")

        first = execute_live_trade(
            client, settings, thin, portfolio, min_trade_usd=1.0, max_trade_usd=380.0
        )
        second = execute_live_trade(
            client, settings, other, portfolio, min_trade_usd=1.0, max_trade_usd=380.0
        )

        self.assertTrue(first.response.get("success"))
        self.assertTrue(second.response.get("success"))
        # First buy took only what the book offered (≤ 90% of $33.25)…
        self.assertLessEqual(placed[0][1], round(0.95 * 35 * 0.90, 2))
        # …and the next market still got a full-size stake from the rest.
        self.assertEqual(placed[1][0], "tok-deep")
        self.assertGreater(placed[1][1], 300.0)
        self.assertEqual(len(portfolio.positions), 2)

    def test_position_records_true_fill_not_price_guard(self):
        # Regression (2026-06-10 PPI): the ledger booked the price guard
        # (0.954, $229.04) while the real fill was making=$228.51 for
        # taking=240.648 shares (avg 0.9496). Entry-relative math (SL
        # trigger, never-sell-below-entry floor) and share counts must use
        # the true fill from the order response.
        class FakeClient:
            def live_available_balance(self):
                return 900.0

            def place_market_order(self, *, candidate, amount, side="BUY", price=0.0):
                return {"price": price, "amount": amount, "side": side}, {
                    "success": True,
                    "status": "matched",
                    "orderID": "order-1",
                    "makingAmount": "228.509999",
                    "takingAmount": "240.64842",
                }

        candidate = Candidate(
            market_id="ppi",
            question="Will PPI YoY be between 7.0% and 7.9% in May?",
            slug="ppi-yoy",
            end_date=utc_now() + timedelta(hours=5),
            hours_to_close=5,
            liquidity=1300,
            volume=2000,
            outcome="No",
            price=0.953,
            token_id="tok-ppi",
            score=1,
            url="https://polymarket.com/event/ppi",
            best_bid=0.925,
            best_ask=0.953,
            tick_size=0.001,
            accepts_orders=True,
        )
        portfolio = Portfolio(cash=900.0, positions=[])
        execute_live_trade(
            FakeClient(),
            Settings(trade_fraction=0.95, min_order_shares=5.0),
            candidate,
            portfolio,
            min_trade_usd=1.0,
            max_trade_usd=380.0,
        )

        pos = portfolio.positions[0]
        self.assertAlmostEqual(pos["stake"], 228.51, places=2)
        self.assertAlmostEqual(pos["entry_price"], 0.9496, places=4)
        self.assertAlmostEqual(pos["shares"], 228.51 / 0.9496, places=2)
        self.assertAlmostEqual(portfolio.cash, 900.0 - 228.51, places=2)

    def test_live_buy_rejected_when_book_cannot_cover_minimum(self):
        class FakeClient:
            def live_available_balance(self):
                return 900.0

            def get_order_book(self, token_id):
                return {"asks": [{"price": "0.953", "size": "3"}], "bids": []}  # $2.86 depth

            def place_market_order(self, **kwargs):
                raise AssertionError("order must not be sent on a too-thin book")

        candidate = Candidate(
            market_id="ppi",
            question="Will PPI YoY be between 7.0% and 7.9% in May?",
            slug="ppi-yoy",
            end_date=utc_now() + timedelta(hours=6),
            hours_to_close=6,
            liquidity=1300,
            volume=2000,
            outcome="No",
            price=0.953,
            token_id="tok-ppi",
            score=1,
            url="https://polymarket.com/event/ppi",
            best_bid=0.921,
            best_ask=0.953,
            tick_size=0.001,
            accepts_orders=True,
        )
        with self.assertRaisesRegex(ValueError, "book_too_thin"):
            execute_live_trade(
                FakeClient(),
                Settings(trade_fraction=0.95, min_order_shares=5.0),
                candidate,
                Portfolio(cash=900.0, positions=[]),
                min_trade_usd=1.0,
                max_trade_usd=500.0,
            )

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

    def test_live_resting_buy_records_pending_not_position(self):
        # A "live"/resting BUY (success, no fill yet) records NO position but
        # DOES record a pending order — so the dedup blocks re-buying it every
        # tick (2026-06-15 drain fix). Killed FOKs are handled separately.
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
        self.assertTrue(portfolio.has_pending_token("token"))

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

    def test_live_sell_clamps_price_to_polymarket_max(self):
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
            price=0.999,
            token_id="token",
            score=1,
            url="https://polymarket.com/event/q",
            best_bid=0.999,
            best_ask=1.0,
            tick_size=0.001,
            accepts_orders=True,
        )
        position = {
            "status": "open",
            "live": True,
            "market_id": "1",
            "outcome": "Yes",
            "token_id": "token",
            "entry_price": 0.5,
            "stake": 10.0,
            "shares": 20.0,
            "initial_shares": 20.0,
        }
        portfolio = Portfolio(cash=0.0, positions=[position])

        result = execute_live_sell(
            FakeClient(),
            Settings(min_order_shares=5.0, smart_min_sell_usd=1.0),
            candidate,
            portfolio,
            position,
            shares=10.0,
            reason="take_profit_25pct",
        )

        self.assertEqual(result.order["price"], 0.99)
        self.assertEqual(position["exits"][0]["exit_price"], 0.99)

    def test_live_sell_winner_floor_refuses_sub_097_resolved_exit(self):
        # User 2026-06-14: winner floor back to 0.97 (was 0.99). A winner-exit
        # reason carrying a sub-0.97 price must be refused so the position
        # holds for a real 0.97 bid or on-chain settlement. A 0.97 bid sells.
        class TripwireClient:
            def place_live_order(self, **_kwargs):
                raise AssertionError("no order may be placed below the winner floor")

        candidate = Candidate(
            market_id="1",
            question="Q",
            slug="q",
            end_date=utc_now() + timedelta(hours=1),
            hours_to_close=1,
            liquidity=1000,
            volume=2000,
            outcome="Yes",
            price=0.95,
            token_id="token",
            score=1,
            url="https://polymarket.com/event/q",
            best_bid=0.95,
            best_ask=0.97,
            tick_size=0.01,
            accepts_orders=True,
        )
        position = {
            "status": "open",
            "live": True,
            "market_id": "1",
            "outcome": "Yes",
            "token_id": "token",
            "entry_price": 0.90,
            "stake": 9.0,
            "shares": 10.0,
            "initial_shares": 10.0,
        }
        portfolio = Portfolio(cash=0.0, positions=[position])

        for reason in ("race_big_win_resolved", "resolved_market_sweep_win"):
            with self.assertRaisesRegex(ValueError, "winner_floor"):
                execute_live_sell(
                    TripwireClient(),
                    Settings(min_order_shares=5.0, smart_min_sell_usd=1.0),
                    candidate,
                    portfolio,
                    position,
                    shares=10.0,
                    reason=reason,
                )
        self.assertEqual(position["status"], "open")

    def test_auto_improve_tuner_pins_resolved_exit_threshold_at_097(self):
        # User 2026-06-14: winner exit pinned at 0.97 — the tuner must never
        # move it off 0.97.
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "auto_improve",
            Path(__file__).resolve().parent.parent / "scripts" / "auto_improve.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.TUNABLE["race.resolved_exit_threshold"], (0.97, 0.97))

    def test_live_sell_allows_tiny_share_rounding_below_minimum(self):
        class FakeClient:
            def live_share_balance(self, token_id):
                return 4.9992

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
            "entry_price": 0.4,
            "stake": 2.0,
            "shares": 4.9992,
            "initial_shares": 4.9992,
        }
        portfolio = Portfolio(cash=0.0, positions=[position])

        result = execute_live_sell(
            FakeClient(),
            Settings(min_order_shares=5.0, smart_min_sell_usd=1.0),
            candidate,
            portfolio,
            position,
            shares=4.9992,
            reason="near_min_exit",
        )

        self.assertEqual(result.order["size"], 4.9992)
        self.assertEqual(position["status"], "closed")

    def test_sell_plan_uses_profit_tiers_and_peak_protection(self):
        position = {
            "shares": 100.0,
            "initial_shares": 100.0,
            "peak_pnl_pct": 1.2,
            "sell_tiers_hit": ["0.5"],
            "exits": [],
        }
        plan = _sell_plan(position, 1.05, Settings())
        self.assertEqual(plan["reason"], "take_profit_100pct")
        self.assertEqual(plan["shares"], 50.0)

        protected = _sell_plan(
            {**position, "sell_tiers_hit": ["0.5", "1.0"]}, 0.35, Settings()
        )
        self.assertEqual(protected["reason"], "peak_profit_protection")
        self.assertEqual(protected["shares"], 100.0)

    def test_sell_plan_max_hold_time_force_exits_stale_positions(self):
        old_open = (utc_now() - timedelta(hours=30)).isoformat()
        position = {
            "shares": 100.0,
            "initial_shares": 100.0,
            "peak_pnl_pct": 0.10,
            "sell_tiers_hit": [],
            "exits": [],
            "opened_at": old_open,
        }
        settings = Settings(
            smart_max_hold_hours=24,
            smart_take_profit_tiers="1.0:0.50",
            smart_stop_loss_pct=0.0,
            smart_trailing_stop_arm_pct=0.0,
        )
        plan = _sell_plan(position, 0.05, settings)
        self.assertEqual(plan["reason"], "max_hold_time_reached")
        self.assertEqual(plan["shares"], 100.0)

    def test_sell_plan_max_hold_disabled_by_default(self):
        old_open = (utc_now() - timedelta(hours=30)).isoformat()
        position = {
            "shares": 100.0,
            "initial_shares": 100.0,
            "peak_pnl_pct": 0.10,
            "sell_tiers_hit": [],
            "exits": [],
            "opened_at": old_open,
        }
        settings = Settings(
            smart_max_hold_hours=0,
            smart_take_profit_tiers="1.0:0.50",
            smart_stop_loss_pct=0.0,
            smart_trailing_stop_arm_pct=0.0,
        )
        plan = _sell_plan(position, 0.05, settings)
        self.assertIsNone(plan)

    def test_sell_plan_fires_50pct_tier_first_when_unhit(self):
        position = {
            "shares": 100.0,
            "initial_shares": 100.0,
            "peak_pnl_pct": 0.6,
            "sell_tiers_hit": [],
            "exits": [],
        }
        plan = _sell_plan(position, 0.55, Settings())
        self.assertEqual(plan["reason"], "take_profit_50pct")
        self.assertEqual(plan["shares"], 25.0)

    def test_sell_plan_no_stop_loss_even_when_below_threshold(self):
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
        self.assertIsNone(plan)

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

    @unittest.skip("crypto banned 2026-06-03")
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

    @unittest.skip("crypto banned 2026-06-03")
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

    @unittest.skip("crypto banned 2026-06-03")
    def test_load_btc_candidates_queries_btc_keywords(self):
        end_date = (utc_now() + timedelta(hours=3)).isoformat()

        class FakeGammaClient:
            calls = []

            def __init__(self, base_url):
                self.base_url = base_url

            def get_markets(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs.get("question_contains") not in {"bitcoin", "btc"}:
                    return []
                return [
                    {
                        "id": kwargs["question_contains"],
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
                ]

        import polymarket_bot.main as main_module

        original_client = main_module.GammaClient
        try:
            main_module.GammaClient = FakeGammaClient
            candidates = load_btc_candidates(Settings(min_liquidity_usd=0, min_volume_usd=0, soon_hours=24))
        finally:
            main_module.GammaClient = original_client

        keywords = {call.get("question_contains") for call in FakeGammaClient.calls if call.get("question_contains")}
        self.assertEqual(keywords, {"bitcoin", "btc"})
        self.assertTrue(any(candidate.token_id == "yes-token" for candidate in candidates))

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

    def test_smart_money_rejects_low_per_wallet_flow(self):
        candidate = Candidate(
            market_id="1",
            question="Will the quality market resolve Yes?",
            slug="quality-market",
            end_date=utc_now() + timedelta(hours=12),
            hours_to_close=12,
            liquidity=10000,
            volume=50000,
            outcome="Yes",
            price=0.50,
            token_id="quality-token",
            score=10,
            url="https://polymarket.com/event/quality-market",
            best_bid=0.49,
            best_ask=0.50,
            tick_size=0.01,
            accepts_orders=True,
        )
        trades = [
            SmartTrade("0x1", "quality-token", "BUY", 0.50, 200, 100, 1, "Quality", "Yes", "quality"),
            SmartTrade("0x2", "quality-token", "BUY", 0.50, 20, 10, 1, "Quality", "Yes", "quality"),
        ]

        signals, details = smart_money_signals(
            [candidate],
            trades,
            Settings(
                smart_min_consensus=2,
                smart_min_wallet_flow_usdc=25,
                smart_min_copied_usdc=50,
                smart_min_trade_usd=1,
            ),
            include_details=True,
        )

        self.assertEqual(signals, [])
        self.assertEqual(details["rejected"]["not_enough_wallet_flow"], 1)

    def test_smart_money_rejects_dominated_wallet_flow(self):
        candidate = Candidate(
            market_id="1",
            question="Will the dominated market resolve Yes?",
            slug="dominated-market",
            end_date=utc_now() + timedelta(hours=12),
            hours_to_close=12,
            liquidity=10000,
            volume=50000,
            outcome="Yes",
            price=0.50,
            token_id="dominated-token",
            score=10,
            url="https://polymarket.com/event/dominated-market",
            best_bid=0.49,
            best_ask=0.50,
            tick_size=0.01,
            accepts_orders=True,
        )
        trades = [
            SmartTrade("0x1", "dominated-token", "BUY", 0.50, 900, 450, 1, "Dominated", "Yes", "dominated"),
            SmartTrade("0x2", "dominated-token", "BUY", 0.50, 100, 50, 1, "Dominated", "Yes", "dominated"),
        ]

        signals, details = smart_money_signals(
            [candidate],
            trades,
            Settings(
                smart_min_consensus=2,
                smart_min_copied_usdc=50,
                smart_max_wallet_flow_share=0.80,
                smart_min_trade_usd=1,
            ),
            include_details=True,
        )

        self.assertEqual(signals, [])
        self.assertEqual(details["rejected"]["wallet_flow_too_concentrated"], 1)

    def test_smart_money_requires_fresh_wallet_consensus_when_configured(self):
        candidate = Candidate(
            market_id="1",
            question="Will the fresh market resolve Yes?",
            slug="fresh-market",
            end_date=utc_now() + timedelta(hours=12),
            hours_to_close=12,
            liquidity=10000,
            volume=50000,
            outcome="Yes",
            price=0.50,
            token_id="fresh-token",
            score=10,
            url="https://polymarket.com/event/fresh-market",
            best_bid=0.49,
            best_ask=0.50,
            tick_size=0.01,
            accepts_orders=True,
        )
        now_ts = int(datetime.now(timezone.utc).timestamp())
        trades = [
            SmartTrade("0x1", "fresh-token", "BUY", 0.50, 200, 100, now_ts - 120, "Fresh", "Yes", "fresh"),
            SmartTrade("0x2", "fresh-token", "BUY", 0.50, 200, 100, now_ts - 7200, "Fresh", "Yes", "fresh"),
        ]

        signals, details = smart_money_signals(
            [candidate],
            trades,
            Settings(
                smart_min_consensus=2,
                smart_min_fresh_wallets=2,
                smart_fresh_wallet_minutes=60,
                smart_min_copied_usdc=50,
                smart_min_trade_usd=1,
            ),
            include_details=True,
        )

        self.assertEqual(signals, [])
        self.assertEqual(details["rejected"]["not_enough_fresh_wallets"], 1)

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
            18.0,
        )
        self.assertEqual(
            _max_trade_for_signal(settings, strong, "smart_money", available_cash=900.0),
            50.0,
        )
        self.assertEqual(
            _max_trade_for_signal(settings, weak, "smart_money", available_cash=90.0),
            6.3,
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

    def test_dry_run_swaps_ledger_and_journal_paths(self):
        from pathlib import Path

        live = Settings()
        self.assertEqual(live.state_path, Path("data/paper_state.json"))
        self.assertEqual(live.trade_journal_path, Path("data/trade_journal.jsonl"))
        self.assertFalse(live.dry_run)

        sim = Settings(dry_run=True)
        self.assertEqual(sim.state_path, Path("data/dry_run_state.json"))
        self.assertEqual(sim.trade_journal_path, Path("data/dry_run_journal.jsonl"))
        self.assertTrue(sim.dry_run)

    def test_dry_run_user_paths_are_respected(self):
        from pathlib import Path

        custom = Settings(
            dry_run=True,
            state_path=Path("custom/state.json"),
            trade_journal_path=Path("custom/journal.jsonl"),
        )
        self.assertEqual(custom.state_path, Path("custom/state.json"))
        self.assertEqual(custom.trade_journal_path, Path("custom/journal.jsonl"))

    def test_dry_run_execute_live_trade_with_real_dry_run_client(self):
        """Regression: ``_DryRunClient.live_available_balance()`` returns 0.0,
        but ``execute_live_trade`` must still execute using ``portfolio.cash``
        as the simulated balance. Otherwise every dry-run buy is rejected with
        ``no live balance available``."""
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
        portfolio = Portfolio(cash=100.0, positions=[])
        settings = Settings(dry_run=True, smart_position_pct=0.10, min_order_shares=5.0)
        client = build_client(settings)
        result = execute_live_trade(
            client,
            settings,
            candidate,
            portfolio,
            min_trade_usd=1.0,
            max_trade_usd=10.0,
            strategy="smart_money",
            signal={"consensus": 2, "copied_usdc": 250.0, "selection_metrics": {}},
        )

        self.assertTrue(result.response.get("dry_run"))
        self.assertEqual(result.response.get("status"), "matched")
        self.assertEqual(len(portfolio.positions), 1)

    def test_dry_run_execute_live_trade_skips_sdk_call(self):
        class TripwireClient:
            def live_available_balance(self):
                return 50.0

            def place_market_order(self, **_kwargs):
                raise AssertionError("place_market_order must not be called in dry-run mode")

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
        portfolio = Portfolio(cash=50.0, positions=[])
        result = execute_live_trade(
            TripwireClient(),
            Settings(dry_run=True, trade_fraction=0.10, min_order_shares=5.0),
            candidate,
            portfolio,
            min_trade_usd=1.0,
            max_trade_usd=1.0,
        )

        self.assertTrue(result.response.get("dry_run"))
        self.assertEqual(result.response.get("status"), "matched")
        self.assertEqual(len(portfolio.positions), 1)
        self.assertEqual(portfolio.positions[0]["shares"], 5.0)

    def test_unfilled_delayed_buy_records_pending_blocks_rebuy(self):
        # Regression (2026-06-15): an in-play BUY returning status="delayed"
        # (success=true, empty making/taking) is NOT filled, but it DOES
        # settle on-chain. It must be recorded PENDING so the dedup guard
        # blocks re-buying the same market every tick — the bug that stacked
        # ~$48 of duplicate "submission No" FOKs and drained $89 → $40.
        class DelayedClient:
            def live_available_balance(self):
                return 100.0

            def place_market_order(self, *, candidate, amount, price, side="BUY"):
                return (
                    {"side": side, "amount": amount, "price": price},
                    {"success": True, "orderID": "0xdelayed",
                     "status": "delayed", "makingAmount": "", "takingAmount": ""},
                )

        candidate = Candidate(
            market_id="ufc", question="Will the fight be won by submission?",
            slug="ufc-submission", end_date=utc_now() + timedelta(hours=2),
            hours_to_close=2, liquidity=3700, volume=5000, outcome="No",
            price=0.85, token_id="tok-ufc", score=1,
            url="https://polymarket.com/event/ufc",
            best_bid=0.82, best_ask=0.85, tick_size=0.01, accepts_orders=True,
        )
        portfolio = Portfolio(cash=100.0, positions=[])
        result = execute_live_trade(
            DelayedClient(),
            Settings(dry_run=False, min_order_shares=5.0, race_stake_pct=0.15, quiet=True),
            candidate, portfolio, min_trade_usd=1.0, max_trade_usd=10.0,
            strategy="grinder",
        )
        # No open position recorded (not filled)…
        self.assertEqual([p for p in portfolio.positions if p.get("status") == "open"], [])
        # …but a pending order IS, so the next tick can't re-buy.
        self.assertTrue(portfolio.has_pending_token("tok-ufc"))
        self.assertEqual(result.response.get("status"), "delayed")

    def test_race_resolved_exit_ignores_min_age(self):
        from polymarket_bot.race_strategies import _execute_race_exits

        candidate = Candidate(
            market_id="1",
            question="Q",
            slug="q",
            end_date=utc_now() + timedelta(hours=1),
            hours_to_close=1,
            liquidity=1000,
            volume=2000,
            outcome="Yes",
            price=0.99,
            token_id="token",
            score=1,
            url="https://polymarket.com/event/q",
            best_bid=0.99,
            best_ask=1.0,
            tick_size=0.01,
            accepts_orders=True,
        )
        portfolio = Portfolio(cash=1.0, positions=[])
        position = portfolio.record_live_position(candidate, 4.7, entry_price=0.94)
        self.assertIsNotNone(position)
        position["strategy"] = "grinder"

        exits = _execute_race_exits(
            build_client(Settings(dry_run=True)),
            Settings(
                dry_run=True,
                min_order_shares=5.0,
                race_resolved_exit_threshold=0.99,
                race_sl_min_age_minutes=15,
                quiet=True,
            ),
            portfolio,
            [candidate],
            "grinder",
        )

        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["reason"], "race_big_win_resolved")
        self.assertEqual(position["status"], "closed")
        self.assertAlmostEqual(float(position["realized_pnl"]), 0.25)

    def test_dynamic_tp_high_entry_needs_profit_margin(self):
        # User 2026-06-15: a high-entry favorite must clear entry by the
        # profit margin, not exit at break-even. Entry 0.97 → required 0.99.
        from polymarket_bot.race_strategies import _execute_race_exits

        def run(bid):
            candidate = Candidate(
                market_id="1", question="Will Cabo Verde win on 2026-06-15?",
                slug="cabo-verde-win", end_date=utc_now() + timedelta(hours=1),
                hours_to_close=1, liquidity=1000, volume=2000, outcome="Yes",
                price=bid, token_id="token", score=1,
                url="https://polymarket.com/event/q",
                best_bid=bid, best_ask=min(bid + 0.01, 1.0), tick_size=0.01,
                accepts_orders=True,
            )
            portfolio = Portfolio(cash=1.0, positions=[])
            position = portfolio.record_live_position(candidate, 9.7, entry_price=0.97)
            position["strategy"] = "grinder"
            position["current_price"] = bid
            return _execute_race_exits(
                build_client(Settings(dry_run=True)),
                Settings(dry_run=True, min_order_shares=5.0,
                         race_resolved_exit_threshold=0.97,
                         race_min_profit_margin=0.02,
                         race_sl_min_age_minutes=15, quiet=True),
                portfolio, [candidate], "grinder",
            ), position

        # Bid at 0.97 == entry → break-even → must NOT sell.
        exits, pos = run(0.97)
        self.assertEqual(exits, [])
        self.assertEqual(pos["status"], "open")
        # Bid at 0.99 (entry + 2¢) → sells for a real profit.
        exits, pos = run(0.99)
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["reason"], "race_big_win_resolved")
        self.assertEqual(pos["status"], "closed")

    def test_dynamic_tp_low_entry_keeps_097_exit(self):
        # A normal-entry favorite (0.87) still exits at the configured 0.97
        # (0.87 + 2¢ = 0.89 < 0.97, so the global threshold governs).
        from polymarket_bot.race_strategies import _execute_race_exits

        candidate = Candidate(
            market_id="1", question="Will France win on 2026-06-15?",
            slug="france-win", end_date=utc_now() + timedelta(hours=1),
            hours_to_close=1, liquidity=1000, volume=2000, outcome="Yes",
            price=0.97, token_id="token", score=1,
            url="https://polymarket.com/event/q",
            best_bid=0.97, best_ask=0.99, tick_size=0.01, accepts_orders=True,
        )
        portfolio = Portfolio(cash=1.0, positions=[])
        position = portfolio.record_live_position(candidate, 8.7, entry_price=0.87)
        position["strategy"] = "grinder"
        position["current_price"] = 0.97
        exits = _execute_race_exits(
            build_client(Settings(dry_run=True)),
            Settings(dry_run=True, min_order_shares=5.0,
                     race_resolved_exit_threshold=0.97,
                     race_min_profit_margin=0.02,
                     race_sl_min_age_minutes=15, quiet=True),
            portfolio, [candidate], "grinder",
        )
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["reason"], "race_big_win_resolved")

    def test_race_resolved_exit_uses_position_price_when_pool_quote_missing(self):
        from polymarket_bot.race_strategies import _execute_race_exits

        candidate = Candidate(
            market_id="1",
            question="Q",
            slug="q",
            end_date=utc_now() + timedelta(hours=1),
            hours_to_close=1,
            liquidity=1000,
            volume=2000,
            outcome="Yes",
            price=0.94,
            token_id="token",
            score=1,
            url="https://polymarket.com/event/q",
            best_bid=0.93,
            best_ask=0.94,
            tick_size=0.01,
            accepts_orders=True,
        )
        portfolio = Portfolio(cash=1.0, positions=[])
        position = portfolio.record_live_position(candidate, 4.7, entry_price=0.94)
        self.assertIsNotNone(position)
        position["strategy"] = "grinder"
        position["current_price"] = 1.0

        exits = _execute_race_exits(
            build_client(Settings(dry_run=True)),
            Settings(
                dry_run=True,
                min_order_shares=5.0,
                race_resolved_exit_threshold=0.99,
                race_sl_min_age_minutes=15,
                quiet=True,
            ),
            portfolio,
            [],
            "grinder",
        )

        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["reason"], "race_big_win_resolved")
        self.assertEqual(position["status"], "closed")
        self.assertAlmostEqual(float(position["realized_pnl"]), 0.25)

    # ── Live-book bid probe (2026-06-10 regression) ──────────────────────
    # Gamma's flipped quote and the synced curPrice lag the CLOB: winners
    # whose real book bid sat at 0.99 showed 0.95 in the exit loop and were
    # never sold. The exit must trust the live order book.

    class _LiveBookClient:
        """Fake live TradingSession: order book + sell plumbing only."""

        def __init__(self, bids):
            self._bids = bids
            self.sells = []

        def get_order_book(self, token_id):
            if self._bids is None:
                raise RuntimeError("book unavailable")
            return {"bids": self._bids, "asks": []}

        def live_share_balance(self, token_id):
            return None  # keep the ledger's share count

        def place_live_order(self, *, candidate, price, size, side):
            self.sells.append({"price": price, "size": size, "side": side})
            return (
                {"side": side, "price": price, "size": size},
                {"success": True, "status": "matched", "orderID": "live-sell-1"},
            )

    def _race_exit_live_harness(self, client, stale_bid=0.95, threshold=0.99):
        import tempfile
        from polymarket_bot.race_strategies import _execute_race_exits

        candidate = Candidate(
            market_id="1",
            question="Will Israel close its airspace by June 11?",
            slug="israel-closes-its-airspace-by",
            end_date=utc_now() + timedelta(hours=1),
            hours_to_close=1,
            liquidity=1000,
            volume=2000,
            outcome="No",
            price=stale_bid,
            token_id="token",
            score=1,
            url="https://polymarket.com/event/israel-closes-its-airspace-by",
            best_bid=stale_bid,
            best_ask=stale_bid + 0.04,
            tick_size=0.01,
            accepts_orders=True,
        )
        portfolio = Portfolio(cash=1.0, positions=[])
        position = portfolio.record_live_position(candidate, 4.7, entry_price=0.89)
        self.assertIsNotNone(position)
        position["strategy"] = "grinder"
        position["current_price"] = stale_bid

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            exits = _execute_race_exits(
                client,
                Settings(
                    dry_run=False,
                    min_order_shares=5.0,
                    race_resolved_exit_threshold=threshold,
                    race_sl_min_age_minutes=15,
                    quiet=True,
                    state_path=base / "paper_state.json",
                    trade_journal_path=base / "trade_journal.jsonl",
                ),
                portfolio,
                [candidate],
                "grinder",
            )
        return exits, position, client

    def test_race_resolved_exit_uses_live_book_bid_over_stale_quote(self):
        # Stale view 0.95, real book bid 0.99 → the winner must be sold.
        client = self._LiveBookClient(bids=[{"price": "0.99", "size": "500"}])
        exits, position, client = self._race_exit_live_harness(client)

        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["reason"], "race_big_win_resolved")
        self.assertEqual(position["status"], "closed")
        self.assertEqual(len(client.sells), 1)
        self.assertAlmostEqual(client.sells[0]["price"], 0.99)

    def test_race_resolved_exit_holds_at_098_bid(self):
        # Threshold back to 0.99 (user 2026-06-10): a 0.98 best bid is NOT
        # enough — the displayed "98¢" is usually the midpoint, the position
        # is about to settle at 1.00, so the bot holds for a real 0.99 bid
        # or on-chain resolution.
        client = self._LiveBookClient(bids=[{"price": "0.98", "size": "500"}])
        exits, position, client = self._race_exit_live_harness(client)

        self.assertEqual(exits, [])
        self.assertEqual(position["status"], "open")
        self.assertEqual(client.sells, [])

    def test_fast_lane_winner_exit_fires_at_098(self):
        # User 2026-06-12: esports and stock markets exit at 0.98 — their
        # in-play/in-session books rarely print 0.99 before close. A CS
        # position with a real 0.98 bid must sell (other lanes hold, see
        # test_race_resolved_exit_holds_at_098_bid).
        import tempfile
        from polymarket_bot.race_strategies import _execute_race_exits

        candidate = Candidate(
            market_id="cs", question="Counter-Strike: Marsborne vs F5 Esports (BO3) - Playoffs",
            slug="cs-marsborne-f5", end_date=utc_now() + timedelta(hours=1),
            hours_to_close=1, liquidity=1500, volume=2000, outcome="Marsborne",
            price=0.95, token_id="tok-cs", score=1,
            url="https://polymarket.com/event/cs-marsborne-f5",
            best_bid=0.95, best_ask=0.97, tick_size=0.01, accepts_orders=True,
            event_slug="cs-marsborne-f5",
        )
        portfolio = Portfolio(cash=1.0, positions=[])
        position = portfolio.record_live_position(candidate, 4.7, entry_price=0.91)
        position["strategy"] = "grinder"
        position["current_price"] = 0.95

        client = self._LiveBookClient(bids=[{"price": "0.98", "size": "500"}])
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            exits = _execute_race_exits(
                client,
                Settings(
                    dry_run=False, min_order_shares=5.0,
                    race_resolved_exit_threshold=0.99,
                    race_sl_min_age_minutes=15, quiet=True,
                    state_path=base / "paper_state.json",
                    trade_journal_path=base / "trade_journal.jsonl",
                ),
                portfolio, [candidate], "grinder",
            )
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["reason"], "race_big_win_resolved")
        self.assertEqual(position["status"], "closed")
        self.assertAlmostEqual(client.sells[0]["price"], 0.98)

    def test_race_resolved_exit_book_probe_fails_open(self):
        # Book unavailable → keep the stale price and do nothing (no sell,
        # no writeoff): the position must stay open for the next tick.
        client = self._LiveBookClient(bids=None)
        exits, position, client = self._race_exit_live_harness(client)

        self.assertEqual(exits, [])
        self.assertEqual(position["status"], "open")
        self.assertEqual(client.sells, [])

    def test_live_sell_allows_full_position_below_nominal_share_minimum(self):
        candidate = Candidate(
            market_id="1",
            question="Q",
            slug="q",
            end_date=utc_now() + timedelta(hours=1),
            hours_to_close=1,
            liquidity=1000,
            volume=2000,
            outcome="Yes",
            price=0.99,
            token_id="token",
            score=1,
            url="https://polymarket.com/event/q",
            best_bid=0.99,
            best_ask=1.0,
            tick_size=0.01,
            accepts_orders=True,
        )
        portfolio = Portfolio(cash=1.0, positions=[])
        position = portfolio.record_live_position(candidate, 4.68, entry_price=0.94)
        self.assertIsNotNone(position)

        result = execute_live_sell(
            build_client(Settings(dry_run=True)),
            Settings(dry_run=True, min_order_shares=5.0, quiet=True),
            candidate,
            portfolio,
            position,
            shares=5.0,
            reason="race_big_win_resolved",
        )

        self.assertEqual(result.order["size"], 4.978723)
        self.assertEqual(position["status"], "closed")

    def test_live_sell_allows_full_position_below_min_sell_usd(self):
        candidate = Candidate(
            market_id="1",
            question="Q",
            slug="q",
            end_date=utc_now() + timedelta(hours=1),
            hours_to_close=1,
            liquidity=1000,
            volume=2000,
            outcome="Yes",
            price=0.01,
            token_id="token",
            score=1,
            url="https://polymarket.com/event/q",
            best_bid=0.01,
            best_ask=0.02,
            tick_size=0.01,
            accepts_orders=True,
        )
        portfolio = Portfolio(cash=1.0, positions=[])
        # entry at/below the 0.01 sell price so the loss-floor (never sell below
        # entry) does not block — this test covers the min-sell-USD bypass, not
        # a loss-sell.
        # stake 0.05 @ 0.01 = 5.0 shares (full position); proceeds 5.0*0.01=$0.05
        # is below smart_min_sell_usd, so this exercises the full-position bypass.
        position = portfolio.record_live_position(candidate, 0.05, entry_price=0.01)
        self.assertIsNotNone(position)

        result = execute_live_sell(
            build_client(Settings(dry_run=True)),
            Settings(dry_run=True, min_order_shares=5.0, smart_min_sell_usd=1.0, quiet=True),
            candidate,
            portfolio,
            position,
            shares=5.0,
            reason="race_take_profit",
        )

        self.assertEqual(result.order["size"], 5.0)
        self.assertEqual(position["status"], "closed")

    def test_race_exit_uses_live_position_mark_over_bad_scan_quote(self):
        from polymarket_bot.race_strategies import _execute_race_exits

        candidate = Candidate(
            market_id="1",
            question="Q",
            slug="q",
            end_date=utc_now() + timedelta(hours=1),
            hours_to_close=1,
            liquidity=1000,
            volume=2000,
            outcome="Yes",
            price=0.01,
            token_id="token",
            score=1,
            url="https://polymarket.com/event/q",
            best_bid=0.01,
            best_ask=0.02,
            tick_size=0.01,
            accepts_orders=True,
        )
        portfolio = Portfolio(cash=1.0, positions=[])
        position = portfolio.record_live_position(candidate, 4.58, entry_price=0.91)
        self.assertIsNotNone(position)
        position["strategy"] = "grinder"
        position["current_price"] = 0.93

        exits = _execute_race_exits(
            build_client(Settings(dry_run=True)),
            Settings(
                dry_run=True,
                min_order_shares=5.0,
                smart_min_sell_usd=1.0,
                race_sl_pct=0.15,
                race_tp_pct=0.06,
                race_resolved_exit_threshold=0.99,
                quiet=True,
            ),
            portfolio,
            [candidate],
            "grinder",
        )

        self.assertEqual(exits, [])
        self.assertEqual(position["status"], "open")
        self.assertFalse(position.get("journaled"))

    def test_race_sports_total_does_not_stop_loss_on_halftime_mark(self):
        from polymarket_bot.race_strategies import _execute_race_exits

        candidate = Candidate(
            market_id="1",
            question="CD Comerciantes Unidos vs. CD Garcilaso: O/U 4.5",
            slug="q",
            end_date=utc_now() + timedelta(hours=1),
            hours_to_close=1,
            liquidity=1000,
            volume=2000,
            outcome="Under",
            price=0.505,
            token_id="token",
            score=1,
            url="https://polymarket.com/event/q",
            best_bid=0.505,
            best_ask=0.515,
            tick_size=0.01,
            accepts_orders=True,
        )
        portfolio = Portfolio(cash=1.0, positions=[])
        position = portfolio.record_live_position(candidate, 4.58, entry_price=0.91)
        self.assertIsNotNone(position)
        position["strategy"] = "grinder"
        position["current_price"] = 0.505

        exits = _execute_race_exits(
            build_client(Settings(dry_run=True)),
            Settings(
                dry_run=True,
                min_order_shares=5.0,
                smart_min_sell_usd=1.0,
                race_sl_pct=0.15,
                race_tp_pct=0.06,
                race_resolved_exit_threshold=0.99,
                quiet=True,
            ),
            portfolio,
            [candidate],
            "grinder",
        )

        self.assertEqual(exits, [])
        self.assertEqual(position["status"], "open")
        self.assertFalse(position.get("journaled"))


class MarketCategoryTests(unittest.TestCase):
    def test_inflation_is_not_sports(self):
        self.assertEqual(
            market_category(
                "Will annual inflation increase by 3.9% in April?",
                "will-annual-inflation-increase-by-3pt9-in-april",
            ),
            "ECONOMICS",
        )

    def test_actual_nfl_market_is_sports(self):
        self.assertEqual(
            market_category("Will the NFL Cowboys win on Sunday?", "nfl-cowboys-sun"),
            "SPORTS",
        )

    def test_soccer_fc_market_is_sports(self):
        self.assertEqual(
            market_category("Will Liverpool FC win on 2026-05-09?", "liverpool-fc-may-9"),
            "SPORTS",
        )

    def test_btc_market_is_finance_not_sports(self):
        self.assertEqual(
            market_category("Will Bitcoin be above $100,000 today?", "btc-above-100k"),
            "FINANCE",
        )


class PortfolioDedupTests(unittest.TestCase):
    def _make_candidate(self, *, market_id, outcome, token_id, event_slug):
        return Candidate(
            market_id=market_id,
            question="Will the highest temperature in Seoul be 20C on May 9?",
            slug="seoul-temp-may-9",
            end_date=utc_now() + timedelta(hours=24),
            hours_to_close=24,
            liquidity=10000,
            volume=20000,
            outcome=outcome,
            price=0.30,
            token_id=token_id,
            score=10,
            url="https://polymarket.com/event/seoul-temp-may-9",
            best_bid=0.29,
            best_ask=0.31,
            tick_size=0.01,
            accepts_orders=True,
            event_slug=event_slug,
        )

    def test_event_dedupe_blocks_no_when_yes_open_on_non_sports_market(self):
        portfolio = Portfolio(cash=50.0, positions=[])
        yes_candidate = self._make_candidate(
            market_id="seoul-yes",
            outcome="Yes",
            token_id="tok-yes",
            event_slug="seoul-temp-may-9",
        )
        portfolio.record_live_position(yes_candidate, 5.0, entry_price=0.30, order_id="o1")
        no_candidate = self._make_candidate(
            market_id="seoul-no",
            outcome="No",
            token_id="tok-no",
            event_slug="seoul-temp-may-9",
        )
        self.assertTrue(portfolio.has_open_event_position(no_candidate))

    def test_event_dedupe_does_not_block_unrelated_event(self):
        portfolio = Portfolio(cash=50.0, positions=[])
        seoul_yes = self._make_candidate(
            market_id="seoul-yes",
            outcome="Yes",
            token_id="tok-yes",
            event_slug="seoul-temp-may-9",
        )
        portfolio.record_live_position(seoul_yes, 5.0, entry_price=0.30, order_id="o1")
        elon_candidate = self._make_candidate(
            market_id="elon-tweets",
            outcome="Yes",
            token_id="tok-elon",
            event_slug="elon-tweets-may-7-9",
        )
        self.assertFalse(portfolio.has_open_event_position(elon_candidate))


class AutoTunerTests(unittest.TestCase):
    def test_compute_overrides_paused_below_minimum_trades(self):
        records = [{"realized_pnl": -1.0, "consensus": 2, "exit_reason": "stop_loss"} for _ in range(5)]
        settings = Settings(smart_auto_tune_min_trades=30)
        self.assertEqual(compute_overrides(records, settings), {})

    def test_compute_overrides_tightens_chase_when_stop_loss_dominates(self):
        records = [
            {"realized_pnl": -1.0, "consensus": 2, "exit_reason": "stop_loss", "category": "OTHER"}
            for _ in range(40)
        ]
        settings = Settings(
            smart_auto_tune_min_trades=30,
            smart_max_chase_premium=0.10,
            smart_max_relative_spread=0.30,
        )
        overrides = compute_overrides(records, settings)
        self.assertIn("smart_max_chase_premium", overrides)
        self.assertLess(overrides["smart_max_chase_premium"], 0.10)
        self.assertIn("smart_max_relative_spread", overrides)
        self.assertLess(overrides["smart_max_relative_spread"], 0.30)

    def test_compute_overrides_raises_consensus_when_two_wallets_lose(self):
        records = []
        for _ in range(40):
            records.append(
                {
                    "realized_pnl": -1.0,
                    "consensus": 2,
                    "exit_reason": "stop_loss",
                    "category": "OTHER",
                }
            )
        settings = Settings(
            smart_auto_tune_min_trades=30,
            smart_min_consensus=2,
            smart_max_chase_premium=0.04,
            smart_max_relative_spread=0.20,
        )
        overrides = compute_overrides(records, settings)
        self.assertEqual(overrides.get("smart_min_consensus"), 3)

    def test_compute_overrides_no_change_when_winning(self):
        records = [
            {"realized_pnl": 1.5, "consensus": 2, "exit_reason": "take_profit_100pct", "category": "POLITICS"}
            for _ in range(40)
        ]
        settings = Settings(smart_auto_tune_min_trades=30)
        overrides = compute_overrides(records, settings)
        self.assertEqual(overrides, {})


class SettingsDryRunTests(unittest.TestCase):
    # Path() comparisons, not str(): str(Path) renders backslashes on Windows
    # and the string-equality version of these tests (and the swap itself,
    # fixed 2026-06-11) silently never matched there.
    def test_live_mode_uses_default_paths(self):
        from pathlib import Path
        s = Settings(dry_run=False)
        self.assertEqual(s.state_path, Path("data/paper_state.json"))
        self.assertEqual(s.trade_journal_path, Path("data/trade_journal.jsonl"))
        self.assertEqual(s.strategy_overrides_path, Path("data/strategy_overrides.json"))
        self.assertEqual(s.tick_state_path, Path("data/last_tick.json"))
        self.assertEqual(s.tick_history_path, Path("data/tick_history.jsonl"))

    def test_dry_run_swaps_all_data_paths(self):
        from pathlib import Path
        s = Settings(dry_run=True)
        self.assertEqual(s.state_path, Path("data/dry_run_state.json"))
        self.assertEqual(s.trade_journal_path, Path("data/dry_run_journal.jsonl"))
        self.assertEqual(s.strategy_overrides_path, Path("data/dry_run_strategy_overrides.json"))
        self.assertEqual(s.tick_state_path, Path("data/dry_run_last_tick.json"))
        self.assertEqual(s.tick_history_path, Path("data/dry_run_tick_history.jsonl"))

    def test_dry_run_preserves_explicit_custom_paths(self):
        from pathlib import Path
        s = Settings(
            dry_run=True,
            state_path=Path("/tmp/custom_state.json"),
            trade_journal_path=Path("/tmp/custom_journal.jsonl"),
            strategy_overrides_path=Path("/tmp/custom_over.json"),
            tick_state_path=Path("/tmp/custom_tick.json"),
            tick_history_path=Path("/tmp/custom_hist.jsonl"),
        )
        self.assertEqual(s.state_path, Path("/tmp/custom_state.json"))
        self.assertEqual(s.trade_journal_path, Path("/tmp/custom_journal.jsonl"))
        self.assertEqual(s.strategy_overrides_path, Path("/tmp/custom_over.json"))
        self.assertEqual(s.tick_state_path, Path("/tmp/custom_tick.json"))
        self.assertEqual(s.tick_history_path, Path("/tmp/custom_hist.jsonl"))


class LossCooldownTests(unittest.TestCase):
    def test_token_in_loss_cooldown_reads_latest_exit_record(self):
        now = datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc)
        portfolio = Portfolio(
            cash=10.0,
            positions=[
                {
                    "status": "closed",
                    "token_id": "tok-loss",
                    "exits": [
                        {
                            "reason": "near_expiry_loser_flush",
                            "realized_pnl": -0.05,
                            "closed_at": (now - timedelta(minutes=30)).isoformat(),
                        }
                    ],
                }
            ],
        )
        settings = Settings(smart_entry_cooldown_after_loss_minutes=180)
        self.assertTrue(_token_in_loss_cooldown(portfolio, "tok-loss", settings, now=now))

    def test_token_loss_cooldown_expires_and_ignores_winners(self):
        now = datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc)
        settings = Settings(smart_entry_cooldown_after_loss_minutes=60)
        old_loss = Portfolio(
            cash=10.0,
            positions=[
                {
                    "status": "closed",
                    "token_id": "tok-old",
                    "exits": [
                        {
                            "reason": "resolved_market_sweep_loss",
                            "realized_pnl": -1.0,
                            "closed_at": (now - timedelta(minutes=90)).isoformat(),
                        }
                    ],
                }
            ],
        )
        winner = Portfolio(
            cash=10.0,
            positions=[
                {
                    "status": "closed",
                    "token_id": "tok-win",
                    "exits": [
                        {
                            "reason": "resolved_market_sweep_loss",
                            "realized_pnl": 1.0,
                            "closed_at": (now - timedelta(minutes=10)).isoformat(),
                        }
                    ],
                }
            ],
        )
        self.assertFalse(_token_in_loss_cooldown(old_loss, "tok-old", settings, now=now))
        self.assertFalse(_token_in_loss_cooldown(winner, "tok-win", settings, now=now))


class TickStateLoopTests(unittest.TestCase):
    def test_strategy_loop_writes_tick_state_on_each_tick(self):
        import tempfile
        from pathlib import Path
        from polymarket_bot import tick_state
        from polymarket_bot.main import strategy_loop

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            settings = Settings(
                auto_max_ticks=2,
                auto_interval_seconds=0,
                quiet=True,
                tick_state_path=tmp_path / "last_tick.json",
                tick_history_path=tmp_path / "tick_history.jsonl",
            )

            calls: list[int] = []

            def fake_tick(s):
                calls.append(len(calls) + 1)
                return {
                    "scan_report": {"selected": None, "opportunities": [], "traders_checked": 0},
                    "scan_counts": {"strict": 0, "relaxed": 1, "deep": 0, "candidates_total": 5},
                    "exits": [{"market_id": "mid-1", "question": "Will M1 close yes?", "outcome": "Yes", "action": "sell", "reason": "tp", "pnl_pct": 0.10}],
                    "noise_trades": [],
                    "rejected_signals": [{"market_id": "mid-skip", "question": "Skipped market?", "outcome": "Yes", "reason": "chase too high"}],
                    "auto_tune_info": {"applied": False, "journal_size": 5, "overrides_active": {}},
                    "summary": {"equity": 100.0, "cash": 50.0, "invested": 50.0},
                    "trade": {
                        "strategy": "smart_money",
                        "signal": {"question": "Will M2 happen?", "stake_usd": 2.0, "selection_reason": "consensus 3"},
                        "order": {},
                        "response": {},
                    },
                }

            strategy_loop(settings, "smart_money", fake_tick)

            self.assertEqual(len(calls), 2)
            last = tick_state.read_last_tick(settings)
            self.assertIsNotNone(last)
            self.assertEqual(last["mode"], "live")
            self.assertIn("scan_counts", last)
            self.assertEqual(last["scan_counts"]["relaxed"], 1)
            self.assertIn("actions", last)
            self.assertIn("next_tick_at", last)
            history = tick_state.read_tick_history(settings, limit=10)
            self.assertEqual(len(history), 2)
            self.assertEqual(history[0]["tick_id"], 2)
            self.assertEqual(history[1]["tick_id"], 1)

    def test_strategy_loop_records_dry_run_mode(self):
        import tempfile
        from pathlib import Path
        from polymarket_bot import tick_state
        from polymarket_bot.main import strategy_loop

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            settings = Settings(
                dry_run=True,
                auto_max_ticks=1,
                auto_interval_seconds=0,
                quiet=True,
                tick_state_path=tmp_path / "last_tick.json",
                tick_history_path=tmp_path / "tick_history.jsonl",
            )

            def fake_tick(s):
                return {"summary": {"equity": 0}}

            strategy_loop(settings, "smart_money", fake_tick)
            last = tick_state.read_last_tick(settings)
            self.assertEqual(last["mode"], "dry_run")

    def test_strategy_loop_records_error_ticks(self):
        import tempfile
        from pathlib import Path
        from polymarket_bot import tick_state
        from polymarket_bot.main import strategy_loop

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            settings = Settings(
                auto_max_ticks=1,
                auto_interval_seconds=0,
                quiet=True,
                tick_state_path=tmp_path / "last_tick.json",
                tick_history_path=tmp_path / "tick_history.jsonl",
            )

            def boom(s):
                raise RuntimeError("kaboom")

            strategy_loop(settings, "smart_money", boom)
            last = tick_state.read_last_tick(settings)
            self.assertIsNotNone(last)
            self.assertEqual(last["error"], {"type": "RuntimeError", "message": "kaboom"})
            self.assertEqual(last["actions"], [])

    def test_extract_tick_actions_handles_real_payload_shape(self):
        from polymarket_bot.main import _extract_tick_actions

        actions = _extract_tick_actions({
            "trade": {
                "strategy": "smart_money",
                "signal": {"question": "Q-buy?", "stake_usd": 3.0, "selection_reason": "x"},
            },
            "noise_trades": [{
                "strategy": "noise_fallback",
                "signal": {"question": "Q-noise?", "stake_usd": 1.0},
            }],
            "exits": [
                {"market_id": "m1", "question": "Q-sell?", "outcome": "Yes", "action": "sell", "reason": "stop_loss"},
                {"market_id": "m2", "question": "Q-skipsell?", "outcome": "Yes", "action": "skip_sell", "reason": "below floor"},
            ],
            "rejected_signals": [
                {"market_id": "m3", "question": "Q-skip?", "outcome": "No", "reason": "chase 0.18"},
            ],
        })

        types = [a["type"] for a in actions]
        markets = [a["market"] for a in actions]
        # Two buys (primary + noise), one sell (skip_sell is filtered), one skip
        self.assertEqual(types, ["buy", "buy", "sell", "skip"])
        self.assertEqual(markets, ["Q-buy?", "Q-noise?", "Q-sell?", "Q-skip?"])

    def test_extract_tick_actions_returns_empty_for_empty_tick(self):
        from polymarket_bot.main import _extract_tick_actions
        self.assertEqual(_extract_tick_actions({}), [])

    def test_extract_tick_actions_includes_btc_edge_buy(self):
        from polymarket_bot.main import _extract_tick_actions
        actions = _extract_tick_actions({
            "btc_edge": {
                "trade": {
                    "strategy": "btc_edge",
                    "signal": {"question": "BTC > 100k?", "stake_usd": 4.0, "selection_reason": "edge=0.12"},
                },
            },
        })
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["type"], "buy")
        self.assertEqual(actions[0]["strategy"], "btc_edge")
        self.assertEqual(actions[0]["market"], "BTC > 100k?")

    def test_extract_tick_actions_skips_btc_edge_when_no_trade(self):
        from polymarket_bot.main import _extract_tick_actions
        # btc_edge_once returns {"trade": None, ...} when no signal
        actions = _extract_tick_actions({
            "btc_edge": {"trade": None, "no_signal": "edge below threshold"},
        })
        self.assertEqual(actions, [])


class JournalStatsDrawdownTests(unittest.TestCase):
    def test_max_drawdown_zero_when_only_wins(self):
        import tempfile
        import json
        from pathlib import Path
        from polymarket_bot.main import journal_stats

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "journal.jsonl"
            records = [
                {"closed_at": "2026-01-01T00:00:00Z", "realized_pnl": 1.0},
                {"closed_at": "2026-01-02T00:00:00Z", "realized_pnl": 2.0},
                {"closed_at": "2026-01-03T00:00:00Z", "realized_pnl": 0.5},
            ]
            tmp_path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
            stats = journal_stats(Settings(trade_journal_path=tmp_path))
            self.assertEqual(stats["max_drawdown"], 0.0)

    def test_max_drawdown_captures_worst_peak_to_trough(self):
        import tempfile
        import json
        from pathlib import Path
        from polymarket_bot.main import journal_stats

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "journal.jsonl"
            # Cumulative PnL: +5, +6, +1, +3, -2, +1
            # Peaks:           5,  6,  6, 6,  6,  6
            # Drawdowns:       0,  0, -5, -3, -8, -5
            # Max drawdown = -8
            records = [
                {"closed_at": "2026-01-01T00:00:00Z", "realized_pnl": 5.0},
                {"closed_at": "2026-01-02T00:00:00Z", "realized_pnl": 1.0},
                {"closed_at": "2026-01-03T00:00:00Z", "realized_pnl": -5.0},
                {"closed_at": "2026-01-04T00:00:00Z", "realized_pnl": 2.0},
                {"closed_at": "2026-01-05T00:00:00Z", "realized_pnl": -5.0},
                {"closed_at": "2026-01-06T00:00:00Z", "realized_pnl": 3.0},
            ]
            tmp_path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
            stats = journal_stats(Settings(trade_journal_path=tmp_path))
            self.assertEqual(stats["max_drawdown"], -8.0)

    def test_max_drawdown_handles_unsorted_records(self):
        import tempfile
        import json
        from pathlib import Path
        from polymarket_bot.main import journal_stats

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "journal.jsonl"
            records = [
                {"closed_at": "2026-01-03T00:00:00Z", "realized_pnl": -5.0},
                {"closed_at": "2026-01-01T00:00:00Z", "realized_pnl": 5.0},
                {"closed_at": "2026-01-02T00:00:00Z", "realized_pnl": 1.0},
            ]
            tmp_path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
            stats = journal_stats(Settings(trade_journal_path=tmp_path))
            self.assertEqual(stats["max_drawdown"], -5.0)

    def test_journal_stats_reads_realized_cache_and_dedupes_journal(self):
        import tempfile
        import json
        from pathlib import Path
        from polymarket_bot.main import journal_stats

        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.jsonl"
            cache = Path(tmp) / "realized_trade_cache.jsonl"
            fred = {
                "closed_at": "2026-05-25T10:00:00+00:00",
                "token_id": "fred",
                "exit_reason": "race_take_profit",
                "realized_pnl": 0.45,
            }
            btc = {
                "closed_at": "2026-05-25T11:00:00+00:00",
                "token_id": "btc",
                "exit_reason": "race_big_win_resolved",
                "realized_pnl": 0.25,
            }
            journal.write_text(json.dumps(fred) + "\n", encoding="utf-8")
            cache.write_text(json.dumps(fred) + "\n" + json.dumps(btc) + "\n", encoding="utf-8")

            stats = journal_stats(Settings(trade_journal_path=journal, realized_cache_path=cache))

            self.assertEqual(stats["records"], 2)
            self.assertEqual(stats["wins"], 2)
            self.assertEqual(stats["losses"], 0)
            self.assertEqual(stats["total_pnl"], 0.7)

    def test_starting_equity_prefers_live_profile_snapshot(self):
        import tempfile
        from pathlib import Path
        from polymarket_bot.main import _starting_equity_for_stats

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "live_baseline.json").write_text('{"starting_cash": 7.34}', encoding="utf-8")
            (base / "live_config_snapshot.toml").write_text(
                "[run]\nstarting_cash = 6.0\n",
                encoding="utf-8",
            )

            value = _starting_equity_for_stats(
                Settings(
                    state_path=base / "paper_state.json",
                    paper_balance_usd=1.0,
                    assumed_live_balance_usd=1.0,
                )
            )

            self.assertEqual(value, 6.0)


class JournalSuggestionsTests(unittest.TestCase):
    def _records(self, n, exit_reason="take_profit_50", pnl=1.0, **extra):
        base = {"realized_pnl": pnl, "exit_reason": exit_reason, "category": "POLITICS", "consensus": 3}
        base.update(extra)
        return [dict(base) for _ in range(n)]

    def test_suggestions_empty_when_below_min_trades(self):
        from polymarket_bot.main import _journal_suggestions
        result = _journal_suggestions(self._records(5))
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "below_min_trades")
        self.assertEqual(result[0]["records"], 5)

    def test_suggestions_flag_excessive_stop_loss(self):
        from polymarket_bot.main import _journal_suggestions
        records = self._records(20, exit_reason="stop_loss", pnl=-0.5) + self._records(20, exit_reason="take_profit_50", pnl=0.5)
        suggestions = _journal_suggestions(records)
        ids = {s["id"] for s in suggestions}
        self.assertIn("excessive_stop_loss", ids)
        sl = next(s for s in suggestions if s["id"] == "excessive_stop_loss")
        self.assertEqual(sl["param"], "MAX_CHASE_PREMIUM")
        self.assertEqual(sl["ratio"], 0.80)
        self.assertIn("stop_loss", sl["reason"])

    def test_format_suggestions_returns_human_lines(self):
        from polymarket_bot.main import format_suggestions
        structured = [
            {"id": "below_min_trades", "param": None, "ratio": None, "reason": "only 5 closed trades"},
            {"id": "excessive_stop_loss", "param": "MAX_CHASE_PREMIUM", "ratio": 0.80, "reason": "stop_loss = 50% of 40 trades"},
        ]
        lines = format_suggestions(structured)
        self.assertEqual(len(lines), 2)
        self.assertIn("only 5 closed trades", lines[0])
        self.assertIn("MAX_CHASE_PREMIUM", lines[1])
        self.assertIn("0.80", lines[1])

    def test_journal_stats_suggestions_field_is_structured(self):
        import tempfile, json
        from pathlib import Path
        from polymarket_bot.main import journal_stats

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "journal.jsonl"
            tmp_path.write_text("\n".join(
                json.dumps({"realized_pnl": 1.0, "exit_reason": "take_profit_50"}) for _ in range(5)
            ) + "\n")
            stats = journal_stats(Settings(trade_journal_path=tmp_path))
            suggestions = stats["suggestions"]
            self.assertTrue(suggestions)
            self.assertIsInstance(suggestions[0], dict)
            self.assertIn("id", suggestions[0])


class ActionableCandidatesTests(unittest.TestCase):
    """Regression for pick-slot burning (2026-06-10): with one Spurs/Knicks
    position open, the four Spurs O/U lines outscored everything (soonest to
    close), filled all race_max_orders_per_tick pick slots, and were then
    skipped as duplicates — the 5th-ranked PPI market was never attempted.
    Already-held markets must be dropped BEFORE the selector truncates."""

    @staticmethod
    def _candidate(market_id, question, slug, event_slug, token_id, hours, bid):
        return Candidate(
            market_id=market_id,
            question=question,
            slug=slug,
            end_date=utc_now() + timedelta(hours=hours),
            hours_to_close=hours,
            liquidity=1500,
            volume=2000,
            outcome="Under",
            price=bid,
            token_id=token_id,
            score=1,
            url=f"https://polymarket.com/event/{event_slug}",
            best_bid=bid,
            best_ask=bid + 0.01,
            tick_size=0.01,
            accepts_orders=True,
            event_slug=event_slug,
        )

    def test_held_event_does_not_burn_pick_slots(self):
        from polymarket_bot.race_strategies import _actionable_candidates, select_grinder

        spurs_lines = [
            self._candidate(str(i), f"Spurs vs. Knicks: O/U 19{i}.5", f"spurs-knicks-ou-19{i}",
                            "nba-sas-nyk-2026-06-10", f"tok-{i}", hours=0.5, bid=0.91)
            for i in range(4)
        ]
        ppi = self._candidate("99", "Will PPI YoY be between 7.0% and 7.9% in May?",
                              "ppi-yoy-70-79", "producer-price-index-ppi-yoy-may-2026",
                              "tok-ppi", hours=6.0, bid=0.92)
        eligible = [(c, 0.0) for c in spurs_lines + [ppi]]

        portfolio = Portfolio(cash=900.0, positions=[{
            "status": "open",
            "token_id": "tok-0",
            "market_id": "0",
            "question": "Spurs vs. Knicks: O/U 196.5",
            "event_slug": "nba-sas-nyk-2026-06-10",
            "stake": 357.8,
        }])

        # race_stake_pct=0 disables the top-up lane → held markets all drop.
        actionable = _actionable_candidates(eligible, portfolio, Settings(race_stake_pct=0.0))
        self.assertEqual([c.market_id for c, _ in actionable], ["99"])

        picks = select_grinder(actionable, 4)
        self.assertEqual([c.market_id for c in picks], ["99"])

    def test_nothing_held_keeps_all_candidates(self):
        from polymarket_bot.race_strategies import _actionable_candidates

        ppi = self._candidate("99", "Will PPI YoY be between 7.0% and 7.9% in May?",
                              "ppi-yoy-70-79", "producer-price-index-ppi-yoy-may-2026",
                              "tok-ppi", hours=6.0, bid=0.92)
        eligible = [(ppi, 0.0)]
        portfolio = Portfolio(cash=900.0, positions=[])
        self.assertEqual(len(_actionable_candidates(eligible, portfolio, Settings())), 1)

    def test_held_token_stays_actionable_while_below_position_cap(self):
        # Top-up lane (2026-06-10): a depth-capped entry ($229 of a $379
        # target) keeps its market actionable so later ticks can complete
        # the position — but only up to equity × race_stake_pct.
        from polymarket_bot.race_strategies import _actionable_candidates

        ppi = self._candidate("99", "Will PPI YoY be between 7.0% and 7.9% in May?",
                              "ppi-yoy-70-79", "producer-price-index-ppi-yoy-may-2026",
                              "tok-ppi", hours=6.0, bid=0.92)
        eligible = [(ppi, 0.0)]
        portfolio = Portfolio(cash=900.0, positions=[{
            "status": "open",
            "token_id": "tok-ppi",
            "market_id": "99",
            "question": "Will PPI YoY be between 7.0% and 7.9% in May?",
            "event_slug": "producer-price-index-ppi-yoy-may-2026",
            "stake": 229.0,
        }])
        # equity = 900 + 229 = 1129. cap @30% = 338.7 → room $109.7 → kept.
        # (ceiling_usd=0 mirrors the live profiles, where it is disabled.)
        kept = _actionable_candidates(
            eligible, portfolio,
            Settings(race_stake_pct=0.30, smart_max_position_ceiling_usd=0.0),
        )
        self.assertEqual([c.market_id for c, _ in kept], ["99"])
        # cap @20% = 225.8 < stake → no room → dropped.
        dropped = _actionable_candidates(
            eligible, portfolio,
            Settings(race_stake_pct=0.20, smart_max_position_ceiling_usd=0.0),
        )
        self.assertEqual(dropped, [])

    def test_top_up_live_position_averages_the_fill(self):
        portfolio = Portfolio(cash=661.86, positions=[{
            "status": "open",
            "token_id": "tok-ppi",
            "market_id": "99",
            "question": "Will PPI YoY be between 7.0% and 7.9% in May?",
            "event_slug": "producer-price-index-ppi-yoy-may-2026",
            "stake": 228.51,
            "shares": 240.64842,
            "entry_price": 0.9496,
        }])

        pos = portfolio.top_up_live_position("tok-ppi", 100.0, 0.96, order_id="order-2")

        self.assertIsNotNone(pos)
        self.assertAlmostEqual(pos["stake"], 328.51, places=2)
        self.assertAlmostEqual(pos["shares"], 240.64842 + 100.0 / 0.96, places=4)
        self.assertAlmostEqual(pos["entry_price"], 328.51 / (240.64842 + 100.0 / 0.96), places=4)
        self.assertAlmostEqual(portfolio.cash, 561.86, places=2)
        self.assertEqual(pos["topup_order_ids"], ["order-2"])
        self.assertEqual(len(portfolio.positions), 1)

    def test_execute_live_trade_tops_up_held_token(self):
        # Same token already held → the buy must average into the existing
        # position (no duplicate_open_sports_event despite the same event).
        class FakeClient:
            def live_available_balance(self):
                return 661.86

            def place_market_order(self, *, candidate, amount, side="BUY", price=0.0):
                return {"price": price, "amount": amount, "side": side}, {
                    "success": True,
                    "status": "matched",
                    "orderID": "order-2",
                    "makingAmount": "100.0",
                    "takingAmount": "104.16",
                }

        candidate = self._candidate("99", "Will PPI YoY be between 7.0% and 7.9% in May?",
                                    "ppi-yoy-70-79", "producer-price-index-ppi-yoy-may-2026",
                                    "tok-ppi", hours=5.0, bid=0.95)
        portfolio = Portfolio(cash=661.86, positions=[{
            "status": "open",
            "token_id": "tok-ppi",
            "market_id": "99",
            "question": "Will PPI YoY be between 7.0% and 7.9% in May?",
            "event_slug": "producer-price-index-ppi-yoy-may-2026",
            "stake": 228.51,
            "shares": 240.64842,
            "entry_price": 0.9496,
        }])

        execute_live_trade(
            FakeClient(),
            Settings(trade_fraction=0.95, min_order_shares=5.0),
            candidate,
            portfolio,
            min_trade_usd=1.0,
            max_trade_usd=110.0,
        )

        self.assertEqual(len(portfolio.positions), 1)
        pos = portfolio.positions[0]
        self.assertAlmostEqual(pos["stake"], 328.51, places=2)
        self.assertAlmostEqual(pos["shares"], 240.64842 + 104.16, places=2)
        self.assertAlmostEqual(portfolio.cash, 561.86, places=2)


class EntryWindowStartOrCloseTests(unittest.TestCase):
    """User rule 2026-06-14: keep a market only if its game STARTS within the
    next max_hours OR it CLOSES within the next max_hours."""

    def _market(self, mid, *, end_h, start_h=None):
        m = {
            "id": mid, "question": f"Will Team {mid} win on 2026-06-14?",
            "slug": f"team-{mid}-win", "acceptingOrders": True,
            "liquidity": 1500, "volume24hr": 2000,
            "bestBid": 0.91, "bestAsk": 0.92, "orderPriceMinTickSize": 0.01,
            "outcomes": '["Yes", "No"]', "outcomePrices": '["0.92", "0.08"]',
            "clobTokenIds": f'["tok-{mid}-y", "tok-{mid}-n"]',
            "endDate": (utc_now() + timedelta(hours=end_h)).isoformat(),
        }
        if start_h is not None:
            m["gameStartTime"] = (utc_now() + timedelta(hours=start_h)).isoformat()
        return m

    def test_keeps_close_soon_or_start_soon_drops_the_rest(self):
        from polymarket_bot.race_strategies import _build_eligible_candidates

        settings = Settings(race_min_price=0.85, race_max_price=0.97,
                            race_max_spread=0.04, race_max_hours=4.0)
        markets = [
            self._market("close3", end_h=3.0),                  # closes in 3h ✓
            self._market("start2", end_h=6.0, start_h=2.0),     # starts in 2h ✓
            self._market("inprog", end_h=6.0, start_h=-1.0),    # started, closes 6h ✗
            self._market("late", end_h=7.0),                    # closes in 7h ✗
        ]
        ids = {c.market_id for c, _ in _build_eligible_candidates(markets, settings)}
        self.assertEqual(ids, {"close3", "start2"})


class DynamicEntryWindowTests(unittest.TestCase):
    """User rule 2026-06-11: prefer bets ≤4h from resolution; if nothing is
    actionable, widen the window 4 → 6 → 8 → 10 and stop at 12h max."""

    def test_entry_window_ladder_steps_by_2h_to_the_cap(self):
        from polymarket_bot.race_strategies import _entry_window_ladder

        ladder = _entry_window_ladder(Settings(race_max_hours=4.0, race_max_hours_cap=12.0))
        self.assertEqual(ladder, [4.0, 6.0, 8.0, 10.0, 12.0])

    def test_entry_window_ladder_jumps_to_24h_cap_then_daily_rung(self):
        # User rule 2026-06-12: 4 → 6 → 8 → 10 → 12 → 24, and when even 24h
        # is empty, one last rung to the end of TOMORROW (UTC) so daily
        # markets like the Trump-approval one stay reachable.
        from datetime import datetime, timezone
        from polymarket_bot.race_strategies import _entry_window_ladder

        now = datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc)
        ladder = _entry_window_ladder(
            Settings(race_max_hours=4.0, race_max_hours_cap=24.0,
                     race_daily_expiry_fallback=True),
            now=now,
        )
        # End of tomorrow = 2026-06-14T00:00Z → 38h from 10:00Z.
        self.assertEqual(ladder, [4.0, 6.0, 8.0, 10.0, 12.0, 24.0, 38.0])

        # Late in the day the daily rung still clears the 24h cap (25h).
        late = datetime(2026, 6, 12, 23, 0, tzinfo=timezone.utc)
        ladder = _entry_window_ladder(
            Settings(race_max_hours=4.0, race_max_hours_cap=24.0,
                     race_daily_expiry_fallback=True),
            now=late,
        )
        self.assertEqual(ladder[-1], 25.0)
        self.assertEqual(ladder[:-1], [4.0, 6.0, 8.0, 10.0, 12.0, 24.0])

        # Fallback off → ladder stops at the cap.
        ladder = _entry_window_ladder(
            Settings(race_max_hours=4.0, race_max_hours_cap=24.0), now=now
        )
        self.assertEqual(ladder, [4.0, 6.0, 8.0, 10.0, 12.0, 24.0])

    def test_entry_window_ladder_disabled_without_cap(self):
        from polymarket_bot.race_strategies import _entry_window_ladder

        self.assertEqual(_entry_window_ladder(Settings(race_max_hours=4.0)), [4.0])
        self.assertEqual(
            _entry_window_ladder(Settings(race_max_hours=4.0, race_max_hours_cap=3.0)),
            [4.0],
        )

    def test_narrow_window_preferred_wide_used_only_when_empty(self):
        from polymarket_bot.race_strategies import (
            _actionable_candidates,
            _entry_window_ladder,
        )

        def pick_window(eligible, portfolio, settings):
            ladder = _entry_window_ladder(settings)
            actionable, used = [], ladder[0]
            for used in ladder:
                subset = [(c, m) for c, m in eligible if (c.hours_to_close or 0.0) <= used]
                actionable = _actionable_candidates(subset, portfolio, settings)
                if actionable:
                    break
            return actionable, used

        near = ActionableCandidatesTests._candidate(
            "1", "Will France win on 2026-06-11?", "france-win", "ev-near", "tok-1",
            hours=3.0, bid=0.92,
        )
        far = ActionableCandidatesTests._candidate(
            "2", "Will CPI YoY be 3.1% in June?", "cpi-31", "ev-far", "tok-2",
            hours=7.5, bid=0.93,
        )
        settings = Settings(race_max_hours=4.0, race_max_hours_cap=12.0)
        empty = Portfolio(cash=100.0, positions=[])

        # A 3h candidate exists → stay at the 4h window; the 7.5h one waits.
        actionable, used = pick_window([(near, 0.0), (far, 0.0)], empty, settings)
        self.assertEqual(used, 4.0)
        self.assertEqual([c.market_id for c, _ in actionable], ["1"])

        # Nothing ≤4h or ≤6h → the 8h rung catches the 7.5h candidate.
        actionable, used = pick_window([(far, 0.0)], empty, settings)
        self.assertEqual(used, 8.0)
        self.assertEqual([c.market_id for c, _ in actionable], ["2"])

        # Nothing within the 12h cap → no bet, the ladder stops.
        nothing = ActionableCandidatesTests._candidate(
            "3", "Will X happen?", "x", "ev-x", "tok-3", hours=14.0, bid=0.92,
        )
        actionable, used = pick_window([(nothing, 0.0)], empty, settings)
        self.assertEqual(used, 12.0)
        self.assertEqual(actionable, [])


class SameEventDedupTests(unittest.TestCase):
    """User rule 2026-06-11/14: one bet per game; keep the single best
    (highest-bid) candidate. The soccer under-4.5 priority was dropped
    2026-06-14 — just take the best bet for each game."""

    def test_best_bid_wins_per_game_no_under_45_priority(self):
        # User 2026-06-14: no more under-4.5 preference. The moneyline at
        # 0.94 beats the under-4.5 at 0.90 purely on bid, either order.
        from polymarket_bot.race_strategies import _actionable_candidates

        moneyline = Candidate(
            market_id="ml", question="Will Nantes win on 2026-06-11?",
            slug="nantes-win", end_date=utc_now() + timedelta(hours=2),
            hours_to_close=2, liquidity=1500, volume=2000, outcome="Yes",
            price=0.94, token_id="tok-ml", score=1,
            url="https://polymarket.com/event/fc-nantes-vs-psg",
            best_bid=0.94, best_ask=0.95, tick_size=0.01, accepts_orders=True,
            event_slug="fc-nantes-vs-psg",
        )
        under = Candidate(
            market_id="u45", question="FC Nantes vs. PSG: O/U 4.5",
            slug="nantes-psg-ou-45", end_date=utc_now() + timedelta(hours=2),
            hours_to_close=2, liquidity=1500, volume=2000, outcome="Under",
            price=0.90, token_id="tok-u45", score=1,
            url="https://polymarket.com/event/fc-nantes-vs-psg",
            best_bid=0.90, best_ask=0.91, tick_size=0.01, accepts_orders=True,
            event_slug="fc-nantes-vs-psg",
        )
        empty = Portfolio(cash=100.0, positions=[])
        for ordering in ([moneyline, under], [under, moneyline]):
            actionable = _actionable_candidates(
                [(c, 0.0) for c in ordering], empty, Settings()
            )
            self.assertEqual([c.market_id for c, _ in actionable], ["ml"])

    def test_non_soccer_same_event_keeps_single_highest_bid(self):
        from polymarket_bot.race_strategies import _actionable_candidates

        a = ActionableCandidatesTests._candidate(
            "1", "Spurs vs. Knicks: O/U 196.5", "ou-196", "nba-sas-nyk", "tok-1",
            hours=1.0, bid=0.91,
        )
        b = ActionableCandidatesTests._candidate(
            "2", "Spurs vs. Knicks: O/U 198.5", "ou-198", "nba-sas-nyk", "tok-2",
            hours=1.0, bid=0.93,
        )
        other = ActionableCandidatesTests._candidate(
            "3", "Will PPI YoY be 7.4% in May?", "ppi", "ppi-may", "tok-3",
            hours=2.0, bid=0.92,
        )
        empty = Portfolio(cash=100.0, positions=[])
        actionable = _actionable_candidates(
            [(a, 0.0), (b, 0.0), (other, 0.0)], empty, Settings()
        )
        self.assertEqual(sorted(c.market_id for c, _ in actionable), ["2", "3"])

    def test_event_exposure_cap_is_one(self):
        from polymarket_bot import race_strategies

        self.assertEqual(race_strategies.EVENT_EXPOSURE_CAP, 1)

    # ── Cross-event game stacking (2026-06-11 regression) ────────────────
    # Polymarket files one game under several events: the bot put $958 on
    # Mexico–South Africa via three slugs (moneyline / -more-markets /
    # -first-to-score). One game = one bet, whatever the event slug.

    @staticmethod
    def _mexico_trio():
        def cand(mid, question, slug, event_slug, token, outcome, bid):
            return Candidate(
                market_id=mid, question=question, slug=slug,
                end_date=utc_now() + timedelta(hours=1), hours_to_close=1,
                liquidity=1500, volume=2000, outcome=outcome, price=bid,
                token_id=token, score=1,
                url=f"https://polymarket.com/event/{event_slug}",
                best_bid=bid, best_ask=bid + 0.01, tick_size=0.01,
                accepts_orders=True, event_slug=event_slug,
            )
        moneyline = cand("ml", "Will South Africa win on 2026-06-11?",
                         "will-south-africa-win-on-2026-06-11",
                         "fifwc-mex-rsa-2026-06-11", "tok-ml", "No", 0.90)
        under = cand("u45", "Mexico vs. South Africa: O/U 4.5",
                     "mexico-vs-south-africa-ou-45",
                     "fifwc-mex-rsa-2026-06-11-more-markets", "tok-u45", "Under", 0.91)
        first = cand("fts", "Mexico vs. South Africa: Neither team to score first?",
                     "mexico-vs-south-africa-first-to-score",
                     "fifwc-mex-rsa-2026-06-11-first-to-score", "tok-fts", "No", 0.94)
        return moneyline, under, first

    def test_one_game_across_three_event_slugs_keeps_single_best_bid(self):
        # One game across three event slugs collapses to ONE bet — the
        # highest bid (first-to-score @ 0.94), no under-4.5 priority.
        from polymarket_bot.race_strategies import _actionable_candidates

        moneyline, under, first = self._mexico_trio()
        empty = Portfolio(cash=1300.0, positions=[])
        actionable = _actionable_candidates(
            [(moneyline, 0.0), (under, 0.0), (first, 0.0)], empty, Settings()
        )
        self.assertEqual([c.market_id for c, _ in actionable], ["fts"])

    def test_open_position_blocks_other_markets_of_same_game(self):
        from polymarket_bot.race_strategies import _actionable_candidates

        moneyline, under, first = self._mexico_trio()
        holding_under = Portfolio(cash=900.0, positions=[{
            "status": "open",
            "token_id": "tok-u45",
            "market_id": "u45",
            "question": "Mexico vs. South Africa: O/U 4.5",
            "event_slug": "fifwc-mex-rsa-2026-06-11-more-markets",
            "stake": 372.09,
        }])
        # Top-ups disabled → even the held market drops; the moneyline and
        # first-to-score (different event slugs, same game) must drop too.
        actionable = _actionable_candidates(
            [(moneyline, 0.0), (under, 0.0), (first, 0.0)],
            holding_under,
            Settings(race_stake_pct=0.0),
        )
        self.assertEqual(actionable, [])

    def test_different_games_unaffected_by_game_keys(self):
        from polymarket_bot.race_strategies import _actionable_candidates

        def cand(mid, question, event_slug, token):
            return Candidate(
                market_id=mid, question=question, slug=mid,
                end_date=utc_now() + timedelta(hours=1), hours_to_close=1,
                liquidity=1500, volume=2000, outcome="Under", price=0.9,
                token_id=token, score=1,
                url=f"https://polymarket.com/event/{event_slug}",
                best_bid=0.9, best_ask=0.91, tick_size=0.01,
                accepts_orders=True, event_slug=event_slug,
            )
        a = cand("1", "France vs. Germany: O/U 4.5", "fifwc-fra-ger-2026-06-11-more-markets", "t1")
        b = cand("2", "Brazil vs. Chile: O/U 4.5", "fifwc-bra-chi-2026-06-11-more-markets", "t2")
        empty = Portfolio(cash=900.0, positions=[])
        actionable = _actionable_candidates([(a, 0.0), (b, 0.0)], empty, Settings())
        self.assertEqual(sorted(c.market_id for c, _ in actionable), ["1", "2"])


class DoubleDownTests(unittest.TestCase):
    """User rule 2026-06-14: when ANY held position's live ask dips a bit
    below entry (e.g. 0.96 → 0.89), double down (buy more) — once, bounded by
    the 10% cap. Generalized from soccer Under-4.5 to every grinder market."""

    def _under45(self, ask):
        # A tradeable favorite (moneyline) — O/U 4.5 is banned since 2026-06-14,
        # and the double-down skips excluded markets, so use a non-banned one.
        return Candidate(
            market_id="u45", question="Will PSG win on 2026-06-14?",
            slug="psg-win-2026-06-14", end_date=utc_now() + timedelta(hours=2),
            hours_to_close=2, liquidity=1500, volume=2000, outcome="Yes",
            price=ask, token_id="tok-u45", score=1,
            url="https://polymarket.com/event/fc-nantes-vs-psg",
            best_bid=round(ask - 0.01, 2), best_ask=ask, tick_size=0.01,
            accepts_orders=True, event_slug="fc-nantes-vs-psg",
        )

    def _settings(self, **over):
        base = dict(dry_run=True, min_order_shares=5.0, race_stake_pct=0.20,
                    race_min_price=0.85, race_max_price=0.97, race_cash_floor_pct=0.0,
                    race_double_down_enabled=True, quiet=True)
        base.update(over)
        return Settings(**base)

    def test_dipped_position_doubles_down_once(self):
        from polymarket_bot.race_strategies import _execute_double_downs

        entry_cand = self._under45(0.93)  # entry ask
        portfolio = Portfolio(cash=1000.0, positions=[])
        pos = portfolio.record_live_position(entry_cand, 40.0, entry_price=0.93)
        pos["strategy"] = "grinder"

        client = build_client(Settings(dry_run=True))
        # Live ask dipped to 0.90 (3¢ below entry) → double down.
        dipped = self._under45(0.90)
        stake_before = float(pos["stake"])
        outs = _execute_double_downs(client, self._settings(), portfolio, [dipped], "grinder")
        self.assertEqual(len(outs), 1)
        self.assertEqual(outs[0]["reason"], "dip_double_down")
        self.assertTrue(pos.get("doubled_down"))
        self.assertGreater(float(pos["stake"]), stake_before)

        # Second run: already doubled → no-op.
        outs2 = _execute_double_downs(client, self._settings(), portfolio, [dipped], "grinder")
        self.assertEqual(outs2, [])

    def test_no_double_down_above_entry_or_below_price_floor(self):
        from polymarket_bot.race_strategies import _execute_double_downs

        client = build_client(Settings(dry_run=True))
        # above entry (no dip), <1¢ dip, and below the 0.60 alive-floor.
        for ask in (0.94, 0.925, 0.55):
            entry_cand = self._under45(0.93)
            portfolio = Portfolio(cash=1000.0, positions=[])
            pos = portfolio.record_live_position(entry_cand, 40.0, entry_price=0.93)
            pos["strategy"] = "grinder"
            outs = _execute_double_downs(
                client, self._settings(), portfolio, [self._under45(ask)], "grinder")
            self.assertEqual(outs, [], f"ask={ask}")
            self.assertFalse(pos.get("doubled_down"), f"ask={ask}")

    def test_double_down_on_big_dip_while_still_above_060(self):
        # User 2026-06-14 (Sweden-Tunisia Under): double down while the cote
        # is still above 0.6, even on a big dip (0.93 → 0.70). The old 8¢
        # max-dip cap is gone — only the 0.60 alive-floor gates the size.
        from polymarket_bot.race_strategies import _execute_double_downs

        entry_cand = self._under45(0.93)
        portfolio = Portfolio(cash=1000.0, positions=[])
        pos = portfolio.record_live_position(entry_cand, 40.0, entry_price=0.93)
        pos["strategy"] = "grinder"
        outs = _execute_double_downs(
            build_client(Settings(dry_run=True)),
            self._settings(), portfolio, [self._under45(0.70)], "grinder")
        self.assertEqual(len(outs), 1)
        self.assertEqual(outs[0]["reason"], "dip_double_down")
        self.assertTrue(pos.get("doubled_down"))

    def test_dipped_non_soccer_also_doubles_down(self):
        # User 2026-06-14: the double-down applies to ANY dipped favorite, not
        # only soccer Under-4.5 — a moneyline that slid 0.96 → 0.89 qualifies.
        from polymarket_bot.race_strategies import _execute_double_downs

        def ml(ask):
            return Candidate(
                market_id="ml", question="Will Nantes win on 2026-06-14?",
                slug="nantes-win", end_date=utc_now() + timedelta(hours=2),
                hours_to_close=2, liquidity=1500, volume=2000, outcome="Yes",
                price=ask, token_id="tok-ml", score=1,
                url="https://polymarket.com/event/fc-nantes-vs-psg",
                best_bid=round(ask - 0.01, 2), best_ask=ask, tick_size=0.01,
                accepts_orders=True, event_slug="fc-nantes-vs-psg",
            )
        portfolio = Portfolio(cash=1000.0, positions=[])
        pos = portfolio.record_live_position(ml(0.96), 40.0, entry_price=0.96)
        pos["strategy"] = "grinder"
        outs = _execute_double_downs(
            client=build_client(Settings(dry_run=True)),
            settings=self._settings(), portfolio=portfolio, pool=[ml(0.89)],
            strategy_name="grinder")
        self.assertEqual(len(outs), 1)
        self.assertEqual(outs[0]["reason"], "dip_double_down")
        self.assertTrue(pos.get("doubled_down"))

    def test_disabled_by_default(self):
        from polymarket_bot.race_strategies import _execute_double_downs

        entry_cand = self._under45(0.93)
        portfolio = Portfolio(cash=1000.0, positions=[])
        pos = portfolio.record_live_position(entry_cand, 40.0, entry_price=0.93)
        pos["strategy"] = "grinder"
        outs = _execute_double_downs(
            build_client(Settings(dry_run=True)),
            self._settings(race_double_down_enabled=False),
            portfolio, [self._under45(0.90)], "grinder")
        self.assertEqual(outs, [])


class DynamicStakeTargetTests(unittest.TestCase):
    """Opportunity-spread sizing (user 2026-06-10): 20% of equity hard cap
    per bet; with N actionable markets each bet targets cash/N; a slow
    market gives each bet the full cap."""

    @staticmethod
    def _settings():
        return Settings(race_stake_pct=0.20, smart_max_position_ceiling_usd=0.0)

    def test_slow_market_uses_the_full_cap(self):
        from polymarket_bot.race_strategies import _dynamic_stake_target

        target = _dynamic_stake_target(self._settings(), 1000.0, 800.0, 1, 3.0)
        self.assertAlmostEqual(target, 200.0)  # 20% of equity, not 800

    def test_busy_window_spreads_cash_across_opportunities(self):
        from polymarket_bot.race_strategies import _dynamic_stake_target

        target = _dynamic_stake_target(self._settings(), 1000.0, 800.0, 20, 3.0)
        self.assertAlmostEqual(target, 40.0)  # 800 / 20

    def test_cap_holds_even_with_few_opportunities_and_deep_cash(self):
        from polymarket_bot.race_strategies import _dynamic_stake_target

        target = _dynamic_stake_target(self._settings(), 1000.0, 800.0, 2, 3.0)
        self.assertAlmostEqual(target, 200.0)  # 800/2=400 → capped at 20%

    def test_initial_stake_pct_caps_fresh_entries_below_hard_cap(self):
        # User 2026-06-14: fresh entries target initial_stake_pct (5%) so the
        # dip double-down has headroom up to the hard race_stake_pct cap (10%).
        from polymarket_bot.race_strategies import (
            _dynamic_stake_target, _entry_cap_usd, _position_cap_usd,
        )
        s = Settings(race_stake_pct=0.10, race_initial_stake_pct=0.05,
                     smart_max_position_ceiling_usd=0.0)
        # Slow market, deep cash → entry targets the 5% INITIAL cap, not 10%.
        self.assertAlmostEqual(_dynamic_stake_target(s, 1000.0, 800.0, 1, 3.0), 50.0)
        self.assertAlmostEqual(_entry_cap_usd(s, 1000.0), 50.0)
        # The hard per-position cap (double-down ceiling) stays 10%.
        self.assertAlmostEqual(_position_cap_usd(s, 1000.0), 100.0)
        # Headroom for the double-down = 100 - 50 = 50.

        # Disabled (0 or ≥ cap) → entry targets the full cap, old behavior.
        s_off = Settings(race_stake_pct=0.10, race_initial_stake_pct=0.0,
                         smart_max_position_ceiling_usd=0.0)
        self.assertAlmostEqual(_entry_cap_usd(s_off, 1000.0), 100.0)
        self.assertAlmostEqual(_dynamic_stake_target(s_off, 1000.0, 800.0, 1, 3.0), 100.0)

    def test_near_resolution_boost_scales_share_but_never_pierces_cap(self):
        from polymarket_bot.race_strategies import _dynamic_stake_target

        boosted = _dynamic_stake_target(self._settings(), 1000.0, 800.0, 20, 0.4)
        self.assertAlmostEqual(boosted, 60.0)  # (800/20) × 1.5
        capped = _dynamic_stake_target(self._settings(), 1000.0, 800.0, 3, 0.4)
        self.assertAlmostEqual(capped, 200.0)  # (800/3)×1.5=400 → cap


class PriceMovementNeverExcludesTests(unittest.TestCase):
    """User decision 2026-06-10: markets that moved recently must stay
    tradeable — the 1h flux gates AND the day-change gates (>10% day move,
    >5% day fall) were all removed. Recently moving markets are often the
    ones converging toward resolution; if anything they are the focus.
    Pin: no oneHourPriceChange or oneDayPriceChange value may exclude a
    market."""

    @staticmethod
    def _market(one_hour=0.0, one_day=0.0):
        end = (utc_now() + timedelta(hours=2)).isoformat()
        return {
            "id": "m1",
            "question": "Will Foo win on 2026-06-11?",
            "slug": "foo-win",
            "endDate": end,
            "acceptingOrders": True,
            "liquidity": "2000",
            "volume24hr": "5000",
            "bestBid": 0.90,
            "bestAsk": 0.92,
            "orderPriceMinTickSize": 0.01,
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.91", "0.09"]',
            "clobTokenIds": '["tok-yes", "tok-no"]',
            "oneDayPriceChange": one_day,
            "oneHourPriceChange": one_hour,
        }

    @staticmethod
    def _settings():
        return Settings(
            race_min_price=0.85, race_max_price=0.97, race_max_hours=6.0,
            race_max_spread=0.04, race_min_liquidity_usd=500, race_min_volume_24h_usd=300,
        )

    def test_fast_moving_markets_stay_eligible(self):
        from polymarket_bot.race_strategies import _build_eligible_candidates

        for one_hour in (0.20, -0.20, 0.05, -0.05):
            eligible = _build_eligible_candidates([self._market(one_hour=one_hour)], self._settings())
            self.assertEqual(len(eligible), 1, f"one_hour={one_hour} must not exclude")

    def test_day_movers_and_day_fallers_stay_eligible(self):
        from polymarket_bot.race_strategies import _build_eligible_candidates

        for one_day in (0.30, -0.30, 0.10, -0.10):
            eligible = _build_eligible_candidates([self._market(one_day=one_day)], self._settings())
            self.assertEqual(len(eligible), 1, f"one_day={one_day} must not exclude")
            # Both outcomes survive — no momentum floor on either side.
            self.assertEqual(len(eligible[0:1]), 1)

    def test_movement_gate_knobs_no_longer_exist(self):
        for knob in (
            {"race_max_hour_change_pct": 0.05},
            {"race_min_outcome_momentum_1h": -0.02},
            {"race_max_day_change_pct": 0.10},
            {"race_min_outcome_momentum": -0.05},
        ):
            with self.assertRaises(TypeError):
                Settings(**knob)


class ExcludedMarketTests(unittest.TestCase):
    def test_lol_prefix_titles_are_excluded(self):
        # Regression: Polymarket titles LoL markets "LoL: A vs B - Game N
        # Winner", not "League of Legends" — the bot bought $351 of
        # FENNEL vs KT Rolster on 2026-06-10 despite the esports ban.
        self.assertTrue(is_excluded_market({
            "question": "LoL: FENNEL vs KT Rolster Challengers - Game 1 Winner",
            "slug": "lol-fennel-vs-kt-rolster-challengers-game-1-winner",
        }))

    def test_esports_keywords_excluded(self):
        for question in (
            "Counter-Strike: NaVi vs FaZe (BO3)",
            "Valorant Champions: winner of map 2?",
            "Will T1 win the League of Legends final?",
        ):
            self.assertTrue(is_excluded_market({"question": question, "slug": ""}), question)

    def test_elections_and_primaries_stay_tradeable(self):
        # User decision 2026-06-10: elections/primaries/mayoral races are a
        # liked, profitable lane (Billy Webster, Castrovillari wins) and must
        # NOT be banned — despite the postponement risk seen on the Alan
        # Wilson SC primary. Pin them as allowed so a future exclusion-list
        # edit can't silently drop them.
        for market in (
            {"question": "Will Alan Wilson win the 2026 South Carolina Governor Republican primary election?",
             "slug": "south-carolina-governor-republican-primary-winner-784"},
            {"question": "Will Billy Webster win the 2026 South Carolina Governor Democratic primary election?",
             "slug": "south-carolina-governor-democratic-primary"},
            {"question": "Will Ernesto Bello win the 2026 Castrovillari mayoral election?",
             "slug": "castrovillari-mayoral-election-winner"},
        ):
            self.assertFalse(is_excluded_market(market), market["question"])

    def test_regular_sports_not_excluded(self):
        for market in (
            {"question": "Will Nigeria win on 2026-06-10?", "slug": "fif-nga-2026-06-10"},
            {"question": "Will PPI YoY be between 7.0% and 7.9% in May?", "slug": "producer-price-index-ppi-yoy-may-2026"},
            {"question": "Will annual inflation be 4.1% in May?", "slug": "annual-inflation-may-2026"},
        ):
            self.assertFalse(is_excluded_market(market), market["question"])

    def test_stock_market_excluded(self):
        # User rule 2026-06-11: never bet on stock-market markets (the bot
        # bought "S&P 500 (SPY) closes above $725" on 2026-06-10).
        for market in (
            {"question": "S&P 500 (SPY) closes above $725 on June 10?",
             "slug": "sp-500-spy-closes-above-725-on-june-10"},
            {"question": "Google (GOOGL) closes above $200 on June 30?",
             "slug": "google-googl-closes-above-200"},
            {"question": "Will Tesla stock close above $400 this week?",
             "slug": "tesla-stock-close-above-400"},
            {"question": "Nasdaq 100 (QQQ) closes below $530 on June 12?",
             "slug": "nasdaq-100-qqq-closes-below-530"},
            {"question": "Will Apple (AAPL) hit a $5T market cap in 2026?",
             "slug": "apple-aapl-5t-market-cap-2026"},
            {"question": "Will the Dow Jones close above 50,000 in June?",
             "slug": "dow-jones-close-above-50000-june"},
            {"question": "Will Nvidia beat earnings expectations?",
             "slug": "nvidia-earnings-q2-2026"},
            # Slug-only marker (question scrubbed of keywords).
            {"question": "Closes above the strike on Friday?",
             "slug": "msft-weekly-close-above-strike"},
        ):
            self.assertTrue(is_excluded_market(market), market["question"])

    def test_stock_ticker_word_boundary_no_false_positives(self):
        # Short tickers are word-bounded: "spy" must not ban spy thrillers,
        # "meta" must not ban metal, "dax" must not ban Dexter etc.
        for market in (
            {"question": "Will the spying scandal force a minister to resign?",
             "slug": "spying-scandal-minister-resign"},
            {"question": "Will the metal band win the Eurovision final?",
             "slug": "eurovision-metal-band-final"},
            {"question": "Will France win on 2026-06-11?", "slug": "fra-win-2026-06-11"},
        ):
            self.assertFalse(is_excluded_market(market), market["question"])

    # ── Outright bans (user 2026-06-12, final): esports + stocks ──────────

    def test_esports_banned_outright_even_live(self):
        # User 2026-06-12 (final): esports banned completely — the brief
        # live-only and LoL-only re-allows from the same day are gone.
        from datetime import datetime, timezone

        now = datetime(2026, 6, 12, 18, 0, tzinfo=timezone.utc)
        base = {"question": "LoL: Solary vs Eintracht Spandau - Game 1 Winner",
                "slug": "lol-solary-vs-eintracht-spandau-game-1-winner"}
        live = dict(base, gameStartTime="2026-06-12T17:00:00+00:00")     # 1h in
        pregame = dict(base, gameStartTime="2026-06-12T19:00:00+00:00")  # +1h
        self.assertTrue(is_excluded_market(live, now=now))
        self.assertTrue(is_excluded_market(pregame, now=now))
        self.assertTrue(is_excluded_market(base, now=now))  # unknown start

    def test_esports_every_title_banned(self):
        # Counter-Strike, Valorant, Mobile Legends, LoL — all banned, live
        # or not.
        from datetime import datetime, timezone

        now = datetime(2026, 6, 12, 18, 0, tzinfo=timezone.utc)
        live_start = "2026-06-12T17:00:00+00:00"
        for market in (
            {"question": "Counter-Strike: Marsborne vs F5 Esports (BO3) - Playoffs",
             "slug": "cs-marsborne-vs-f5", "gameStartTime": live_start},
            {"question": "Valorant Champions: winner of map 2?",
             "slug": "valorant-champions-map-2", "gameStartTime": live_start},
            {"question": "Team A vs Team B (BO3) - Grand Final",
             "slug": "team-a-vs-team-b-esports", "gameStartTime": live_start},
            {"question": "Mobile Legends: ONIC vs Blacklist - Game 3 Winner",
             "slug": "mobile-legends-onic-vs-blacklist-game-3",
             "gameStartTime": live_start},
            {"question": "LoL: T1 vs Gen.G - Game 2 Winner",
             "slug": "lol-t1-vs-geng-game-2", "gameStartTime": live_start},
        ):
            self.assertTrue(is_excluded_market(market, now=now), market["question"])

    def test_league_of_ireland_banned(self):
        # User rule 2026-06-12: no League of Ireland (Premier Division
        # Ireland) bets. The question carries no league marker — the "irl1-"
        # slug prefix is the identifier (both real markets pinned).
        for market in (
            {"question": "Derry City FC vs. Bohemians Dublin FC: O/U 4.5",
             "slug": "irl1-der-boh-2026-06-12-total-4pt5"},
            {"question": "Shelbourne FC vs. Shamrock Rovers: O/U 4.5",
             "slug": "irl1-she-sha-2026-06-12-total-4pt5"},
            {"question": "Will Shelbourne FC win on 2026-06-12?",
             "slug": "irl1-she-sha-2026-06-12"},
        ):
            self.assertTrue(is_excluded_market(market), market["slug"])
        # Other leagues' moneylines stay tradeable (O/U 4.5 itself is banned
        # everywhere since 2026-06-14, regardless of league).
        self.assertFalse(is_excluded_market(
            {"question": "Will Málaga CF win on 2026-06-12?",
             "slug": "esp2-mal-lpa-2026-06-12"}))

    def test_ou_45_banned_everywhere(self):
        # Data-driven ban 2026-06-14: O/U 4.5 Unders were 80% of all losses
        # (3 worst trades ever). The last open O/U line is now closed.
        for market in (
            {"question": "Sweden vs. Tunisia: O/U 4.5", "slug": "fifwc-swe-tun-ou-45"},
            {"question": "Málaga CF vs. UD Las Palmas: O/U 4.5",
             "slug": "esp2-mal-lpa-2026-06-12-total-4pt5"},
            {"question": "Team A vs Team B: Over/Under 4.5", "slug": "ab-ou-4pt5"},
        ):
            self.assertTrue(is_excluded_market(market), market["question"])
        # The moneyline of the same game stays tradeable.
        self.assertFalse(is_excluded_market(
            {"question": "Will Sweden win on 2026-06-14?", "slug": "fifwc-swe-tun-win"}))

    def test_game_handicap_markets_banned_even_for_live_lol(self):
        # "Game Handicap: HLE (-2.5) vs T1 (+2.5)" slipped past the "Spread:"
        # pattern and was bought pre-game at 0.889 (2026-06-12). Handicaps
        # are spread markets — banned outright, even for a live LoL game.
        from datetime import datetime, timezone

        now = datetime(2026, 6, 12, 18, 0, tzinfo=timezone.utc)
        market = {"question": "Game Handicap: HLE (-2.5) vs T1 (+2.5)",
                  "slug": "lol-game-handicap-hle-t1",
                  "gameStartTime": "2026-06-12T17:00:00+00:00"}
        self.assertTrue(is_excluded_market(market, now=now))

    def test_fast_lane_markets_never_reach_eligibility(self):
        # Esports and stocks are banned at is_excluded_market level — they
        # can never become eligible candidates, whatever the ask. The esports
        # lane floor (ESPORTS_MIN_ASK) is a dead-letter backstop.
        from polymarket_bot.race_strategies import _build_eligible_candidates

        def lol_market(ask):
            start = (utc_now() - timedelta(hours=1)).isoformat()
            end = (utc_now() + timedelta(hours=2)).isoformat()
            return {
                "id": f"lol-{ask}", "question": "LoL: T1 vs Gen.G - Game 2 Winner",
                "slug": f"lol-t1-vs-geng-{ask}", "endDate": end,
                "gameStartTime": start, "acceptingOrders": True,
                "liquidity": 1500, "volume24hr": 2000,
                "bestBid": round(ask - 0.02, 2), "bestAsk": ask,
                "orderPriceMinTickSize": 0.01,
                "outcomes": '["T1", "Gen.G"]',
                "outcomePrices": f'["{ask}", "{round(1 - ask, 2)}"]',
                "clobTokenIds": '["tok-a", "tok-b"]',
            }

        settings = Settings(race_min_price=0.85, race_max_price=0.97,
                            race_max_spread=0.04, race_max_hours=4.0)
        for ask in (0.90, 0.93, 0.96):
            self.assertEqual(_build_eligible_candidates([lol_market(ask)], settings), [])

        def normal_market(ask):
            # A genuine non-lane market — first-to-score, NOT a "Will X win"
            # moneyline (those now carry their own 0.92 floor, 2026-06-17) and
            # NOT a stock (banned). Keeps the global 0.85 floor.
            end = (utc_now() + timedelta(hours=2)).isoformat()
            return {
                "id": f"fts-{ask}", "question": "France vs. Spain: Neither team to score first?",
                "slug": f"fra-esp-{ask}", "endDate": end, "acceptingOrders": True,
                "liquidity": 1500, "volume24hr": 2000,
                "bestBid": round(ask - 0.02, 2), "bestAsk": ask,
                "orderPriceMinTickSize": 0.01,
                "outcomes": '["Yes", "No"]',
                "outcomePrices": f'["{ask}", "{round(1 - ask, 2)}"]',
                "clobTokenIds": '["tok-a", "tok-b"]',
            }

        # Non-fast-lane markets keep the global 0.85 floor and stay eligible.
        normal = _build_eligible_candidates([normal_market(0.86)], settings)
        self.assertEqual([c.best_ask for c, _ in normal], [0.86])

        # Stocks are banned outright (re-banned 2026-06-12) — classification
        # still works and feeds the ban.
        from polymarket_bot.models import is_stock_text
        self.assertTrue(is_stock_text("Will Apple (AAPL) close above $290 on June 12?", ""))

    def test_stocks_banned_outright_even_in_session(self):
        # Re-banned 2026-06-12 (user) after a one-day in-session experiment:
        # no session window, ever — even mid-session for a same-day close.
        from datetime import datetime, timezone

        market = {"question": "Apple (AAPL) closes above $290 on June 10?",
                  "slug": "apple-aapl-closes-above-290-june-10",
                  "endDate": "2026-06-11T00:00:00+00:00"}
        in_session = datetime(2026, 6, 10, 18, 0, tzinfo=timezone.utc)
        after_hours = datetime(2026, 6, 10, 21, 0, tzinfo=timezone.utc)
        self.assertTrue(is_excluded_market(market, now=in_session))
        self.assertTrue(is_excluded_market(market, now=after_hours))

    def test_youtube_view_count_markets_banned(self):
        # User rule 2026-06-14 (lost a MrBeast view-count bet).
        for market in (
            {"question": "Will MrBeast's new video reach 100 million views by June 20?",
             "slug": "mrbeast-video-100m-views-june-20"},
            {"question": "How many views will the trailer get in 24h?",
             "slug": "trailer-views-24h"},
            {"question": "Will the YouTube debate stream pass 5M views?",
             "slug": "youtube-debate-stream-5m"},
        ):
            self.assertTrue(is_excluded_market(market), market["question"])

    def test_entertainment_markets_banned(self):
        # User 2026-06-14: block "Divertissement" — awards, box office, charts,
        # streaming, social metrics. No convergence edge.
        for market in (
            {"question": "Will the movie gross $200M opening weekend?", "slug": "movie-200m"},
            {"question": "Who wins Best Picture at the Academy Awards?", "slug": "best-picture"},
            {"question": "Will the album hit #1 on Billboard?", "slug": "album-billboard-1"},
            {"question": "Will the song pass 1B Spotify streams?", "slug": "spotify-1b"},
            {"question": "Will the creator reach 10M subscribers by July?", "slug": "creator-10m-subs"},
            {"question": "Will the film win the Palme d'Or?", "slug": "palme-dor"},
            {"question": "Will the Netflix show be #1 this week?", "slug": "netflix-1"},
        ):
            self.assertTrue(is_excluded_market(market), market["question"])

    def test_entertainment_ban_keeps_winners_tradeable(self):
        # The audit's winning lanes must NOT be caught: AI-model markets,
        # geopolitics, golf, WNBA, soccer moneylines/specials.
        for market in (
            {"question": "Will claude-opus-4-6 be the best AI model on June 13?", "slug": "best-ai-model"},
            {"question": "Will Sam Burns win the 2026 RBC Canadian Open?", "slug": "rbc-canadian-open"},
            {"question": "Will Israel close its airspace by June 12?", "slug": "israel-airspace"},
            {"question": "Washington Mystics vs. New York Liberty", "slug": "wnba-mys-nyl"},
            {"question": "Will Sweden win on 2026-06-14?", "slug": "swe-win"},
        ):
            self.assertFalse(is_excluded_market(market), market["question"])

    def test_views_word_boundary_no_false_positives(self):
        for market in (
            {"question": "Will the candidate get positive reviews after the debate?",
             "slug": "candidate-reviews-debate"},
            {"question": "Will the candidate do 3 interviews before the vote?",
             "slug": "candidate-interviews"},
        ):
            self.assertFalse(is_excluded_market(market), market["question"])

    def test_tweet_count_markets_banned_outright(self):
        # User rule 2026-06-12 — the bot bought "Will Elon Musk post 240-259
        # tweets from June 5 to June 12?" (week-long count, no convergence).
        for market in (
            {"question": "Will Elon Musk post 240-259 tweets from June 5 to June 12, 2026?",
             "slug": "elon-musk-of-tweets-june-5-june-12"},
            {"question": "Will Trump tweet about the Fed this week?",
             "slug": "trump-tweet-about-fed"},
        ):
            self.assertTrue(is_excluded_market(market), market["question"])

    def test_fed_rate_decision_markets_banned_outright(self):
        # User rule 2026-06-17 — the bot kept re-buying "Fed rate cut by
        # September 2026 meeting?" (resolves months out but shows a near-term
        # Gamma endDate, so it slipped through the 4h window). "Too far away."
        for market in (
            {"question": "Fed rate cut by September 2026 meeting?",
             "slug": "fed-rate-cut-september-2026"},
            {"question": "Will the Fed hike rates in July?", "slug": "fed-rate-hike-july"},
            {"question": "FOMC interest rate decision: 25 basis points cut?",
             "slug": "fomc-25bps-cut"},
        ):
            self.assertTrue(is_excluded_market(market), market["question"])
        # Must not collide with non-monetary "rate" markets.
        for ok in (
            {"question": "Will Trump approval rating stay above 45%?", "slug": "trump-approval"},
            {"question": "Will Brazil win at this rate of scoring?", "slug": "brazil-win"},
        ):
            self.assertFalse(is_excluded_market(ok), ok["question"])

    def test_weekly_and_touch_stock_markets_banned_even_in_session(self):
        # The bot bought "Will Airbnb, Inc. (ABNB) hit (LOW) $124 Week of
        # June 8 2026?" on 2026-06-11 — ABNB wasn't in the ticker list and
        # weekly/touch markets have no end-of-session convergence. Banned
        # outright, session or not.
        from datetime import datetime, timezone

        in_session = datetime(2026, 6, 10, 18, 0, tzinfo=timezone.utc)  # Wed 14:00 ET
        abnb = {"question": "Will Airbnb, Inc. (ABNB) hit (LOW) $124 Week of June 8 2026?",
                "slug": "will-abnb-hit-week-of-june-8-2026",
                "endDate": "2026-06-10T23:00:00+00:00"}
        self.assertTrue(is_excluded_market(abnb, now=in_session))

    def test_generic_paren_ticker_with_dollar_is_classified_stock(self):
        # Tickers not in the enumerated list are caught by "(TICKER) … $"
        # — and stocks are banned outright.
        from datetime import datetime, timezone

        market = {"question": "Will Snowflake (SNOW) close above $310 on June 10?",
                  "slug": "snowflake-close-above-310-june-10",
                  "endDate": "2026-06-11T00:00:00+00:00"}
        in_session = datetime(2026, 6, 10, 18, 0, tzinfo=timezone.utc)
        self.assertTrue(is_excluded_market(market, now=in_session))
        # No dollar sign → parenthesized acronyms alone don't classify:
        politics = {"question": "Will the (GOP) keep the House majority?",
                    "slug": "gop-house-majority-2026"}
        self.assertFalse(is_excluded_market(politics, now=in_session))


class SoccerMoneylineSLGateTests(unittest.TestCase):
    """SL lane gate (user 2026-06-16): a soccer club like América FC must be
    covered regardless of league; elections/awards must NOT stop out."""

    def _gate(self, question, outcome="No", slug=""):
        from polymarket_bot.race_strategies import _is_soccer_moneyline_position
        return _is_soccer_moneyline_position(
            {"question": question, "outcome": outcome, "slug": slug, "event_slug": slug}
        )

    def test_soccer_club_covered_regardless_of_league_slug(self):
        # The América FC case: a "Will <club> win on <date>?" Yes/No with a
        # slug that has NO enumerated league keyword must still get the SL.
        self.assertTrue(self._gate(
            "Will América FC win on 2026-06-16?", "No",
            "will-america-fc-win-on-2026-06-16"))
        self.assertTrue(self._gate("Will France win on 2026-06-16?", "Yes", "fifwc-fra"))

    def test_elections_and_awards_never_stop_out(self):
        for q, slug in (
            ("Will Donald Trump win on 2026-11-03?", "us-presidential-election-2026"),
            ("Will the incumbent win on 2026-06-16?", "governor-primary-runoff"),
            ("Will the film win on 2026-06-16?", "academy-award-best-picture"),
        ):
            self.assertFalse(self._gate(q, "Yes", slug), q)

    def test_non_moneyline_and_non_yesno_excluded(self):
        # O/U totals and Over/Under outcomes never match the moneyline regex.
        self.assertFalse(self._gate("Spurs vs. Knicks: O/U 196.5", "Under", "nba-ou"))
        self.assertFalse(self._gate("Will América FC win on 2026-06-16?", "Maybe", "x"))


class SoccerMoneylineEntryFloorTests(unittest.TestCase):
    """Entry floor (user 2026-06-17): soccer/sport moneylines need ask ≥ 0.92 —
    every moneyline loss ever entered at ≤ 0.90; 0.90+ has zero losses."""

    def _moneyline_market(self, ask, question="Will Difaâ Hassani El Jadida win on 2026-06-17?",
                          slug="mar1-mad-dhe-2026-06-17"):
        end = (utc_now() + timedelta(hours=2)).isoformat()
        return {
            "id": f"ml-{ask}", "question": question, "slug": slug,
            "endDate": end, "gameStartTime": (utc_now() + timedelta(hours=1)).isoformat(),
            "acceptingOrders": True, "liquidity": 1500, "volume24hr": 2000,
            "bestBid": round(ask - 0.02, 2), "bestAsk": ask,
            "orderPriceMinTickSize": 0.01,
            "outcomes": '["Yes", "No"]',
            "outcomePrices": f'["{ask}", "{round(1 - ask, 2)}"]',
            "clobTokenIds": '["tok-a", "tok-b"]',
        }

    def _settings(self):
        return Settings(race_min_price=0.85, race_max_price=0.97,
                        race_max_spread=0.04, race_max_hours=4.0)

    def _build(self, market):
        from polymarket_bot.race_strategies import _build_eligible_candidates
        return _build_eligible_candidates([market], self._settings())

    def test_moneyline_below_092_excluded(self):
        self.assertEqual(self._build(self._moneyline_market(0.89)), [])
        self.assertEqual(self._build(self._moneyline_market(0.90)), [])

    def test_moneyline_at_or_above_092_allowed(self):
        got = self._build(self._moneyline_market(0.93))
        self.assertEqual([c.best_ask for c, _ in got], [0.93])

    def test_non_moneyline_keeps_global_floor(self):
        # A first-to-score market at 0.88 stays eligible (global 0.85 floor).
        got = self._build(self._moneyline_market(
            0.88, question="Portugal vs. DR Congo: Neither team to score first?",
            slug="fifwc-prt-cdr-2026-06-17-first-to-score"))
        self.assertEqual([c.best_ask for c, _ in got], [0.88])


class SoccerMoneylineAntiGapSLTests(unittest.TestCase):
    """Anti-gap guard (user 2026-06-17): the confirmed SL must HOLD, not dump,
    when the live bid has gapped far below the -30% level. Difaâ "No" went
    0.89 → 0.02 (sold by the SL) → resolved 1.0 — a winner booked as a loss."""

    def _plan(self, decision_bid, entry=0.8949, confirm_count=2):
        from polymarket_bot.race_strategies import _simple_exit_plan
        s = Settings(race_sl_pct=0.30, race_sl_confirm_ticks=3,
                     race_sl_min_age_minutes=5, race_sl_min_exit_price=0.50,
                     race_tp_pct=1.0)
        pos = {
            "shares": 24.0, "entry_price": entry, "outcome": "No",
            "question": "Will Difaâ Hassani El Jadida win on 2026-06-17?",
            "slug": "mar1-mad-dhe-2026-06-17",
            "opened_at": (utc_now() - timedelta(minutes=30)).isoformat(),
            "sl_confirm_count": confirm_count,
        }
        pnl = (decision_bid - entry) / entry
        return _simple_exit_plan(pos, pnl, s, decision_bid=decision_bid), pos

    def test_gap_below_floor_holds(self):
        plan, pos = self._plan(0.02)
        self.assertIsNone(plan)                       # not sold
        self.assertEqual(pos["sl_confirm_count"], 0)  # streak reset

    def test_orderly_decline_still_stops_out(self):
        # bid 0.55 for a 0.8949 entry = -38.5%, above the 0.50 floor → SL fires.
        plan, _ = self._plan(0.55)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["reason"], "race_stop_loss_confirmed")


if __name__ == "__main__":
    unittest.main()
