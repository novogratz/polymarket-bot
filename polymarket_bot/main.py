from __future__ import annotations

import argparse
import json
import time

from dataclasses import replace
from datetime import timedelta

from .bitcoin import CoinbaseBtcClient, choose_btc_edge_trade
from .config import Settings
from .dashboard import serve
from .gamma import GammaClient
from .portfolio import Portfolio
from .models import utc_now
from .portfolio import paper_tick
from .smart_money import DataApiClient, analyze_smart_money, _top_traders
from .trading import build_client, choose_trade, execute_live_trade
from .strategy import rank_markets


def load_candidates(settings: Settings):
    client = GammaClient(settings.gamma_base_url)
    now = utc_now()
    markets = client.get_markets(
        limit=settings.scan_limit,
        end_date_min=now,
        end_date_max=now + timedelta(hours=settings.soon_hours),
    )
    return rank_markets(markets, settings)


def load_smart_candidates(settings: Settings):
    client = GammaClient(settings.gamma_base_url)
    now = utc_now()
    horizon = now + timedelta(hours=settings.smart_soon_hours)
    batches = [
        client.get_markets(
            limit=settings.smart_scan_limit,
            end_date_min=now,
            end_date_max=horizon,
        ),
        client.get_markets(
            limit=settings.smart_scan_limit,
            order="volume",
            ascending=False,
            end_date_min=now,
            end_date_max=horizon,
        ),
    ]
    markets_by_id = {
        str(market.get("id") or market.get("conditionId") or index): market
        for index, batch in enumerate(batches)
        for market in batch
    }
    smart_settings = replace(
        settings,
        scan_limit=settings.smart_scan_limit,
        soon_hours=settings.smart_soon_hours,
    )
    return rank_markets(list(markets_by_id.values()), smart_settings)


def scan(settings: Settings) -> list[dict[str, object]]:
    return [candidate.to_dict() for candidate in load_candidates(settings)]


def reset_ledger(settings: Settings) -> dict[str, object]:
    cash = settings.paper_balance_usd
    source = "paper_balance"
    if settings.private_key and settings.api_key and settings.api_secret and settings.api_passphrase:
        client = build_client(settings)
        live_cash = client.live_available_balance()
        if live_cash > 0:
            cash = round(live_cash, 2)
            source = "live_clob"
    portfolio = Portfolio(cash=cash, positions=[])
    portfolio.save(settings.state_path)
    return {
        "reset": True,
        "balance_source": source,
        "summary": portfolio.summary(),
    }


def bootstrap_creds(settings: Settings) -> dict[str, str]:
    client = build_client(settings)
    creds = client.derive_or_create_api_creds()
    return creds.to_dict()


def require_saved_api_creds(settings: Settings) -> None:
    if settings.api_key and settings.api_secret and settings.api_passphrase:
        return
    if settings.relayer_api_key or settings.relayer_api_key_address:
        raise RuntimeError(
            "Relayer credentials are configured, but this bot's live order path needs CLOB credentials: "
            "POLYMARKET_API_KEY, POLYMARKET_API_SECRET, and POLYMARKET_API_PASSPHRASE. "
            "RELAYER_API_KEY and RELAYER_API_KEY_ADDRESS are not enough for this CLOB order flow."
        )
    raise RuntimeError(
        "Missing POLYMARKET_API_KEY, POLYMARKET_API_SECRET, and POLYMARKET_API_PASSPHRASE in .env. "
        "The bot will not call /auth/api-key during autonomous trading because Cloudflare is blocking "
        "credential bootstrap from this IP. Add saved CLOB API credentials, then run auto-loop again."
    )


def btc_edge_once(settings: Settings) -> dict[str, object]:
    candidates = load_candidates(settings)
    btc_model = CoinbaseBtcClient().model(settings)
    portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)
    portfolio.mark_to_market(candidates)

    eligible_candidates = [
        candidate
        for candidate in candidates
        if candidate.token_id
        and candidate.accepts_orders
        and candidate.best_ask is not None
        and candidate.best_bid is not None
        and candidate.tick_size is not None
        and not portfolio.has_open_position(candidate.market_id)
    ]
    signal = choose_btc_edge_trade(eligible_candidates, settings, btc_model)
    if signal is None:
        return {
            "trade": None,
            "model": {
                "spot": btc_model.spot,
                "annual_volatility": btc_model.annual_volatility,
                "fetched_at": btc_model.fetched_at.isoformat(),
            },
            "summary": portfolio.summary(),
        }

    signal_payload = signal.to_dict()
    require_saved_api_creds(settings)
    client = build_client(settings)
    result = execute_live_trade(
        client,
        settings,
        signal.candidate,
        portfolio,
        min_trade_usd=settings.btc_min_trade_usd,
        max_trade_usd=settings.btc_max_trade_usd,
        strategy="btc_edge",
        signal=signal_payload,
    )
    portfolio.save(settings.state_path)
    return {
        "trade": {
            "strategy": "btc_edge",
            "signal": signal_payload,
            "order": result.order,
            "response": result.response,
        },
        "summary": portfolio.summary(),
    }


def smart_money_once(settings: Settings) -> dict[str, object]:
    candidates = load_smart_candidates(settings)
    portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)
    portfolio.mark_to_market(candidates)
    open_count = portfolio.summary()["open_positions"]

    # 1. WHALE EXIT: Check if we should close any existing positions
    client = build_client(settings)
    whale_exit_report = []
    if portfolio.positions:
        # Get latest smart money trades to see who is still in
        data_client = DataApiClient(settings.data_api_base_url)
        traders = _top_traders(data_client, settings)
        # Look back 2 hours for exits
        exit_lookback = int(time.time()) - (120 * 60)
        
        for position in portfolio.positions:
            if position.get("status") != "open" or not position.get("live"):
                continue
            
            # If this trade was from smart money, check if they are still buying/holding
            signal = position.get("signal")
            if not signal or "wallets" not in signal:
                continue
                
            original_wallets = set(signal["wallets"])
            # Check if any of the original wallets have bought again recently
            # If they haven't bought in the last 2 hours, and we find NO recent buys for this token, we exit
            recent_trades = []
            for wallet in original_wallets:
                recent_trades.extend(data_client.trades(user=wallet, start=exit_lookback))
            
            still_holding = any(t.asset == position.get("token_id") for t in recent_trades)
            if not still_holding and original_wallets:
                # WHALE EXIT TRIGGERED
                print(f"\n🐋 WHALE EXIT: Smart money left '{position['question']}'. Selling...\n")
                # In a real bot, we'd call sdk_client.create_and_post_order for a SELL side
                # For now, we mark as closed to free up the 'exposure' logic
                position["status"] = "closed"
                position["closed_at"] = utc_now().isoformat()
                whale_exit_report.append(position["question"])

    # 2. CATEGORY DIVERSIFICATION: Count open categories
    open_categories: dict[str, int] = {}
    for pos in portfolio.positions:
        if pos.get("status") == "open":
            cat = pos.get("signal", {}).get("category", "OTHER")
            open_categories[cat] = open_categories.get(cat, 0) + 1

    eligible_candidates = [
        candidate
        for candidate in candidates
        if candidate.token_id
        and candidate.accepts_orders
        and candidate.best_ask is not None
        and candidate.best_bid is not None
        and candidate.tick_size is not None
        and not portfolio.has_open_position(candidate.market_id)
    ]

    report = analyze_smart_money(eligible_candidates, settings)
    signal = report.selected
    strategy = "smart_money"
    if signal is None and open_count < settings.min_open_positions:
        fallback_settings = replace(
            settings,
            smart_min_consensus=max(2, settings.smart_fallback_consensus),
        )
        fallback_report = analyze_smart_money(eligible_candidates, fallback_settings)
        if fallback_report.selected is not None:
            report = fallback_report
            signal = fallback_report.selected
            strategy = "smart_money_starter"

    signal_payload = signal.to_dict() if signal else None
    require_saved_api_creds(settings)
    client = build_client(settings)

    # Try to take the smart money trade
    trade_executed = False
    if signal:
        # Gracefully wait if out of funds
        live_cash = client.live_available_balance()
        if live_cash < 1.0:
            portfolio.save(settings.state_path)
            return {
                "trade": None,
                "strategy": strategy,
                "status": "waiting_for_funds",
                "available_cash": live_cash,
                "whale_exits": whale_exit_report,
                "category_summary": open_categories,
                "scan_report": report.to_dict(),
                "summary": portfolio.summary(),
            }

        try:
            result = execute_live_trade(
                client,
                settings,
                signal.candidate,
                portfolio,
                min_trade_usd=1.0,
                max_trade_usd=settings.starter_trade_usd if strategy == "smart_money_starter" else settings.smart_max_trade_usd,
                strategy=strategy,
                signal=signal_payload,
            )
            trade_executed = True
        except ValueError as e:
            if "Anti-pump" in str(e):
                print(f"⚠️  Skipping pumped signal: {str(e)}")
            else:
                raise e

    if trade_executed:
        portfolio.save(settings.state_path)
        return {
            "trade": {
                "strategy": strategy,
                "signal": signal_payload,
                "order": result.order,
                "response": result.response,
            },
            "whale_exits": whale_exit_report,
            "category_summary": open_categories,
            "scan_report": report.to_dict(),
            "summary": portfolio.summary(),
        }

    portfolio.save(settings.state_path)
    return {
        "trade": None,
        "strategy": "smart_money",
        "whale_exits": whale_exit_report,
        "category_summary": open_categories,
        "scan_report": report.to_dict(),
        "summary": portfolio.summary(),
    }


def strategy_loop(settings: Settings, strategy_name: str, tick_fn) -> None:
    tick = 0
    while settings.auto_max_ticks <= 0 or tick < settings.auto_max_ticks:
        tick += 1
        started_at = utc_now()
        try:
            result: dict[str, object] = {
                "tick": tick,
                "strategy": strategy_name,
                "started_at": started_at.isoformat(),
                "result": tick_fn(settings),
            }
        except Exception as exc:
            result = {
                "tick": tick,
                "strategy": strategy_name,
                "started_at": started_at.isoformat(),
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            }
        print(json.dumps(result, indent=2), flush=True)
        if settings.auto_max_ticks > 0 and tick >= settings.auto_max_ticks:
            break
        time.sleep(settings.auto_interval_seconds)


def btc_edge_loop(settings: Settings) -> None:
    strategy_loop(settings, "btc_edge", btc_edge_once)


def smart_money_loop(settings: Settings) -> None:
    strategy_loop(settings, "smart_money", smart_money_once)


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket scanner, paper dashboard, and live trader")
    parser.add_argument(
        "command",
        choices=[
            "scan",
            "paper-tick",
            "trade-once",
            "btc-edge-once",
            "btc-edge-loop",
            "smart-money-once",
            "smart-money-loop",
            "auto-loop",
            "bootstrap-creds",
            "reset-ledger",
            "dashboard",
        ],
    )
    parser.add_argument("--limit", type=int, default=20, help="Rows to print for scan/paper-tick")
    args = parser.parse_args()

    settings = Settings()
    if args.command == "scan":
        print(json.dumps(scan(settings)[: args.limit], indent=2))
    elif args.command == "paper-tick":
        candidates = load_candidates(settings)
        portfolio, opened = paper_tick(candidates, settings)
        print(json.dumps({"opened": opened, "summary": portfolio.summary()}, indent=2))
    elif args.command == "bootstrap-creds":
        print(json.dumps(bootstrap_creds(settings), indent=2))
    elif args.command == "reset-ledger":
        print(json.dumps(reset_ledger(settings), indent=2))
    elif args.command == "trade-once":
        if not settings.live_trading_enabled:
            raise SystemExit("Live trading is disabled. Set POLYMARKET_ENABLE_LIVE_TRADING=1 to proceed.")
        candidates = load_candidates(settings)
        portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)
        portfolio.mark_to_market(candidates)
        client = build_client(settings)
        if client.api_creds is None:
            client.derive_or_create_api_creds()
        trade_target = choose_trade(candidates, portfolio)
        if trade_target is None:
            print(json.dumps({"trade": None, "summary": portfolio.summary()}, indent=2))
        else:
            result = execute_live_trade(client, settings, trade_target, portfolio)
            portfolio.save(settings.state_path)
            print(json.dumps({
                "trade": {
                    "market_id": result.candidate.market_id,
                    "question": result.candidate.question,
                    "outcome": result.candidate.outcome,
                    "order": result.order,
                    "response": result.response,
                },
                "summary": portfolio.summary(),
            }, indent=2))
    elif args.command == "btc-edge-once":
        if not settings.live_trading_enabled:
            raise SystemExit("Live trading is disabled. Set POLYMARKET_ENABLE_LIVE_TRADING=1 to proceed.")
        print(json.dumps(btc_edge_once(settings), indent=2))
    elif args.command == "btc-edge-loop":
        if not settings.live_trading_enabled:
            raise SystemExit("Live trading is disabled. Set POLYMARKET_ENABLE_LIVE_TRADING=1 to proceed.")
        btc_edge_loop(settings)
    elif args.command == "smart-money-once":
        if not settings.live_trading_enabled:
            raise SystemExit("Live trading is disabled. Set POLYMARKET_ENABLE_LIVE_TRADING=1 to proceed.")
        print(json.dumps(smart_money_once(settings), indent=2))
    elif args.command in {"smart-money-loop", "auto-loop"}:
        if not settings.live_trading_enabled:
            raise SystemExit("Live trading is disabled. Set POLYMARKET_ENABLE_LIVE_TRADING=1 to proceed.")
        smart_money_loop(settings)
    else:
        serve(settings)


if __name__ == "__main__":
    main()
