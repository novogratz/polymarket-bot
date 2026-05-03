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
from .models import parse_dt, utc_now
from .portfolio import paper_tick
from .smart_money import DataApiClient, analyze_smart_money, _top_traders
from .trading import build_client, choose_trade, execute_live_sell, execute_live_trade
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
    horizon_hours = settings.smart_soon_hours
    if settings.smart_max_hours_to_close > 0:
        horizon_hours = min(horizon_hours, settings.smart_max_hours_to_close)
    horizon = now + timedelta(hours=horizon_hours)
    batches = []
    for kwargs in (
        {
            "limit": settings.smart_scan_limit,
            "end_date_min": now,
            "end_date_max": horizon,
        },
        {
            "limit": settings.smart_scan_limit,
            "order": "volume",
            "ascending": False,
            "end_date_min": now,
            "end_date_max": horizon,
        },
    ):
        try:
            batches.append(client.get_markets(**kwargs))
        except Exception as exc:
            print(f"⚠️  Gamma smart market batch skipped: {type(exc).__name__}: {exc}")
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
    portfolio = Portfolio(cash=cash, positions=[])
    if settings.private_key and settings.api_key and settings.api_secret and settings.api_passphrase:
        client = build_client(settings)
        live_cash = client.live_available_balance()
        if live_cash > 0:
            cash = round(live_cash, 2)
            source = "live_clob"
        portfolio.cash = cash
        if settings.sync_live_positions and settings.funder_address:
            _sync_live_positions(settings, portfolio)
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
        and not portfolio.has_open_token(candidate.token_id)
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
    sync_report = _sync_live_positions(settings, portfolio) if settings.sync_live_positions else []
    portfolio.mark_to_market(candidates)
    open_count = portfolio.summary()["open_positions"]

    # 1. WHALE EXIT: Check if we should close any existing positions
    client = build_client(settings)
    pending_report = _cancel_stale_pending_orders(client, settings, portfolio)
    whale_exit_report = []
    if portfolio.positions:
        # Get latest smart money trades to see who is still in
        data_client = DataApiClient(settings.data_api_base_url)
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
            whale_watch_failed = False
            for wallet in original_wallets:
                try:
                    recent_trades.extend(data_client.trades(user=wallet, start=exit_lookback))
                except Exception as exc:
                    message = f"whale_watch_timeout_or_api_error: {type(exc).__name__}: {exc}"
                    print(f"⚠️  WHALE WATCH SKIPPED for '{position['question']}': {message}")
                    whale_exit_report.append(
                        {
                            "question": position.get("question"),
                            "status": "skipped",
                            "reason": message,
                        }
                    )
                    recent_trades = []
                    whale_watch_failed = True
                    break
            if whale_watch_failed:
                continue
            
            still_holding = any(t.asset == position.get("token_id") for t in recent_trades)
            if not still_holding and original_wallets:
                print(f"\n🐋 WHALE WATCH: No fresh buys from copied wallets on '{position['question']}'.\n")
                whale_exit_report.append(position["question"])

    require_saved_api_creds(settings)
    exit_report = _execute_sell_strategy(client, settings, portfolio, candidates)

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
        and not portfolio.has_open_token(candidate.token_id)
        and not portfolio.has_pending_token(candidate.token_id)
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

    executed_trades: list[dict[str, object]] = []
    stop_reason: str | None = None
    rejected_signals: list[dict[str, object]] = []
    if report.opportunities:
        # Gracefully wait if out of funds
        live_cash = client.live_available_balance()
        portfolio.cash = round(live_cash, 2)
        if live_cash < 1.0:
            portfolio.save(settings.state_path)
            return {
                "trade": None,
                "strategy": strategy,
                "status": "waiting_for_funds",
                "available_cash": live_cash,
                "whale_exits": whale_exit_report,
                "exits": exit_report,
                "sync": sync_report,
                "pending_orders": pending_report,
                "category_summary": open_categories,
                "scan_report": report.to_dict(),
                "summary": portfolio.summary(),
            }

        for opportunity in report.opportunities:
            if settings.smart_max_orders_per_tick > 0 and len(executed_trades) >= settings.smart_max_orders_per_tick:
                stop_reason = "max_orders_per_tick_reached"
                break
            if portfolio.has_open_position(opportunity.candidate.market_id):
                rejected_signals.append(
                    {
                        "market_id": opportunity.candidate.market_id,
                        "outcome": opportunity.candidate.outcome,
                        "reason": "duplicate_open_market",
                        "selection_reason": opportunity.to_dict()["selection_reason"],
                    }
                )
                continue
            opportunity_payload = opportunity.to_dict()
            print(f"🧠 SELECTED: {opportunity_payload['selection_reason']}")
            max_trade_usd = _max_trade_for_signal(settings, opportunity_payload, strategy)
            try:
                result = execute_live_trade(
                    client,
                    settings,
                    opportunity.candidate,
                    portfolio,
                    min_trade_usd=1.0,
                    max_trade_usd=max_trade_usd,
                    strategy=strategy,
                    signal=opportunity_payload,
                )
                signal = opportunity
                signal_payload = opportunity_payload
                trade_payload = {
                    "strategy": strategy,
                    "signal": signal_payload,
                    "order": result.order,
                    "response": result.response,
                }
                executed_trades.append(trade_payload)
                portfolio.save(settings.state_path)
            except ValueError as e:
                if "Anti-pump" in str(e):
                    print(f"⚠️  Skipping pumped signal: {str(e)}")
                    rejected_signals.append(
                        {
                            "market_id": opportunity.candidate.market_id,
                            "outcome": opportunity.candidate.outcome,
                            "reason": str(e),
                        }
                    )
                    continue
                if _is_unfilled_market_order_error(str(e)):
                    print(f"⚠️  Skipping unfilled market order: {str(e)}")
                    rejected_signals.append(
                        {
                            "market_id": opportunity.candidate.market_id,
                            "outcome": opportunity.candidate.outcome,
                            "reason": str(e),
                        }
                    )
                    continue
                if _is_funds_error(str(e)):
                    stop_reason = str(e)
                    break
                else:
                    raise e
            except Exception as e:
                if _is_unfilled_market_order_error(str(e)):
                    print(f"⚠️  Skipping unfilled market order: {str(e)}")
                    rejected_signals.append(
                        {
                            "market_id": opportunity.candidate.market_id,
                            "outcome": opportunity.candidate.outcome,
                            "reason": str(e),
                        }
                    )
                    continue
                if _is_funds_error(str(e)):
                    stop_reason = str(e)
                    break
                raise

    if executed_trades:
        portfolio.save(settings.state_path)
        return {
            "trade": executed_trades[-1],
            "trades": executed_trades,
            "orders_placed": len(executed_trades),
            "stop_reason": stop_reason,
            "whale_exits": whale_exit_report,
            "exits": exit_report,
            "sync": sync_report,
            "pending_orders": pending_report,
            "category_summary": open_categories,
            "rejected_signals": rejected_signals,
            "scan_report": report.to_dict(),
            "summary": portfolio.summary(),
        }

    portfolio.save(settings.state_path)
    return {
        "trade": None,
        "strategy": "smart_money",
        "whale_exits": whale_exit_report,
        "exits": exit_report,
        "sync": sync_report,
        "pending_orders": pending_report,
        "category_summary": open_categories,
        "rejected_signals": rejected_signals,
        "scan_report": report.to_dict(),
        "summary": portfolio.summary(),
    }


def _execute_sell_strategy(
    client,
    settings: Settings,
    portfolio: Portfolio,
    candidates,
) -> list[dict[str, object]]:
    by_token = {candidate.token_id: candidate for candidate in candidates if candidate.token_id}
    exit_report: list[dict[str, object]] = []
    for position in list(portfolio.positions):
        if position.get("status") != "open" or not position.get("live"):
            continue
        token_id = position.get("token_id")
        candidate = by_token.get(token_id)
        if candidate is None or candidate.best_bid is None:
            continue

        entry_price = float(position.get("entry_price", 0.0))
        if entry_price <= 0:
            continue
        current_pnl_pct = (candidate.best_bid - entry_price) / entry_price
        position["peak_pnl_pct"] = max(float(position.get("peak_pnl_pct", current_pnl_pct)), current_pnl_pct)
        plan = _sell_plan(position, current_pnl_pct, settings)
        if plan is None and _should_exit_before_expiry(candidate, current_pnl_pct, settings):
            plan = {
                "reason": "positive_pnl_before_expiry",
                "shares": float(position.get("shares", 0.0)),
            }
        if plan is None:
            continue

        try:
            result = execute_live_sell(
                client,
                settings,
                candidate,
                portfolio,
                position,
                shares=plan["shares"],
                reason=str(plan["reason"]),
            )
        except ValueError as exc:
            exit_report.append(
                {
                    "market_id": position.get("market_id"),
                    "outcome": position.get("outcome"),
                    "action": "skip_sell",
                    "reason": str(exc),
                    "pnl_pct": round(current_pnl_pct, 4),
                    "peak_pnl_pct": round(float(position.get("peak_pnl_pct", 0.0)), 4),
                }
            )
            continue

        if str(plan["reason"]).startswith("take_profit_"):
            position.setdefault("sell_tiers_hit", []).append(str(plan["tier"]))
        portfolio.save(settings.state_path)
        exit_report.append(
            {
                "market_id": position.get("market_id"),
                "outcome": position.get("outcome"),
                "action": "sell",
                "reason": plan["reason"],
                "pnl_pct": round(current_pnl_pct, 4),
                "peak_pnl_pct": round(float(position.get("peak_pnl_pct", 0.0)), 4),
                "order": result.order,
                "response": result.response,
            }
        )
    return exit_report


def _cancel_stale_pending_orders(client, settings: Settings, portfolio: Portfolio) -> list[dict[str, object]]:
    if settings.smart_pending_order_ttl_seconds <= 0:
        return []
    now = utc_now()
    report: list[dict[str, object]] = []
    for order in portfolio.pending_orders or []:
        if order.get("status") != "live":
            continue
        created_at = parse_dt(str(order.get("created_at") or ""))
        if created_at is None:
            continue
        age_seconds = (now - created_at).total_seconds()
        if age_seconds < settings.smart_pending_order_ttl_seconds:
            continue
        order_id = str(order.get("order_id") or "")
        if not order_id:
            order["status"] = "stale_missing_order_id"
            report.append({"action": "pending_order_stale_missing_order_id", "question": order.get("question")})
            continue
        try:
            response = client.cancel_order(order_id)
        except Exception as exc:
            order["cancel_error"] = f"{type(exc).__name__}: {exc}"
            report.append(
                {
                    "action": "pending_order_cancel_failed",
                    "order_id": order_id,
                    "question": order.get("question"),
                    "reason": order["cancel_error"],
                }
            )
            continue
        order["status"] = "canceled"
        order["canceled_at"] = now.isoformat()
        order["cancel_response"] = response
        report.append(
            {
                "action": "pending_order_canceled",
                "order_id": order_id,
                "question": order.get("question"),
                "age_seconds": round(age_seconds, 1),
                "response": response,
            }
        )
    if report:
        portfolio.save(settings.state_path)
    return report


def _max_trade_for_signal(settings: Settings, signal: dict[str, object], strategy: str) -> float:
    base_cap = (
        min(settings.starter_trade_usd, settings.max_position_usd)
        if strategy == "smart_money_starter"
        else min(settings.smart_max_trade_usd, settings.max_position_usd)
    )
    metrics = signal.get("selection_metrics", {}) if isinstance(signal.get("selection_metrics"), dict) else {}
    consensus = int(metrics.get("profitable_wallet_count") or signal.get("consensus") or 0)
    copied_usdc = float(metrics.get("copied_usdc") or signal.get("copied_usdc") or 0.0)
    if metrics.get("is_crypto_micro"):
        base_cap = min(base_cap, settings.smart_crypto_micro_max_trade_usd)
    if consensus >= 4 and copied_usdc >= 1000:
        quality_cap = settings.max_position_usd
    elif consensus >= 3 and copied_usdc >= 250:
        quality_cap = min(settings.max_position_usd, 10.0)
    else:
        quality_cap = min(settings.max_position_usd, 5.0)
    return min(base_cap, quality_cap)


def _sell_plan(position: dict[str, object], current_pnl_pct: float, settings: Settings) -> dict[str, object] | None:
    current_shares = float(position.get("shares", 0.0))
    if current_shares <= 0:
        return None
    peak_pnl_pct = float(position.get("peak_pnl_pct", current_pnl_pct))
    if peak_pnl_pct >= settings.smart_peak_protect_trigger and current_pnl_pct <= settings.smart_peak_protect_floor:
        return {
            "reason": "peak_profit_protection",
            "shares": current_shares,
        }

    exits = position.get("exits", [])
    sold_shares = sum(float(exit_record.get("shares", 0.0)) for exit_record in exits if isinstance(exit_record, dict))
    initial_shares = float(position.get("initial_shares", current_shares + sold_shares))
    tiers_hit = {str(item) for item in position.get("sell_tiers_hit", []) if item is not None}
    for threshold, fraction in _take_profit_tiers(settings):
        tier_key = str(threshold)
        if current_pnl_pct >= threshold and tier_key not in tiers_hit:
            return {
                "reason": f"take_profit_{int(threshold * 100)}pct",
                "tier": tier_key,
                "shares": min(current_shares, initial_shares * fraction),
            }
    return None


def _should_exit_before_expiry(candidate, current_pnl_pct: float, settings: Settings) -> bool:
    if settings.smart_exit_minutes_to_close <= 0:
        return False
    if candidate.hours_to_close is None:
        return False
    return (
        candidate.hours_to_close * 60 <= settings.smart_exit_minutes_to_close
        and current_pnl_pct >= settings.smart_exit_min_profit
    )


def _take_profit_tiers(settings: Settings) -> list[tuple[float, float]]:
    tiers: list[tuple[float, float]] = []
    for item in settings.smart_take_profit_tiers.split(","):
        if not item.strip() or ":" not in item:
            continue
        threshold, fraction = item.split(":", 1)
        try:
            tiers.append((float(threshold), float(fraction)))
        except ValueError:
            continue
    return sorted(tiers)


def _sync_live_positions(settings: Settings, portfolio: Portfolio) -> list[dict[str, object]]:
    if not settings.funder_address:
        return []
    report: list[dict[str, object]] = []
    try:
        live_positions = DataApiClient(settings.data_api_base_url).positions(user=settings.funder_address)
    except Exception as exc:
        return [{"action": "sync_skipped", "reason": f"{type(exc).__name__}: {exc}"}]

    active_by_token: dict[str, dict[str, object]] = {}
    for item in live_positions:
        token_id = str(item.get("asset") or "")
        if not token_id:
            continue
        size = _float(item.get("size"))
        current_value = _float(item.get("currentValue"))
        if size <= 0 or current_value < settings.live_position_min_value_usd:
            continue
        active_by_token[token_id] = item

    local_by_token = {
        str(position.get("token_id")): position
        for position in portfolio.positions
        if position.get("live") and position.get("token_id")
    }
    for token_id, position in local_by_token.items():
        if position.get("status") == "open" and token_id not in active_by_token:
            position["status"] = "closed"
            position["closed_at"] = utc_now().isoformat()
            position["sync_closed"] = True
            report.append({"action": "closed_stale_local_position", "token_id": token_id})

    for token_id, item in active_by_token.items():
        position = local_by_token.get(token_id)
        for pending in portfolio.pending_orders or []:
            if pending.get("status") == "live" and pending.get("token_id") == token_id:
                pending["status"] = "filled_by_live_sync"
                pending["filled_at"] = utc_now().isoformat()
        if position is None:
            position = _position_from_live_api(item)
            portfolio.positions.append(position)
            report.append({"action": "imported_live_position", "token_id": token_id, "question": position.get("question")})
        else:
            _update_position_from_live_api(position, item)
            report.append({"action": "updated_live_position", "token_id": token_id, "question": position.get("question")})
    return report


def _position_from_live_api(item: dict[str, object]) -> dict[str, object]:
    size = _float(item.get("size"))
    avg_price = _float(item.get("avgPrice"))
    current_price = _float(item.get("curPrice"), avg_price)
    stake = round(_float(item.get("initialValue"), size * avg_price), 2)
    return {
        "status": "open",
        "opened_at": utc_now().isoformat(),
        "market_id": str(item.get("conditionId") or item.get("eventId") or ""),
        "question": str(item.get("title") or ""),
        "slug": str(item.get("slug") or item.get("eventSlug") or ""),
        "url": f"https://polymarket.com/event/{item.get('eventSlug') or item.get('slug') or ''}",
        "outcome": str(item.get("outcome") or ""),
        "token_id": str(item.get("asset") or ""),
        "entry_price": avg_price,
        "current_price": current_price,
        "stake": stake,
        "shares": size,
        "initial_shares": _float(item.get("totalBought"), size),
        "unrealized_pnl": round(_float(item.get("currentValue"), size * current_price) - stake, 2),
        "realized_pnl": round(_float(item.get("realizedPnl")), 2),
        "live": True,
        "strategy": "live_sync",
        "synced_from_polymarket": True,
    }


def _update_position_from_live_api(position: dict[str, object], item: dict[str, object]) -> None:
    size = _float(item.get("size"))
    avg_price = _float(item.get("avgPrice"), _float(position.get("entry_price")))
    current_price = _float(item.get("curPrice"), avg_price)
    stake = round(_float(item.get("initialValue"), size * avg_price), 2)
    position["shares"] = size
    position["entry_price"] = avg_price
    position["current_price"] = current_price
    position["stake"] = stake
    position["initial_shares"] = max(_float(position.get("initial_shares")), _float(item.get("totalBought"), size), size)
    position["unrealized_pnl"] = round(_float(item.get("currentValue"), size * current_price) - stake, 2)
    position["realized_pnl"] = round(_float(item.get("realizedPnl"), _float(position.get("realized_pnl"))), 2)
    position["status"] = "open"
    position["synced_from_polymarket"] = True


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_funds_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in (
            "insufficient",
            "not enough",
            "no live balance",
            "no cash available",
            "below polymarket",
            "below minimum",
        )
    )


def _is_unfilled_market_order_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in (
            "fully filled or killed",
            "couldn't be fully filled",
            "could not be fully filled",
        )
    )


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
