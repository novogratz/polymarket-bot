from __future__ import annotations

import argparse
import json
import time

from datetime import timedelta

from .bitcoin import CoinbaseBtcClient, choose_btc_edge_trade
from .config import Settings
from .dashboard import serve
from .gamma import GammaClient
from .portfolio import Portfolio
from .models import utc_now
from .portfolio import paper_tick
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


def scan(settings: Settings) -> list[dict[str, object]]:
    return [candidate.to_dict() for candidate in load_candidates(settings)]


def bootstrap_creds(settings: Settings) -> dict[str, str]:
    client = build_client(settings)
    creds = client.derive_or_create_api_creds()
    return creds.to_dict()


def btc_edge_once(settings: Settings) -> dict[str, object]:
    candidates = load_candidates(settings)
    btc_model = CoinbaseBtcClient().model(settings)
    portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)
    portfolio.mark_to_market(candidates)
    client = build_client(settings)
    if client.api_creds is None:
        client.derive_or_create_api_creds()

    eligible_candidates = [
        candidate
        for candidate in candidates
        if not portfolio.has_open_position(candidate.market_id, candidate.outcome)
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

    result = execute_live_trade(client, settings, signal.candidate, portfolio)
    portfolio.save(settings.state_path)
    return {
        "trade": {
            "signal": signal.to_dict(),
            "order": result.order,
            "response": result.response,
        },
        "summary": portfolio.summary(),
    }


def btc_edge_loop(settings: Settings) -> None:
    tick = 0
    while settings.auto_max_ticks <= 0 or tick < settings.auto_max_ticks:
        tick += 1
        started_at = utc_now()
        try:
            result: dict[str, object] = {
                "tick": tick,
                "started_at": started_at.isoformat(),
                "result": btc_edge_once(settings),
            }
        except Exception as exc:
            result = {
                "tick": tick,
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
            "bootstrap-creds",
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
    else:
        serve(settings)


if __name__ == "__main__":
    main()
