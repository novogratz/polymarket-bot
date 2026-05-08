"""Tick orchestration, sizing helpers, trade journal, and CLI entry point.

This module owns the end-to-end smart-money tick: load Gamma markets, sync
live positions, refresh live cash, run the cohort-exit and sell strategies,
execute the three-pass smart-money scan with reverse-lookup, place trades
with conviction-weighted dynamic sizing, optionally run the BTC edge tick
and the noise fallback, then persist state and stream a JSON tick result.

It also exposes the package's six public CLI commands: ``auto-loop``,
``dashboard``, ``journal-stats``, ``tune-strategy``, ``bootstrap-creds``,
and ``reset-ledger``.
"""

from __future__ import annotations

import argparse
import json
import time

from dataclasses import replace
from datetime import timedelta

from .auto_tuner import apply_overrides, maybe_tune
from .bitcoin import CoinbaseBtcClient, choose_btc_edge_trade
from .config import Settings
from .dashboard import serve
from .gamma import GammaClient
from .portfolio import Portfolio
from .models import parse_dt, utc_now
from .smart_money import (
    DataApiClient,
    analyze_smart_money,
    analyze_smart_money_with_data,
    fetch_smart_money_data,
    market_category,
    _top_traders,
)
from .trading import build_client, execute_live_sell, execute_live_trade
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
    keyword_limit = max(20, min(settings.smart_scan_limit // 10, 100))
    for keyword in _smart_discovery_keywords(settings):
        try:
            batches.append(
                client.get_markets(
                    limit=keyword_limit,
                    order="volume",
                    ascending=False,
                    end_date_min=now,
                    end_date_max=horizon,
                    question_contains=keyword,
                )
            )
        except Exception as exc:
            print(f"⚠️  Gamma keyword batch skipped: {keyword} {type(exc).__name__}: {exc}")
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
    print("▶  tick start", flush=True)
    if settings.smart_auto_tune_enabled:
        overrides, journal_size = maybe_tune(settings)
        if overrides:
            print(
                f"   auto-tune: {len(overrides)} override(s) from {journal_size} closed trade(s): {overrides}",
                flush=True,
            )
            settings = apply_overrides(settings, overrides)
        elif journal_size < settings.smart_auto_tune_min_trades:
            print(
                f"   auto-tune: paused ({journal_size}/{settings.smart_auto_tune_min_trades} closed trades)",
                flush=True,
            )
    print("   loading markets...", flush=True)
    candidates = load_smart_candidates(settings)
    print(f"   markets: {len(candidates)} candidates", flush=True)
    portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)
    if settings.sync_live_positions:
        print("   syncing live positions...", flush=True)
        sync_report = _sync_live_positions(settings, portfolio)
        print(f"   sync actions: {len(sync_report)}", flush=True)
    else:
        sync_report = []
    portfolio.mark_to_market(candidates)
    open_count = portfolio.summary()["open_positions"]
    print(f"   open positions: {open_count}", flush=True)

    client = build_client(settings)
    pending_report = _cancel_stale_pending_orders(client, settings, portfolio)
    if pending_report:
        print(f"   pending orders cleared: {len(pending_report)}", flush=True)
    live_open_count = sum(
        1 for p in portfolio.positions if p.get("status") == "open" and p.get("live")
    )
    if settings.smart_cohort_exit_enabled and live_open_count:
        print(f"   cohort-exit check on {live_open_count} live position(s)...", flush=True)
    cohort_exit_tokens, whale_exit_report = _detect_cohort_exits(settings, portfolio)
    if cohort_exit_tokens:
        print(f"   cohort flipped on {len(cohort_exit_tokens)} token(s) -> exit", flush=True)

    require_saved_api_creds(settings)
    print("   running sell strategy...", flush=True)
    exit_report = _execute_sell_strategy(
        client,
        settings,
        portfolio,
        candidates,
        cohort_exit_tokens=cohort_exit_tokens,
    )
    sells = sum(1 for e in exit_report if e.get("action") == "sell")
    if sells:
        print(f"   sells executed: {sells}", flush=True)

    try:
        live_cash = client.live_available_balance()
        portfolio.cash = round(live_cash, 2)
        print(f"   live cash: ${portfolio.cash:.2f}", flush=True)
    except Exception as exc:
        print(f"   live cash refresh failed: {type(exc).__name__}: {exc}", flush=True)

    # 2. CATEGORY DIVERSIFICATION: Count open categories
    open_categories: dict[str, int] = {}
    for pos in portfolio.positions:
        if pos.get("status") == "open":
            cat = pos.get("signal", {}).get("category") or market_category(
                str(pos.get("question") or ""),
                str(pos.get("slug") or ""),
            )
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
        and not portfolio.has_open_event_position(candidate)
    ]

    print(f"   smart-money scan over {len(eligible_candidates)} eligible candidate(s)...", flush=True)
    smart_data = fetch_smart_money_data(settings)

    if settings.smart_reverse_lookup_enabled:
        extra_candidates = _reverse_lookup_smart_money_markets(settings, smart_data, candidates)
        if extra_candidates:
            existing_tokens_eligible = {c.token_id for c in eligible_candidates if c.token_id}
            added = 0
            for extra in extra_candidates:
                if not extra.token_id or extra.token_id in existing_tokens_eligible:
                    continue
                if (
                    not extra.accepts_orders
                    or extra.best_bid is None
                    or extra.best_ask is None
                    or extra.tick_size is None
                ):
                    continue
                if portfolio.has_open_position(extra.market_id):
                    continue
                if portfolio.has_open_token(extra.token_id):
                    continue
                if portfolio.has_pending_token(extra.token_id):
                    continue
                if portfolio.has_open_event_position(extra):
                    continue
                eligible_candidates.append(extra)
                existing_tokens_eligible.add(extra.token_id)
                added += 1
            if added:
                print(f"   eligible after reverse-lookup: {len(eligible_candidates)} (+{added})", flush=True)

    report = analyze_smart_money_with_data(eligible_candidates, settings, smart_data)
    print(f"   strict scan: {len(report.opportunities)} opportunity(ies)", flush=True)
    signal = report.selected
    strategy = "smart_money"
    opportunities = list(report.opportunities)
    if open_count + len(opportunities) < settings.min_open_positions:
        print(
            f"   below min open positions ({open_count + len(opportunities)} < {settings.min_open_positions}); running relaxed scan (reusing leaderboard+trades)...",
            flush=True,
        )
        fallback_settings = replace(
            settings,
            smart_min_consensus=max(2, settings.smart_fallback_consensus),
        )
        fallback_report = analyze_smart_money_with_data(eligible_candidates, fallback_settings, smart_data)
        print(f"   relaxed scan: {len(fallback_report.opportunities)} opportunity(ies)", flush=True)
        seen_tokens = {opp.candidate.token_id for opp in opportunities if opp.candidate.token_id}
        for opp in fallback_report.opportunities:
            token_id = opp.candidate.token_id
            if token_id and token_id in seen_tokens:
                continue
            opportunities.append(opp)
            if token_id:
                seen_tokens.add(token_id)
        if signal is None and fallback_report.selected is not None:
            report = fallback_report
            signal = fallback_report.selected
            strategy = "smart_money_starter"
        if (
            settings.smart_deep_fallback_enabled
            and open_count + len(opportunities) < settings.min_open_positions
        ):
            print(
                f"   still below min ({open_count + len(opportunities)} < {settings.min_open_positions}); running deep fallback (single-wallet, loosened filters)...",
                flush=True,
            )
            deep_settings = replace(
                settings,
                smart_min_consensus=1,
                smart_min_buy_price=max(0.02, settings.smart_min_buy_price - 0.02),
                smart_max_buy_price=min(0.98, settings.smart_max_buy_price + 0.03),
                smart_max_relative_spread=max(0.40, settings.smart_max_relative_spread),
                smart_max_chase_premium=max(0.15, settings.smart_max_chase_premium),
                smart_min_copied_usdc=max(
                    settings.smart_deep_fallback_min_copied_usdc, settings.smart_min_copied_usdc
                ),
            )
            deep_report = analyze_smart_money_with_data(eligible_candidates, deep_settings, smart_data)
            print(f"   deep fallback: {len(deep_report.opportunities)} opportunity(ies)", flush=True)
            for opp in deep_report.opportunities:
                token_id = opp.candidate.token_id
                if token_id and token_id in seen_tokens:
                    continue
                opportunities.append(opp)
                if token_id:
                    seen_tokens.add(token_id)
            if signal is None and deep_report.selected is not None:
                report = deep_report
                signal = deep_report.selected
                strategy = "smart_money_deep_fallback"

    signal_payload = signal.to_dict() if signal else None

    executed_trades: list[dict[str, object]] = []
    stop_reason: str | None = None
    rejected_signals: list[dict[str, object]] = []
    if opportunities:
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

        for slot_index, opportunity in enumerate(opportunities):
            remaining_slots = max(1, len(opportunities) - slot_index)
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
            if portfolio.has_open_event_position(opportunity.candidate):
                rejected_signals.append(
                    {
                        "market_id": opportunity.candidate.market_id,
                        "outcome": opportunity.candidate.outcome,
                        "event_slug": opportunity.candidate.event_slug,
                        "reason": "duplicate_open_sports_event",
                        "selection_reason": opportunity.to_dict()["selection_reason"],
                    }
                )
                continue
            opportunity_payload = opportunity.to_dict()
            category = str(opportunity_payload.get("category") or "OTHER")
            if (
                category == "SPORTS"
                and settings.smart_max_sports_positions >= 0
                and open_categories.get("SPORTS", 0) >= settings.smart_max_sports_positions
            ):
                rejected_signals.append(
                    {
                        "market_id": opportunity.candidate.market_id,
                        "outcome": opportunity.candidate.outcome,
                        "reason": "sports_position_cap_reached",
                        "category": category,
                        "selection_reason": opportunity_payload["selection_reason"],
                    }
                )
                continue
            print(f"🧠 SELECTED: {opportunity_payload['selection_reason']}")
            max_trade_usd = _dynamic_max_trade(
                settings, opportunity_payload, strategy, portfolio, remaining_slots
            )
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
                open_categories[category] = open_categories.get(category, 0) + 1
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

    noise_trades: list[dict[str, object]] = []
    if settings.smart_noise_fallback_enabled:
        noise_picks = _noise_fallback_candidates(
            settings, portfolio, candidates, open_categories, smart_data=smart_data
        )
        if noise_picks:
            label = (
                "below min positions or above cash-pressure threshold"
                if not executed_trades
                else "smart-money fired but cash still idle"
            )
            print(
                f"   noise fallback: trying {len(noise_picks)} small bet(s) ({label})",
                flush=True,
            )
            for candidate in noise_picks:
                if portfolio.has_open_position(candidate.market_id):
                    continue
                if portfolio.has_open_token(candidate.token_id):
                    continue
                if portfolio.has_pending_token(candidate.token_id):
                    continue
                if portfolio.has_open_event_position(candidate):
                    continue
                try:
                    noise_signal = {
                        "category": market_category(candidate.question, candidate.slug),
                        "selection_reason": "noise_fallback: no smart-money signal; small bet on top-scored candidate",
                        "selection_metrics": {
                            "current_ask": candidate.best_ask,
                            "current_bid": candidate.best_bid,
                            "spread": (candidate.best_ask or 0) - (candidate.best_bid or 0),
                        },
                    }
                    noise_result = execute_live_trade(
                        client,
                        settings,
                        candidate,
                        portfolio,
                        min_trade_usd=1.0,
                        max_trade_usd=settings.smart_noise_fallback_max_trade_usd,
                        strategy="noise_fallback",
                        signal=noise_signal,
                    )
                    noise_trades.append(
                        {
                            "strategy": "noise_fallback",
                            "signal": noise_signal,
                            "order": noise_result.order,
                            "response": noise_result.response,
                        }
                    )
                    portfolio.save(settings.state_path)
                except ValueError as exc:
                    if _is_funds_error(str(exc)):
                        break
                    print(f"   noise fallback skipped: {exc}", flush=True)
                    continue
                except Exception as exc:
                    if _is_unfilled_market_order_error(str(exc)) or _is_funds_error(str(exc)):
                        continue
                    print(f"   noise fallback error: {type(exc).__name__}: {exc}", flush=True)
                    continue

    portfolio.save(settings.state_path)
    last_smart_trade = executed_trades[-1] if executed_trades else None
    last_noise_trade = noise_trades[-1] if noise_trades else None
    response: dict[str, object] = {
        "trade": last_smart_trade or last_noise_trade,
        "strategy": (
            strategy
            if executed_trades
            else ("noise_fallback" if noise_trades else "smart_money")
        ),
        "trades": executed_trades,
        "orders_placed": len(executed_trades),
        "stop_reason": stop_reason,
        "noise_trades": noise_trades,
        "whale_exits": whale_exit_report,
        "exits": exit_report,
        "sync": sync_report,
        "pending_orders": pending_report,
        "category_summary": open_categories,
        "rejected_signals": rejected_signals,
        "scan_report": report.to_dict(),
        "summary": portfolio.summary(),
    }
    if settings.btc_edge_integrated:
        try:
            print("   running btc-edge tick...", flush=True)
            response["btc_edge"] = btc_edge_once(settings)
        except Exception as exc:
            print(f"   btc-edge tick failed: {type(exc).__name__}: {exc}", flush=True)
            response["btc_edge"] = {"error": f"{type(exc).__name__}: {exc}"}
    return response


def _execute_sell_strategy(
    client,
    settings: Settings,
    portfolio: Portfolio,
    candidates,
    *,
    cohort_exit_tokens: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    by_token = {candidate.token_id: candidate for candidate in candidates if candidate.token_id}
    cohort_exit_tokens = cohort_exit_tokens or {}
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
        if (
            plan is None
            and settings.smart_resolved_exit_threshold > 0
            and candidate.best_bid is not None
            and candidate.best_bid >= settings.smart_resolved_exit_threshold
        ):
            plan = {
                "reason": "resolved_market_exit",
                "shares": float(position.get("shares", 0.0)),
            }
        if plan is None and _should_exit_before_expiry(candidate, current_pnl_pct, settings):
            plan = {
                "reason": "positive_pnl_before_expiry",
                "shares": float(position.get("shares", 0.0)),
            }
        if plan is None and token_id in cohort_exit_tokens:
            plan = {
                "reason": cohort_exit_tokens[token_id],
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
        except Exception as exc:
            message = str(exc)
            print(f"⚠️  sell skipped on {position.get('question')}: {type(exc).__name__}: {message}", flush=True)
            exit_report.append(
                {
                    "market_id": position.get("market_id"),
                    "outcome": position.get("outcome"),
                    "action": "skip_sell",
                    "reason": f"{type(exc).__name__}: {message}",
                    "pnl_pct": round(current_pnl_pct, 4),
                    "peak_pnl_pct": round(float(position.get("peak_pnl_pct", 0.0)), 4),
                }
            )
            if "balance is not enough" in message.lower() or "allowance" in message.lower():
                position.setdefault("sell_blocked_reason", "active_sell_order_pending")
            continue

        if str(plan["reason"]).startswith("take_profit_"):
            position.setdefault("sell_tiers_hit", []).append(str(plan["tier"]))
        portfolio.save(settings.state_path)
        if position.get("status") == "closed":
            _append_trade_journal(settings, position, str(plan["reason"]))
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


def _noise_fallback_candidates(
    settings: Settings,
    portfolio: Portfolio,
    candidates: list,
    open_categories: dict[str, int],
    smart_data=None,
) -> list:
    if not settings.smart_noise_fallback_enabled:
        return []
    open_count = sum(1 for p in portfolio.positions if p.get("status") == "open")
    summary = portfolio.summary()
    cash = float(summary.get("cash") or 0.0)
    invested = float(summary.get("invested") or 0.0)
    unrealized = float(summary.get("unrealized_pnl") or 0.0)
    equity = cash + invested + unrealized
    cash_pct = (cash / equity) if equity > 0 else 0.0
    below_min = open_count < settings.min_open_positions
    cash_pressure = (
        settings.smart_noise_fallback_cash_pressure_pct > 0
        and cash_pct > settings.smart_noise_fallback_cash_pressure_pct
    )
    if not below_min and not cash_pressure:
        return []

    smart_active_tokens: dict[str, float] = {}
    if smart_data is not None and getattr(smart_data, "trades", None):
        for trade in smart_data.trades:
            asset = getattr(trade, "asset", None)
            if not asset:
                continue
            smart_active_tokens[asset] = smart_active_tokens.get(asset, 0.0) + float(
                getattr(trade, "usdc_size", 0.0) or 0.0
            )

    if smart_active_tokens:
        ranked = sorted(
            (c for c in candidates if c.token_id and c.token_id in smart_active_tokens),
            key=lambda c: smart_active_tokens.get(c.token_id, 0.0),
            reverse=True,
        )
        ordered = ranked + [c for c in candidates if c not in ranked]
    else:
        ordered = list(candidates)

    picks: list = []
    seen_market_ids: set[str] = set()
    for candidate in ordered:
        if not candidate.token_id or not candidate.accepts_orders:
            continue
        if candidate.best_bid is None or candidate.best_ask is None or candidate.tick_size is None:
            continue
        if candidate.best_ask < settings.smart_noise_fallback_min_buy_price:
            continue
        if candidate.best_ask > settings.smart_noise_fallback_max_buy_price:
            continue
        spread = candidate.best_ask - candidate.best_bid
        if spread < 0 or spread > settings.smart_noise_fallback_max_spread:
            continue
        market_id = str(candidate.market_id or "")
        if market_id and market_id in seen_market_ids:
            continue
        if portfolio.has_open_position(candidate.market_id):
            continue
        if portfolio.has_open_token(candidate.token_id):
            continue
        if portfolio.has_pending_token(candidate.token_id):
            continue
        if portfolio.has_open_event_position(candidate):
            continue
        category = market_category(candidate.question, candidate.slug)
        if category == "SPORTS" and open_categories.get("SPORTS", 0) >= settings.smart_max_sports_positions:
            continue
        picks.append(candidate)
        if market_id:
            seen_market_ids.add(market_id)
        if len(picks) >= settings.smart_noise_fallback_max_trades_per_tick:
            break
    return picks


def _reverse_lookup_smart_money_markets(
    settings: Settings,
    smart_data,
    existing_candidates: list,
) -> list:
    if not settings.smart_reverse_lookup_enabled or not getattr(smart_data, "trades", None):
        return []
    existing_tokens = {c.token_id for c in existing_candidates if c.token_id}
    flow_by_token: dict[str, float] = {}
    for trade in smart_data.trades:
        if trade.side.upper() != "BUY":
            continue
        if not trade.asset or trade.asset in existing_tokens:
            continue
        flow_by_token[trade.asset] = flow_by_token.get(trade.asset, 0.0) + trade.usdc_size
    flow_by_token = {
        token: usdc
        for token, usdc in flow_by_token.items()
        if usdc >= settings.smart_reverse_lookup_min_copied_usdc
    }
    if not flow_by_token:
        return []
    top_tokens = sorted(flow_by_token.items(), key=lambda kv: kv[1], reverse=True)[
        : max(1, settings.smart_reverse_lookup_max_tokens)
    ]
    print(
        f"   reverse-lookup: fetching markets for {len(top_tokens)} smart-money token(s) not in scan...",
        flush=True,
    )
    gamma = GammaClient(settings.gamma_base_url)
    try:
        markets = gamma.get_markets_by_clob_token_ids([token for token, _ in top_tokens])
    except Exception as exc:
        print(f"   reverse-lookup failed: {type(exc).__name__}: {exc}", flush=True)
        return []
    if not markets:
        print("   reverse-lookup: 0 markets returned by Gamma (clob_token_ids filter may be unsupported)", flush=True)
        return []
    smart_settings = replace(
        settings,
        scan_limit=settings.smart_scan_limit,
        soon_hours=settings.smart_soon_hours,
        min_liquidity_usd=min(settings.min_liquidity_usd, settings.smart_reverse_lookup_min_liquidity_usd),
        min_volume_usd=min(settings.min_volume_usd, settings.smart_reverse_lookup_min_volume_usd),
    )
    new_candidates = rank_markets(markets, smart_settings)
    print(
        f"   reverse-lookup: gamma returned {len(markets)} market(s), {len(new_candidates)} survived ranking",
        flush=True,
    )
    return new_candidates


def _detect_cohort_exits(
    settings: Settings,
    portfolio: Portfolio,
) -> tuple[dict[str, str], list[dict[str, object]]]:
    report: list[dict[str, object]] = []
    exit_tokens: dict[str, str] = {}
    if not settings.smart_cohort_exit_enabled or not portfolio.positions:
        return exit_tokens, report
    data_client = DataApiClient(settings.data_api_base_url)
    lookback = int(time.time()) - max(settings.smart_cohort_exit_lookback_minutes, 1) * 60
    min_age = max(settings.smart_cohort_exit_min_age_minutes, 0)
    min_wallets = max(settings.smart_cohort_exit_min_wallets, 1)
    for position in portfolio.positions:
        if position.get("status") != "open" or not position.get("live"):
            continue
        token_id = str(position.get("token_id") or "")
        if not token_id:
            continue
        signal = position.get("signal")
        if not isinstance(signal, dict):
            continue
        wallets = [w for w in (signal.get("wallets") or []) if isinstance(w, str) and w]
        if len(wallets) < min_wallets:
            continue
        if _position_age_minutes(position) < min_age:
            continue
        cohort_lower = {w.lower() for w in wallets}
        recent_trades: list = []
        failed = False
        for wallet in wallets:
            try:
                recent_trades.extend(data_client.trades(user=wallet, start=lookback, side="BUY"))
                recent_trades.extend(data_client.trades(user=wallet, start=lookback, side="SELL"))
            except Exception as exc:
                report.append(
                    {
                        "question": position.get("question"),
                        "status": "skipped",
                        "reason": f"cohort_watch_api_error: {type(exc).__name__}: {exc}",
                    }
                )
                failed = True
                break
        if failed:
            continue
        sells_by_cohort = {
            t.wallet.lower()
            for t in recent_trades
            if getattr(t, "asset", None) == token_id
            and t.side.upper() == "SELL"
            and t.wallet.lower() in cohort_lower
        }
        buys_by_cohort = {
            t.wallet.lower()
            for t in recent_trades
            if getattr(t, "asset", None) == token_id
            and t.side.upper() == "BUY"
            and t.wallet.lower() in cohort_lower
        }
        if sells_by_cohort:
            exit_tokens[token_id] = "cohort_sold"
            report.append(
                {
                    "question": position.get("question"),
                    "token_id": token_id,
                    "status": "cohort_sold",
                    "selling_wallets": sorted(sells_by_cohort),
                    "wallets": wallets,
                    "lookback_minutes": settings.smart_cohort_exit_lookback_minutes,
                }
            )
        elif not buys_by_cohort:
            exit_tokens[token_id] = "cohort_silent"
            report.append(
                {
                    "question": position.get("question"),
                    "token_id": token_id,
                    "status": "cohort_silent",
                    "wallets": wallets,
                    "lookback_minutes": settings.smart_cohort_exit_lookback_minutes,
                }
            )
    return exit_tokens, report


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


def _signal_quality_multiplier(signal: dict[str, object]) -> tuple[float, bool]:
    metrics = signal.get("selection_metrics", {}) if isinstance(signal.get("selection_metrics"), dict) else {}
    consensus = int(metrics.get("profitable_wallet_count") or signal.get("consensus") or 0)
    copied_usdc = float(metrics.get("copied_usdc") or signal.get("copied_usdc") or 0.0)
    is_crypto_micro = bool(metrics.get("is_crypto_micro"))
    if is_crypto_micro:
        return 0.55, True
    if consensus >= 5 and copied_usdc >= 5000:
        return 2.5, False
    if consensus >= 4 and copied_usdc >= 2000:
        return 2.0, False
    if consensus >= 4 and copied_usdc >= 1000:
        return 1.6, False
    if consensus >= 3 and copied_usdc >= 500:
        return 1.3, False
    if consensus >= 3 and copied_usdc >= 250:
        return 1.1, False
    if consensus >= 2 and copied_usdc >= 1000:
        return 1.1, False
    if consensus >= 2 and copied_usdc >= 250:
        return 0.9, False
    return 0.7, False


def _dynamic_max_trade(
    settings: Settings,
    signal: dict[str, object],
    strategy: str,
    portfolio: Portfolio,
    remaining_slots: int,
) -> float:
    base = _max_trade_for_signal(settings, signal, strategy, available_cash=portfolio.cash)
    if settings.smart_cash_floor_pct <= 0 or settings.smart_cash_floor_pct >= 1:
        return base
    summary = portfolio.summary()
    cash = float(summary.get("cash") or 0.0)
    invested = float(summary.get("invested") or 0.0)
    unrealized = float(summary.get("unrealized_pnl") or 0.0)
    total_equity = cash + invested + unrealized
    if total_equity <= 0:
        return base
    target_deployed = total_equity * (1.0 - settings.smart_cash_floor_pct)
    remaining_to_deploy = max(0.0, target_deployed - invested)
    if remaining_to_deploy <= 0 or remaining_slots <= 0:
        return base
    quality_mult, is_crypto_micro = _signal_quality_multiplier(signal)
    per_slot = remaining_to_deploy / max(1, remaining_slots)
    dynamic = per_slot * quality_mult
    if is_crypto_micro:
        dynamic = min(dynamic, settings.smart_crypto_micro_max_trade_usd)
    ceiling = settings.smart_max_position_ceiling_usd
    if settings.smart_max_position_ceiling_pct > 0 and total_equity > 0:
        ceiling = max(ceiling, total_equity * settings.smart_max_position_ceiling_pct)
    if ceiling > 0:
        dynamic = min(dynamic, ceiling)
    dynamic = min(dynamic, cash)
    return round(max(base, dynamic), 2)


def _max_trade_for_signal(
    settings: Settings,
    signal: dict[str, object],
    strategy: str,
    available_cash: float | None = None,
) -> float:
    quality_mult, is_crypto_micro = _signal_quality_multiplier(signal)
    metrics = signal.get("selection_metrics", {}) if isinstance(signal.get("selection_metrics"), dict) else {}
    consensus = int(metrics.get("profitable_wallet_count") or signal.get("consensus") or 0)
    copied_usdc = float(metrics.get("copied_usdc") or signal.get("copied_usdc") or 0.0)

    if (
        settings.smart_position_pct > 0
        and available_cash is not None
        and available_cash > 0
    ):
        size = available_cash * settings.smart_position_pct * quality_mult
        if settings.smart_max_position_ceiling_usd > 0:
            size = min(size, settings.smart_max_position_ceiling_usd)
        if is_crypto_micro:
            size = min(size, settings.smart_crypto_micro_max_trade_usd)
        return round(max(0.0, size), 2)

    base_cap = (
        min(settings.starter_trade_usd, settings.max_position_usd)
        if strategy == "smart_money_starter"
        else min(settings.smart_max_trade_usd, settings.max_position_usd)
    )
    if is_crypto_micro:
        base_cap = min(base_cap, settings.smart_crypto_micro_max_trade_usd)
    if consensus >= 4 and copied_usdc >= 1000:
        quality_cap = settings.max_position_usd
    elif consensus >= 3 and copied_usdc >= 250:
        quality_cap = min(settings.max_position_usd, 10.0)
    elif consensus >= 2 and copied_usdc >= 1000:
        quality_cap = settings.max_position_usd
    elif consensus >= 2 and copied_usdc >= 250:
        quality_cap = min(settings.max_position_usd, 10.0)
    else:
        quality_cap = min(settings.max_position_usd, 5.0)
    return min(base_cap, quality_cap)


def _smart_discovery_keywords(settings: Settings) -> list[str]:
    seen: set[str] = set()
    keywords: list[str] = []
    for raw in settings.smart_discovery_keywords.split(","):
        keyword = raw.strip()
        key = keyword.lower()
        if keyword and key not in seen:
            seen.add(key)
            keywords.append(keyword)
    return keywords


def _sell_plan(position: dict[str, object], current_pnl_pct: float, settings: Settings) -> dict[str, object] | None:
    current_shares = float(position.get("shares", 0.0))
    if current_shares <= 0:
        return None
    peak_pnl_pct = float(position.get("peak_pnl_pct", current_pnl_pct))
    if (
        settings.smart_stop_loss_pct > 0
        and current_pnl_pct <= -abs(settings.smart_stop_loss_pct)
        and peak_pnl_pct < settings.smart_peak_protect_trigger
        and _position_age_minutes(position) >= settings.smart_stop_loss_min_age_minutes
    ):
        return {
            "reason": "stop_loss",
            "shares": current_shares,
        }
    if peak_pnl_pct >= settings.smart_peak_protect_trigger and current_pnl_pct <= settings.smart_peak_protect_floor:
        return {
            "reason": "peak_profit_protection",
            "shares": current_shares,
        }

    if (
        settings.smart_trailing_stop_arm_pct > 0
        and settings.smart_trailing_stop_giveback_pct > 0
        and peak_pnl_pct >= settings.smart_trailing_stop_arm_pct
        and peak_pnl_pct < settings.smart_peak_protect_trigger
    ):
        giveback_floor = peak_pnl_pct * (1.0 - settings.smart_trailing_stop_giveback_pct)
        if current_pnl_pct <= giveback_floor and current_pnl_pct > 0:
            return {
                "reason": "trailing_stop",
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
    if (
        settings.smart_max_hold_hours > 0
        and _position_age_minutes(position) >= settings.smart_max_hold_hours * 60
    ):
        return {
            "reason": "max_hold_time_reached",
            "shares": current_shares,
        }
    return None


def journal_stats(settings: Settings) -> dict[str, object]:
    path = settings.trade_journal_path
    records: list[dict[str, object]] = []
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    if not records:
        return {"records": 0, "message": "no closed trades yet"}

    def pnl(record: dict[str, object]) -> float:
        try:
            return float(record.get("realized_pnl") or 0)
        except (TypeError, ValueError):
            return 0.0

    def bucket_stats(group: list[dict[str, object]]) -> dict[str, object]:
        if not group:
            return {"count": 0, "total_pnl": 0.0, "avg_pnl": 0.0, "win_rate": 0.0}
        pnls = [pnl(r) for r in group]
        wins = sum(1 for p in pnls if p > 0)
        return {
            "count": len(group),
            "total_pnl": round(sum(pnls), 2),
            "avg_pnl": round(sum(pnls) / len(pnls), 4),
            "win_rate": round(wins / len(group), 3),
        }

    def group_by(records: list[dict[str, object]], key_fn) -> dict[str, dict[str, object]]:
        groups: dict[str, list[dict[str, object]]] = {}
        for record in records:
            key = key_fn(record)
            if key is None:
                continue
            groups.setdefault(str(key), []).append(record)
        return {k: bucket_stats(v) for k, v in sorted(groups.items())}

    def consensus_bucket(record: dict[str, object]) -> str:
        consensus = record.get("consensus")
        if consensus is None:
            return "unknown"
        try:
            consensus_int = int(consensus)
        except (TypeError, ValueError):
            return "unknown"
        if consensus_int <= 1:
            return "1_wallet"
        if consensus_int == 2:
            return "2_wallets"
        if consensus_int == 3:
            return "3_wallets"
        return "4plus_wallets"

    def price_bucket(record: dict[str, object]) -> str:
        try:
            entry = float(record.get("entry_price") or 0)
        except (TypeError, ValueError):
            return "unknown"
        if entry <= 0:
            return "unknown"
        if entry < 0.20:
            return "0.00-0.20"
        if entry < 0.50:
            return "0.20-0.50"
        if entry < 0.80:
            return "0.50-0.80"
        return "0.80-1.00"

    pnls = [pnl(r) for r in records]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    flats = sum(1 for p in pnls if p == 0)
    return {
        "records": len(records),
        "total_pnl": round(sum(pnls), 2),
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate": round(wins / len(records), 3),
        "avg_pnl": round(sum(pnls) / len(records), 4),
        "by_category": group_by(records, lambda r: r.get("category")),
        "by_consensus": group_by(records, consensus_bucket),
        "by_strategy": group_by(records, lambda r: r.get("strategy")),
        "by_exit_reason": group_by(records, lambda r: r.get("exit_reason")),
        "by_entry_price_bucket": group_by(records, price_bucket),
        "suggestions": _journal_suggestions(records),
    }


def _journal_suggestions(records: list[dict[str, object]]) -> list[str]:
    if len(records) < 30:
        return [
            f"only {len(records)} closed trades — need ~30+ before any reading is statistically meaningful; suggestions paused."
        ]
    suggestions: list[str] = []
    by_category: dict[str, list[float]] = {}
    by_consensus: dict[int, list[float]] = {}
    by_exit: dict[str, list[float]] = {}
    for record in records:
        try:
            pnl = float(record.get("realized_pnl") or 0)
        except (TypeError, ValueError):
            continue
        category = str(record.get("category") or "OTHER")
        by_category.setdefault(category, []).append(pnl)
        consensus = record.get("consensus")
        try:
            consensus_int = int(consensus) if consensus is not None else None
        except (TypeError, ValueError):
            consensus_int = None
        if consensus_int is not None:
            by_consensus.setdefault(consensus_int, []).append(pnl)
        exit_reason = str(record.get("exit_reason") or "unknown")
        by_exit.setdefault(exit_reason, []).append(pnl)
    for category, pnls in by_category.items():
        if len(pnls) < 10:
            continue
        avg = sum(pnls) / len(pnls)
        if avg < -0.20:
            suggestions.append(
                f"category {category}: {len(pnls)} trades, avg PnL ${avg:.2f} — consider penalizing or excluding."
            )
    for consensus, pnls in sorted(by_consensus.items()):
        if len(pnls) < 10:
            continue
        avg = sum(pnls) / len(pnls)
        if consensus <= 2 and avg < -0.10:
            suggestions.append(
                f"consensus={consensus}: {len(pnls)} trades, avg PnL ${avg:.2f} — consider raising POLYMARKET_SMART_MIN_CONSENSUS."
            )
    stop_pnls = by_exit.get("stop_loss", [])
    if len(stop_pnls) >= 10:
        share = len(stop_pnls) / len(records)
        if share > 0.30:
            suggestions.append(
                f"stop_loss exits = {share:.0%} of trades — entry filters may be too loose; consider tightening MAX_CHASE_PREMIUM or MAX_RELATIVE_SPREAD."
            )
    if not suggestions:
        suggestions.append("no clear underperformer across buckets with >= 10 trades each.")
    return suggestions


def _append_trade_journal(settings: Settings, position: dict[str, object], reason: str) -> None:
    path = settings.trade_journal_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return
    signal = position.get("signal") if isinstance(position.get("signal"), dict) else {}
    metrics = signal.get("selection_metrics") if isinstance(signal, dict) else {}
    metrics = metrics if isinstance(metrics, dict) else {}
    exits = position.get("exits") if isinstance(position.get("exits"), list) else []
    realized_pnl = float(position.get("realized_pnl") or 0.0)
    entry_price = float(position.get("entry_price") or 0.0)
    initial_shares = float(position.get("initial_shares") or 0.0)
    cost_basis = round(entry_price * initial_shares, 4) if entry_price and initial_shares else 0.0
    pnl_pct = round(realized_pnl / cost_basis, 4) if cost_basis > 0 else None
    record = {
        "closed_at": position.get("closed_at") or utc_now().isoformat(),
        "opened_at": position.get("opened_at"),
        "market_id": position.get("market_id"),
        "question": position.get("question"),
        "outcome": position.get("outcome"),
        "token_id": position.get("token_id"),
        "event_slug": position.get("event_slug"),
        "category": signal.get("category") if isinstance(signal, dict) else None,
        "strategy": position.get("strategy"),
        "exit_reason": reason,
        "entry_price": entry_price,
        "initial_shares": initial_shares,
        "cost_basis": cost_basis,
        "realized_pnl": realized_pnl,
        "pnl_pct": pnl_pct,
        "peak_pnl_pct": position.get("peak_pnl_pct"),
        "consensus": metrics.get("profitable_wallet_count"),
        "copied_usdc": metrics.get("copied_usdc"),
        "avg_copy_price": metrics.get("avg_copy_price"),
        "score": signal.get("score") if isinstance(signal, dict) else None,
        "wallets": signal.get("wallets") if isinstance(signal, dict) else None,
        "exit_count": len(exits),
    }
    try:
        with path.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as exc:
        print(f"⚠️  trade journal write failed: {type(exc).__name__}: {exc}", flush=True)


def _position_age_minutes(position: dict[str, object]) -> float:
    opened_at = parse_dt(str(position.get("opened_at") or ""))
    if opened_at is None:
        return float("inf")
    return max((utc_now() - opened_at).total_seconds() / 60.0, 0.0)


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
        "event_slug": str(item.get("eventSlug") or ""),
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
    if item.get("eventSlug"):
        position["event_slug"] = str(item.get("eventSlug") or "")


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


def smart_money_loop(settings: Settings) -> None:
    strategy_loop(settings, "smart_money", smart_money_once)


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket smart-money copy-trading bot")
    parser.add_argument(
        "command",
        choices=[
            "auto-loop",
            "dashboard",
            "journal-stats",
            "tune-strategy",
            "bootstrap-creds",
            "reset-ledger",
        ],
    )
    args = parser.parse_args()

    settings = Settings()
    if args.command == "auto-loop":
        if not settings.live_trading_enabled:
            raise SystemExit("Live trading is disabled. Set POLYMARKET_ENABLE_LIVE_TRADING=1 to proceed.")
        smart_money_loop(settings)
    elif args.command == "dashboard":
        serve(settings)
    elif args.command == "journal-stats":
        print(json.dumps(journal_stats(settings), indent=2))
    elif args.command == "tune-strategy":
        overrides, journal_size = maybe_tune(settings)
        print(
            json.dumps(
                {
                    "auto_tune_enabled": settings.smart_auto_tune_enabled,
                    "min_trades_required": settings.smart_auto_tune_min_trades,
                    "trades_seen": journal_size,
                    "overrides": overrides,
                    "overrides_path": str(settings.strategy_overrides_path),
                },
                indent=2,
            )
        )
    elif args.command == "bootstrap-creds":
        print(json.dumps(bootstrap_creds(settings), indent=2))
    elif args.command == "reset-ledger":
        print(json.dumps(reset_ledger(settings), indent=2))


if __name__ == "__main__":
    main()
