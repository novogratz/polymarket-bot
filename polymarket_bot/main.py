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

import datetime as dt
import json
import os
import sys
import time

import typer

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import notifications, tick_state
from .auto_tuner import apply_overrides, maybe_tune
from .bitcoin import CoinbaseBtcClient, choose_btc_edge_trade
from .config import Settings
from .dashboard import serve
from .dry_run_cli import app as dry_run_app
from .dry_run_runs import DryRunPaths, ensure_run_directory, update_tick_metadata
from .equity_tracker import append_equity_point
from .profiles import (
    ProfileValidationError,
    apply_profile_to_env,
    load_profile,
    write_snapshot_toml,
)
from .live_confirm import build_live_recap, prompt_live_confirmation
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
from .pricing import ensure_open_positions_in_pool
from .strategy import rank_markets


def _step(settings: Settings, msg: str = "") -> None:
    """Print a progress line unless POLYMARKET_QUIET=1 silences intermediate steps."""
    if not settings.quiet:
        print(msg, flush=True)


def load_candidates(settings: Settings):
    client = GammaClient(settings.gamma_base_url)
    now = utc_now()
    markets = client.get_markets(
        limit=settings.scan_limit,
        end_date_min=now,
        end_date_max=now + timedelta(hours=settings.soon_hours),
    )
    return rank_markets(markets, settings)


def load_btc_candidates(settings: Settings):
    client = GammaClient(settings.gamma_base_url)
    now = utc_now()
    horizon = now + timedelta(hours=settings.soon_hours)
    batches = []
    for kwargs in (
        {
            "limit": settings.scan_limit,
            "end_date_min": now,
            "end_date_max": horizon,
        },
        {
            "limit": settings.scan_limit,
            "order": "volume",
            "ascending": False,
            "end_date_min": now,
            "end_date_max": horizon,
        },
    ):
        try:
            batches.append(client.get_markets(**kwargs))
        except Exception as exc:
            print(f"⚠️  Gamma BTC market batch skipped: {type(exc).__name__}: {exc}")
    keyword_limit = max(20, min(settings.scan_limit, 100))
    for keyword in ("bitcoin", "btc"):
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
            print(f"⚠️  Gamma BTC keyword batch skipped: {keyword} {type(exc).__name__}: {exc}")
    markets_by_id = {
        str(market.get("id") or market.get("conditionId") or index): market
        for index, batch in enumerate(batches)
        for market in batch
    }
    return rank_markets(list(markets_by_id.values()), settings)


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
        if settings.sync_live_positions and settings.funder_address and not settings.dry_run:
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
    if settings.dry_run:
        return
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
    candidates = load_btc_candidates(settings)
    btc_model = CoinbaseBtcClient().model(settings)
    portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)
    pricing_pool = ensure_open_positions_in_pool(settings, portfolio, candidates)
    portfolio.mark_to_market(pricing_pool)

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
    auto_tune_info: dict[str, object] = {
        "applied": False,
        "journal_size": 0,
        "overrides_active": {},
    }
    if settings.smart_auto_tune_enabled:
        overrides, journal_size = maybe_tune(settings)
        auto_tune_info["journal_size"] = journal_size
        auto_tune_info["overrides_active"] = dict(overrides)
        if overrides:
            auto_tune_info["applied"] = True
            print(
                f"   auto-tune: {len(overrides)} override(s) from {journal_size} closed trade(s): {overrides}",
                flush=True,
            )
            settings = apply_overrides(settings, overrides)
        elif journal_size < settings.smart_auto_tune_min_trades and not settings.quiet:
            print(
                f"   auto-tune: paused ({journal_size}/{settings.smart_auto_tune_min_trades} closed trades)",
                flush=True,
            )
    _step(settings, "   loading markets...")
    candidates = load_smart_candidates(settings)
    _step(settings, f"   markets: {len(candidates)} candidates")
    scan_counts = {
        "strict": 0,
        "relaxed": 0,
        "deep": 0,
        "candidates_total": len(candidates),
    }
    portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)
    if settings.dry_run:
        _step(settings, "   [DRY-RUN] skipping live-position sync (using simulated ledger only)")
        sync_report = []
    elif settings.sync_live_positions:
        _step(settings, "   syncing live positions...")
        sync_report = _sync_live_positions(settings, portfolio)
        _step(settings, f"   sync actions: {len(sync_report)}")
    else:
        sync_report = []
    pricing_pool = ensure_open_positions_in_pool(settings, portfolio, candidates)
    portfolio.mark_to_market(pricing_pool)
    open_count = portfolio.summary()["open_positions"]
    _step(settings, f"   open positions: {open_count}")

    client = build_client(settings)
    pending_report = _cancel_stale_pending_orders(client, settings, portfolio)
    if pending_report:
        _step(settings, f"   pending orders cleared: {len(pending_report)}")
    live_open_count = sum(
        1 for p in portfolio.positions if p.get("status") == "open" and p.get("live")
    )
    if settings.smart_cohort_exit_enabled and live_open_count:
        _step(settings, f"   cohort-exit check on {live_open_count} live position(s)...")
    cohort_exit_tokens, whale_exit_report = _detect_cohort_exits(settings, portfolio)
    if cohort_exit_tokens:
        _step(settings, f"   cohort flipped on {len(cohort_exit_tokens)} token(s) -> exit")

    require_saved_api_creds(settings)
    _step(settings, "   running sell strategy...")
    exit_report = _execute_sell_strategy(
        client,
        settings,
        portfolio,
        pricing_pool,
        cohort_exit_tokens=cohort_exit_tokens,
    )
    sells = sum(1 for e in exit_report if e.get("action") == "sell")
    if sells:
        _step(settings, f"   sells executed: {sells}")

    if not settings.dry_run:
        try:
            live_cash = client.live_available_balance()
            portfolio.cash = round(live_cash, 2)
            _step(settings, f"   live cash: ${portfolio.cash:.2f}")
        except Exception as exc:
            print(f"   live cash refresh failed: {type(exc).__name__}: {exc}")
    else:
        _step(settings, f"   [DRY-RUN] cash: ${portfolio.cash:.2f} (simulated ledger)")

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

    _step(settings, f"   smart-money scan over {len(eligible_candidates)} eligible candidate(s)...")
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
                _step(settings, f"   eligible after reverse-lookup: {len(eligible_candidates)} (+{added})")

    report = analyze_smart_money_with_data(eligible_candidates, settings, smart_data)
    _step(settings, f"   strict scan: {len(report.opportunities)} opportunity(ies)")
    scan_counts["strict"] = len(report.opportunities)
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
        _step(settings, f"   relaxed scan: {len(fallback_report.opportunities)} opportunity(ies)")
        scan_counts["relaxed"] = len(fallback_report.opportunities)
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
            _step(settings, f"   deep fallback: {len(deep_report.opportunities)} opportunity(ies)")
            scan_counts["deep"] = len(deep_report.opportunities)
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
        # Gracefully wait if out of funds.
        # In dry-run, trust the local ledger (no live CLOB to query).
        if settings.dry_run:
            live_cash = portfolio.cash
        else:
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
                "scan_counts": scan_counts,
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
                        "question": opportunity.candidate.question,
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
                        "question": opportunity.candidate.question,
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
                        "question": opportunity.candidate.question,
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
                # Stash persistence_score on the just-created position so it
                # propagates to the trade journal on close.
                _persistence_score = max(
                    (
                        smart_data.persistence_signals[w.lower()].persistence_score
                        for w in opportunity.wallets
                        if w.lower() in smart_data.persistence_signals
                    ),
                    default=0.0,
                )
                _new_pos = next(
                    (
                        p
                        for p in portfolio.positions
                        if p.get("status") == "open"
                        and p.get("market_id") == opportunity.candidate.market_id
                    ),
                    None,
                )
                if _new_pos is not None:
                    _new_pos["persistence_score"] = _persistence_score
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
                            "question": opportunity.candidate.question,
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
                            "question": opportunity.candidate.question,
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
                            "question": opportunity.candidate.question,
                            "outcome": opportunity.candidate.outcome,
                            "reason": str(e),
                        }
                    )
                    continue
                if _is_funds_error(str(e)):
                    stop_reason = str(e)
                    break
                try:
                    notifications.notify_error(
                        "order_rejected",
                        str(e)[:500],
                        dedupe_key=f"order_rejected:{opportunity.candidate.token_id}",
                    )
                except Exception:
                    pass
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
                        "question": candidate.question,
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
                    _step(settings, f"   noise fallback skipped: {exc}")
                    continue
                except Exception as exc:
                    if _is_unfilled_market_order_error(str(exc)) or _is_funds_error(str(exc)):
                        continue
                    print(f"   noise fallback error: {type(exc).__name__}: {exc}")
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
        "scan_counts": scan_counts,
        "summary": portfolio.summary(),
        "auto_tune_info": auto_tune_info,
    }
    if settings.btc_edge_integrated:
        try:
            _step(settings, "   running btc-edge tick...")
            response["btc_edge"] = btc_edge_once(settings)
        except Exception as exc:
            print(f"   btc-edge tick failed: {type(exc).__name__}: {exc}")
            response["btc_edge"] = {"error": f"{type(exc).__name__}: {exc}"}

    return response


def _append_dry_run_equity_point(settings: Settings) -> None:
    """Append one equity-curve point to the active dry-run directory and
    bump ``total_ticks`` in its metadata.

    Layout assumption: ``settings.state_path`` lives at
    ``<base>/dry_runs/<run>/state.json``. The run name is the parent
    directory; the base is two levels up. The portfolio is reloaded from
    disk so this function works for any strategy (smart-money, mirror, …)
    as long as the tick persisted the portfolio before returning.
    """
    run_root = settings.state_path.parent
    run_name = run_root.name
    base_dir = run_root.parent.parent
    paths = DryRunPaths.for_run(base_dir, run_name)
    if not paths.metadata.is_file():
        return
    portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)
    open_positions = [
        p for p in portfolio.positions
        if p.get("status") == "open" and float(p.get("stake", 0) or 0) > 0
    ]
    invested = sum(float(p.get("stake", 0) or 0) for p in open_positions)
    unrealized = sum(float(p.get("unrealized_pnl", 0) or 0) for p in open_positions)
    metadata_raw = json.loads(paths.metadata.read_text(encoding="utf-8"))
    tick_idx = int(metadata_raw.get("total_ticks", 0)) + 1
    append_equity_point(
        paths.equity_curve,
        tick=tick_idx,
        cash=float(portfolio.cash),
        invested=invested,
        unrealized=unrealized,
    )
    update_tick_metadata(paths)


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
        if candidate is None or candidate.best_bid is None or candidate.best_bid <= 0:
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
            try:
                notifications.notify_error(
                    "order_rejected",
                    f"SELL skipped: {message}"[:500],
                    dedupe_key=f"order_rejected:{position.get('token_id') or position.get('market_id') or 'unknown'}",
                )
            except Exception:
                pass
            cancelled_ids: list[str] = []
            if "balance is not enough" in message.lower() or "allowance" in message.lower():
                position["sell_blocked_reason"] = "active_sell_order_pending"
                token_id_str = str(position.get("token_id") or "")
                if token_id_str:
                    try:
                        cancelled_ids = client.cancel_active_orders_for_token(token_id_str)
                    except Exception as cancel_exc:
                        print(
                            f"⚠️  cancel attempt failed for {position.get('question')}: {type(cancel_exc).__name__}: {cancel_exc}",
                            flush=True,
                        )
                    if cancelled_ids:
                        print(
                            f"   cancelled {len(cancelled_ids)} resting order(s) on {position.get('question')}; will retry sell next tick",
                            flush=True,
                        )
                        position.pop("sell_blocked_reason", None)
            exit_report.append(
                {
                    "market_id": position.get("market_id"),
                    "question": position.get("question"),
                    "outcome": position.get("outcome"),
                    "action": "skip_sell",
                    "reason": f"{type(exc).__name__}: {message}",
                    "cancelled_orders": cancelled_ids,
                    "pnl_pct": round(current_pnl_pct, 4),
                    "peak_pnl_pct": round(float(position.get("peak_pnl_pct", 0.0)), 4),
                }
            )
            continue

        if str(plan["reason"]).startswith("take_profit_"):
            position.setdefault("sell_tiers_hit", []).append(str(plan["tier"]))
        portfolio.save(settings.state_path)
        if position.get("status") == "closed":
            _append_trade_journal(settings, position, str(plan["reason"]))
        exit_report.append(
            {
                "market_id": position.get("market_id"),
                "question": position.get("question"),
                "outcome": position.get("outcome"),
                "question": position.get("question"),
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
    if not settings.quiet:
        print(
            f"   reverse-lookup: fetching markets for {len(top_tokens)} smart-money token(s) not in scan...",
            flush=True,
        )
    gamma = GammaClient(settings.gamma_base_url)
    try:
        markets = gamma.get_markets_by_clob_token_ids([token for token, _ in top_tokens])
    except Exception as exc:
        print(f"   reverse-lookup failed: {type(exc).__name__}: {exc}")
        return []
    if not markets:
        _step(settings, "   reverse-lookup: 0 markets returned by Gamma (clob_token_ids filter may be unsupported)")
        return []
    smart_settings = replace(
        settings,
        scan_limit=settings.smart_scan_limit,
        soon_hours=settings.smart_soon_hours,
        min_liquidity_usd=min(settings.min_liquidity_usd, settings.smart_reverse_lookup_min_liquidity_usd),
        min_volume_usd=min(settings.min_volume_usd, settings.smart_reverse_lookup_min_volume_usd),
    )
    new_candidates = rank_markets(markets, smart_settings)
    if not settings.quiet:
        print(
            f"   reverse-lookup: gamma returned {len(markets)} market(s), {len(new_candidates)} survived ranking",
            flush=True,
        )
    return new_candidates


def _detect_cohort_exits(
    settings: Settings,
    portfolio: Portfolio,
) -> tuple[dict[str, str], list[dict[str, object]]]:
    from concurrent.futures import ThreadPoolExecutor

    report: list[dict[str, object]] = []
    exit_tokens: dict[str, str] = {}
    if not settings.smart_cohort_exit_enabled or not portfolio.positions:
        return exit_tokens, report
    data_client = DataApiClient(settings.data_api_base_url)
    lookback = int(time.time()) - max(settings.smart_cohort_exit_lookback_minutes, 1) * 60
    min_age = max(settings.smart_cohort_exit_min_age_minutes, 0)
    min_wallets = max(settings.smart_cohort_exit_min_wallets, 1)

    eligible: list[tuple[dict[str, object], str, list[str], set[str]]] = []
    needed_calls: list[tuple[str, str]] = []
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
        eligible.append((position, token_id, wallets, cohort_lower))
        for wallet in wallets:
            needed_calls.append((wallet, "BUY"))
            needed_calls.append((wallet, "SELL"))

    if not eligible:
        return exit_tokens, report

    concurrency = max(1, settings.smart_trade_fetch_concurrency)
    trades_by_call: dict[tuple[str, str], list] = {}
    failures: dict[tuple[str, str], Exception] = {}

    def _fetch(call: tuple[str, str]) -> tuple[tuple[str, str], list, Exception | None]:
        wallet, side = call
        try:
            return call, data_client.trades(user=wallet, start=lookback, side=side), None
        except Exception as exc:
            return call, [], exc

    if concurrency > 1 and len(needed_calls) > 1:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            for call, trades, exc in executor.map(_fetch, needed_calls):
                if exc is not None:
                    failures[call] = exc
                else:
                    trades_by_call[call] = trades
    else:
        for call in needed_calls:
            _, trades, exc = _fetch(call)
            if exc is not None:
                failures[call] = exc
            else:
                trades_by_call[call] = trades

    for position, token_id, wallets, cohort_lower in eligible:
        recent_trades: list = []
        failed = False
        for wallet in wallets:
            buy_call = (wallet, "BUY")
            sell_call = (wallet, "SELL")
            if buy_call in failures or sell_call in failures:
                exc = failures.get(buy_call) or failures.get(sell_call)
                report.append(
                    {
                        "question": position.get("question"),
                        "status": "skipped",
                        "reason": f"cohort_watch_api_error: {type(exc).__name__}: {exc}",
                    }
                )
                failed = True
                break
            recent_trades.extend(trades_by_call.get(buy_call, []))
            recent_trades.extend(trades_by_call.get(sell_call, []))
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


def _ledger_age_seconds(state_path) -> float | None:
    try:
        return max(0.0, time.time() - state_path.stat().st_mtime)
    except (FileNotFoundError, OSError):
        return None


def _humanize_seconds(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds / 60)}m ago"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h ago"
    return f"{seconds / 86400:.1f}d ago"


def _humanize_close_eta(end_iso: str | None, now=None) -> str:
    if not end_iso:
        return "—"
    parsed = parse_dt(end_iso)
    if parsed is None:
        return "—"
    reference = now if now is not None else utc_now()
    delta = (parsed - reference).total_seconds()
    if delta < 0:
        return f"expired {_humanize_seconds(-delta)}"
    if delta < 60:
        return f"in {int(delta)}s"
    if delta < 3600:
        return f"in {int(delta / 60)}m"
    if delta < 86400:
        return f"in {delta / 3600:.1f}h"
    return f"in {delta / 86400:.1f}d"


def status_summary(settings: Settings) -> dict[str, object]:
    """Snapshot rapide du bot : mode, ledger, journal. Read-only."""
    portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)
    summary = portfolio.summary()
    age = _ledger_age_seconds(settings.state_path)
    journal_records = 0
    journal_path = settings.trade_journal_path
    if journal_path.exists():
        try:
            journal_records = sum(1 for line in journal_path.read_text().splitlines() if line.strip())
        except OSError:
            journal_records = 0
    if settings.dry_run:
        mode = "dry-run"
    elif settings.live_trading_enabled:
        mode = "live"
    else:
        mode = "disabled"
    return {
        "mode": mode,
        "quiet": settings.quiet,
        "state_path": str(settings.state_path),
        "state_exists": settings.state_path.exists(),
        "ledger_age_seconds": age,
        "journal_path": str(journal_path),
        "journal_records": journal_records,
        "cash": summary["cash"],
        "invested": summary["invested"],
        "unrealized_pnl": summary["unrealized_pnl"],
        "equity": summary["equity"],
        "open_positions": summary["open_positions"],
    }


def print_status(settings: Settings) -> dict[str, object]:
    from . import _ui

    snapshot = status_summary(settings)
    mode = snapshot["mode"]
    if mode == "live":
        mode_label = _ui.red(_ui.bold("LIVE"))
    elif mode == "dry-run":
        mode_label = _ui.yellow(_ui.bold("DRY-RUN"))
    else:
        mode_label = _ui.dim("disabled")
    typer.echo(f"{_ui.bold('Mode:')}        {mode_label}{' ' + _ui.dim('(quiet)') if snapshot['quiet'] else ''}")
    typer.echo(f"{_ui.bold('Ledger:')}      {snapshot['state_path']}")
    if snapshot["state_exists"]:
        age = snapshot["ledger_age_seconds"]
        age_label = _humanize_seconds(age) if age is not None else "?"
        typer.echo(f"             last write {_ui.dim(age_label)}")
    else:
        typer.echo(f"             {_ui.dim('not yet created')}")
    journal_records = snapshot["journal_records"]
    typer.echo(f"{_ui.bold('Journal:')}     {snapshot['journal_path']} {_ui.dim(f'({journal_records} closed trades)')}")
    typer.echo("")
    cash = float(snapshot["cash"])
    invested = float(snapshot["invested"])
    unrealized = float(snapshot["unrealized_pnl"])
    equity = float(snapshot["equity"])
    typer.echo(f"  Cash         ${cash:>9.2f}")
    typer.echo(f"  Invested     ${invested:>9.2f}")
    typer.echo(f"  Unrealized   {_ui.colorize_pnl(unrealized):>17}")
    typer.echo(f"  {_ui.bold('Equity')}       ${equity:>9.2f}")
    typer.echo(f"  Positions    {snapshot['open_positions']:>9}")
    return snapshot


def format_positions_table(settings: Settings) -> str:
    """Format les positions ouvertes en table CLI alignée. Retourne une chaîne prête à imprimer."""
    from . import _ui

    portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)
    open_positions = [p for p in portfolio.positions if p.get("status") == "open"]
    if not open_positions:
        return _ui.dim("no open positions")

    def _row_pnl(position: dict) -> float:
        try:
            return float(position.get("unrealized_pnl") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    rows = sorted(open_positions, key=_row_pnl, reverse=True)
    now = utc_now()
    headers = ("Market", "Outcome", "Stake", "Entry", "Now", "PnL", "Return", "Closes")
    lines: list[tuple[str, str, str, str, str, str, str, str]] = []
    for position in rows:
        question = str(position.get("question") or position.get("slug") or position.get("market_id") or "?")
        market = question if len(question) <= 50 else question[:47] + "..."
        outcome = str(position.get("outcome") or "")
        stake = f"${float(position.get('stake') or 0):.2f}"
        entry = f"{float(position.get('entry_price') or 0):.3f}"
        now_price = f"{float(position.get('current_price') or 0):.3f}"
        pnl = float(position.get("unrealized_pnl") or 0.0)
        entry_price = float(position.get("entry_price") or 0.0)
        current = float(position.get("current_price") or 0.0)
        ret = ((current - entry_price) / entry_price) if entry_price > 0 else 0.0
        closes = _humanize_close_eta(position.get("end_date"), now=now)
        lines.append((
            market,
            outcome,
            stake,
            entry,
            now_price,
            _ui.colorize_pnl(pnl),
            _ui.colorize_pct(ret),
            closes,
        ))

    def _visible_width(text: str) -> int:
        # Strip ANSI escape sequences for column-width math.
        import re as _re
        return len(_re.sub(r"\x1b\[[0-9;]*m", "", text))

    widths = [_visible_width(h) for h in headers]
    for row in lines:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _visible_width(cell))

    def _pad(cell: str, width: int, *, right: bool = False) -> str:
        gap = width - _visible_width(cell)
        return (" " * gap + cell) if right else (cell + " " * gap)

    right_align = {2, 3, 4, 5, 6}
    header_cells = [_pad(h, widths[i], right=(i in right_align)) for i, h in enumerate(headers)]
    out: list[str] = [_ui.bold("  ".join(header_cells))]
    out.append(_ui.dim("  ".join("-" * w for w in widths)))
    for row in lines:
        cells = [_pad(c, widths[i], right=(i in right_align)) for i, c in enumerate(row)]
        out.append("  ".join(cells))
    return "\n".join(out)


def print_positions(settings: Settings) -> None:
    typer.echo(format_positions_table(settings))


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
        return {
            "records": 0,
            "message": "no closed trades yet",
            "journal_path": str(path),
            "dry_run": settings.dry_run,
        }

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
    records_sorted = sorted(records, key=lambda r: str(r.get("closed_at") or ""))
    running_pnl = 0.0
    peak_pnl = 0.0
    max_drawdown = 0.0
    for record in records_sorted:
        running_pnl += pnl(record)
        peak_pnl = max(peak_pnl, running_pnl)
        max_drawdown = min(max_drawdown, running_pnl - peak_pnl)
    return {
        "records": len(records),
        "total_pnl": round(sum(pnls), 2),
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate": round(wins / len(records), 3),
        "avg_pnl": round(sum(pnls) / len(records), 4),
        "max_drawdown": round(max_drawdown, 2),
        "by_category": group_by(records, lambda r: r.get("category")),
        "by_consensus": group_by(records, consensus_bucket),
        "by_strategy": group_by(records, lambda r: r.get("strategy")),
        "by_exit_reason": group_by(records, lambda r: r.get("exit_reason")),
        "by_entry_price_bucket": group_by(records, price_bucket),
        "suggestions": _journal_suggestions(records),
        "journal_path": str(path),
        "dry_run": settings.dry_run,
    }


def _journal_suggestions(records: list[dict[str, object]]) -> list[dict[str, object]]:
    if len(records) < 30:
        return [{
            "id": "below_min_trades",
            "param": None,
            "ratio": None,
            "records": len(records),
            "reason": (
                f"only {len(records)} closed trades — need ~30+ before any reading is "
                "statistically meaningful; suggestions paused."
            ),
        }]
    suggestions: list[dict[str, object]] = []
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
            suggestions.append({
                "id": f"weak_category_{category.lower()}",
                "param": "category_filter",
                "ratio": None,
                "category": category,
                "reason": (
                    f"category {category}: {len(pnls)} trades, avg PnL ${avg:.2f} — "
                    "consider penalizing or excluding."
                ),
            })

    for consensus, pnls in sorted(by_consensus.items()):
        if len(pnls) < 10:
            continue
        avg = sum(pnls) / len(pnls)
        if consensus <= 2 and avg < -0.10:
            suggestions.append({
                "id": f"weak_consensus_{consensus}",
                "param": "MIN_CONSENSUS",
                "ratio": None,
                "consensus": consensus,
                "reason": (
                    f"consensus={consensus}: {len(pnls)} trades, avg PnL ${avg:.2f} — "
                    "consider raising POLYMARKET_SMART_MIN_CONSENSUS."
                ),
            })

    stop_pnls = by_exit.get("stop_loss", [])
    if len(stop_pnls) >= 10:
        share = len(stop_pnls) / len(records)
        if share > 0.30:
            suggestions.append({
                "id": "excessive_stop_loss",
                "param": "MAX_CHASE_PREMIUM",
                "ratio": 0.80,
                "share": round(share, 3),
                "reason": (
                    f"stop_loss exits = {share:.0%} of {len(records)} trades — entry filters may be "
                    "too loose; consider tightening MAX_CHASE_PREMIUM or MAX_RELATIVE_SPREAD."
                ),
            })

    if not suggestions:
        suggestions.append({
            "id": "all_clear",
            "param": None,
            "ratio": None,
            "reason": "no clear underperformer across buckets with >= 10 trades each.",
        })
    return suggestions


def format_suggestions(suggestions: list[dict[str, object]]) -> list[str]:
    """Render structured suggestions into human-readable lines for the CLI."""
    lines: list[str] = []
    for suggestion in suggestions:
        reason = str(suggestion.get("reason") or "")
        param = suggestion.get("param")
        ratio = suggestion.get("ratio")
        if param and ratio is not None:
            lines.append(f"[{param} ×{ratio:.2f}] {reason}")
        elif param:
            lines.append(f"[{param}] {reason}")
        else:
            lines.append(reason)
    return lines


def _journal_stats_last_24h(
    path: Path,
) -> tuple[int, int, int, dict | None, dict | None, float]:
    """Lit le journal et retourne (trades, wins, losses, top_winner, top_loser, realized_pnl).

    ``realized_pnl`` est la somme des PnL réalisés sur les 24 dernières heures.
    """
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
    trades: list[dict] = []
    try:
        with path.open(encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_raw = rec.get("closed_at") or rec.get("ts")
                if not ts_raw:
                    continue
                try:
                    ts = dt.datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=dt.timezone.utc)
                if ts >= cutoff:
                    trades.append(rec)
    except (FileNotFoundError, OSError):
        return 0, 0, 0, None, None, 0.0

    if not trades:
        return 0, 0, 0, None, None, 0.0

    wins = sum(1 for r in trades if float(r.get("realized_pnl", 0)) > 0)
    losses = sum(1 for r in trades if float(r.get("realized_pnl", 0)) < 0)
    realized_total = sum(float(r.get("realized_pnl", 0) or 0) for r in trades)
    top_w_rec = max(trades, key=lambda r: float(r.get("realized_pnl", 0)))
    top_l_rec = min(trades, key=lambda r: float(r.get("realized_pnl", 0)))

    def _shape(r: dict) -> dict | None:
        pnl = float(r.get("realized_pnl", 0))
        title = r.get("title") or r.get("market_title") or r.get("question") or ""
        return {"title": str(title), "pnl_usd": pnl}

    top_w = _shape(top_w_rec) if float(top_w_rec.get("realized_pnl", 0)) > 0 else None
    top_l = _shape(top_l_rec) if float(top_l_rec.get("realized_pnl", 0)) < 0 else None

    return len(trades), wins, losses, top_w, top_l, realized_total


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
        "persistence_score": float(position.get("persistence_score") or 0.0),
        "exit_count": len(exits),
    }
    try:
        with path.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as exc:
        print(f"⚠️  trade journal write failed: {type(exc).__name__}: {exc}")


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
    end_date_raw = item.get("endDate") or item.get("eventEndDate") or item.get("endDateIso")
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
        "end_date": str(end_date_raw) if end_date_raw else None,
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
    if item.get("slug"):
        position["slug"] = str(item.get("slug") or "")
    event_slug = str(position.get("event_slug") or item.get("eventSlug") or "")
    slug = str(position.get("slug") or item.get("slug") or "")
    if event_slug or slug:
        position["url"] = f"https://polymarket.com/event/{event_slug or slug}"


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


def _all_time_realized_pnl(journal_path: Path) -> tuple[float, int, int, int]:
    """Sum realized PnL across every closed trade in the journal.

    Returns ``(realized_total, closed_count, wins, losses)``. Safe on
    missing/malformed files: returns zeros instead of raising.
    """
    if not journal_path.is_file():
        return 0.0, 0, 0, 0
    total = 0.0
    closed = 0
    wins = 0
    losses = 0
    try:
        with journal_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                closed += 1
                try:
                    pnl = float(rec.get("realized_pnl", 0) or 0)
                except (TypeError, ValueError):
                    pnl = 0.0
                total += pnl
                if pnl > 0:
                    wins += 1
                elif pnl < 0:
                    losses += 1
    except Exception:
        return 0.0, 0, 0, 0
    return total, closed, wins, losses


def _print_stdout_heartbeat(
    settings: Settings,
    *,
    strategy_name: str,
    summary: dict[str, object],
    trades_24h: int,
    wins_24h: int,
    losses_24h: int,
    realized_24h: float,
) -> None:
    """Multi-line portfolio snapshot to stdout.

    Triggered from ``strategy_loop`` at most every
    ``settings.stdout_heartbeat_minutes`` minutes. Independent of the
    Telegram heartbeat — both can be enabled at once.
    """
    equity = float(summary.get("equity", 0) or 0)
    cash = float(summary.get("cash", 0) or 0)
    invested = float(summary.get("invested", 0) or 0)
    unrealized = float(summary.get("unrealized_pnl", 0) or 0)
    positions = int(summary.get("open_positions", 0) or 0)
    cash_pct = (cash / equity * 100.0) if equity > 0 else 0.0
    win_rate = (wins_24h / trades_24h * 100.0) if trades_24h > 0 else 0.0
    realized_all, closed_all, wins_all, losses_all = _all_time_realized_pnl(
        settings.trade_journal_path
    )
    net_since_start = realized_all + unrealized
    win_rate_all = (wins_all / closed_all * 100.0) if closed_all > 0 else 0.0
    stamp = time.strftime("%H:%M:%S", time.localtime())
    mode = "DRY-RUN" if settings.dry_run else "LIVE"
    sep = "─" * 64
    print(sep, flush=True)
    print(f"📊 PORTFOLIO HEARTBEAT · {stamp} · {strategy_name} · {mode}", flush=True)
    print(
        f"   equity ${equity:.2f}  |  cash ${cash:.2f} ({cash_pct:.0f}%)  |  invested ${invested:.2f}",
        flush=True,
    )
    print(f"   open positions: {positions}  |  unrealized PnL: {unrealized:+.2f}", flush=True)
    print(
        f"   since start: realized {realized_all:+.2f} + unrealized {unrealized:+.2f} = "
        f"net {net_since_start:+.2f} ({closed_all} closed, {wins_all}W/{losses_all}L, "
        f"{win_rate_all:.0f}% win rate)",
        flush=True,
    )
    if trades_24h > 0:
        print(
            f"   last 24h: {trades_24h} closed  |  {wins_24h}W/{losses_24h}L "
            f"({win_rate:.0f}% win rate)  |  realized {realized_24h:+.2f}",
            flush=True,
        )
    else:
        print("   last 24h: no closed trades yet", flush=True)
    print(sep, flush=True)


def strategy_loop(settings: Settings, strategy_name: str, tick_fn) -> None:
    last_heartbeat_ts: float = 0.0
    tick = 0
    while settings.auto_max_ticks <= 0 or tick < settings.auto_max_ticks:
        tick += 1
        started_at = utc_now()
        error: dict[str, str] | None = None
        tick_result: dict[str, object] = {}
        try:
            tick_result = tick_fn(settings)
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            try:
                notifications.notify_error(
                    "tick_failed",
                    str(exc)[:500],
                    dedupe_key="tick_failed",
                )
            except Exception:
                pass

        finished_at = utc_now()
        result: dict[str, object] = {
            "tick": tick,
            "strategy": strategy_name,
            "started_at": started_at.isoformat(),
        }
        if error is None:
            result["result"] = tick_result
        else:
            result["error"] = error

        if settings.dry_run and error is None:
            try:
                _append_dry_run_equity_point(settings)
            except Exception as exc:
                print(f"   equity-tracker append failed: {type(exc).__name__}: {exc}")

        try:
            tick_state.write_tick(
                settings,
                _build_tick_record(
                    tick_id=tick,
                    started_at=started_at,
                    finished_at=finished_at,
                    settings=settings,
                    tick_result=tick_result,
                    error=error,
                ),
            )
        except Exception:
            pass

        if settings.quiet:
            from . import _ui
            print(_ui.format_tick_footer(result, settings), flush=True)
        else:
            print(json.dumps(result, indent=2), flush=True)

        # Hooks post-tick best-effort: drawdown, equity floor, résumé quotidien.
        try:
            tick_payload = result.get("result")
            summary_snap = (
                tick_payload.get("summary")
                if isinstance(tick_payload, dict)
                else None
            )
            if not isinstance(summary_snap, dict):
                summary_snap = {}
            equity_val = float(summary_snap.get("equity", 0) or 0)
            cash_val = float(summary_snap.get("cash", 0) or 0)
            unrealized_val = float(summary_snap.get("unrealized_pnl", 0) or 0)
            open_positions_count = int(summary_snap.get("open_positions", 0) or 0)
            notifications.notify_threshold("drawdown", {"equity_usd": equity_val})
            notifications.notify_threshold(
                "equity_floor",
                {
                    "equity_usd": equity_val,
                    "open_positions": open_positions_count,
                    "cash_usd": cash_val,
                },
            )
            trades_24h, wins_24h, losses_24h, top_w, top_l, realized_24h = (
                _journal_stats_last_24h(settings.trade_journal_path)
            )
            notifications.notify_heartbeat(
                {
                    "equity_usd": equity_val,
                    "cash_usd": cash_val,
                    "unrealized_pnl_usd": unrealized_val,
                    "open_positions": open_positions_count,
                    "trades_24h": trades_24h,
                    "wins_24h": wins_24h,
                    "losses_24h": losses_24h,
                    "realized_pnl_24h_usd": realized_24h,
                    "top_winner": top_w,
                    "top_loser": top_l,
                }
            )
            if settings.stdout_heartbeat_minutes > 0:
                now_ts = time.time()
                if now_ts - last_heartbeat_ts >= settings.stdout_heartbeat_minutes * 60:
                    _print_stdout_heartbeat(
                        settings,
                        strategy_name=strategy_name,
                        summary=summary_snap,
                        trades_24h=trades_24h,
                        wins_24h=wins_24h,
                        losses_24h=losses_24h,
                        realized_24h=realized_24h,
                    )
                    last_heartbeat_ts = now_ts
        except Exception as exc:
            print(f"[notif] post-tick hook failed: {exc}", file=sys.stderr, flush=True)

        if settings.auto_max_ticks > 0 and tick >= settings.auto_max_ticks:
            break
        time.sleep(settings.auto_interval_seconds)


def _build_tick_record(
    *,
    tick_id: int,
    started_at: datetime,
    finished_at: datetime,
    settings: Settings,
    tick_result: dict[str, object],
    error: dict[str, str] | None,
) -> dict[str, object]:
    """Re-shape a tick_fn return into the tick_state record format."""
    duration_s = round((finished_at - started_at).total_seconds(), 2)
    next_tick_at = (
        finished_at.timestamp() + max(0, settings.auto_interval_seconds)
    )
    record: dict[str, object] = {
        "tick_id": tick_id,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_s": duration_s,
        "mode": "dry_run" if settings.dry_run else "live",
        "scan_counts": dict(tick_result.get("scan_counts") or {}),
        "actions": _extract_tick_actions(tick_result),
        "tuner_changes": dict(tick_result.get("auto_tune_info") or {}),
        "next_tick_at": datetime.fromtimestamp(
            next_tick_at, tz=timezone.utc
        ).isoformat(),
    }
    if error is not None:
        record["error"] = error
    return record


def _extract_tick_actions(tick_result: dict[str, object]) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    primary = tick_result.get("trade")
    if isinstance(primary, dict):
        signal = primary.get("signal") if isinstance(primary.get("signal"), dict) else {}
        question = signal.get("question") if signal else None
        if question:
            actions.append({
                "type": "buy",
                "market": question,
                "amount_usd": signal.get("stake_usd"),
                "strategy": primary.get("strategy"),
                "reason": signal.get("selection_reason") or "smart_money_signal",
            })
    btc = tick_result.get("btc_edge")
    if isinstance(btc, dict):
        btc_trade = btc.get("trade")
        if isinstance(btc_trade, dict):
            signal = btc_trade.get("signal") if isinstance(btc_trade.get("signal"), dict) else {}
            question = signal.get("question")
            if question:
                actions.append({
                    "type": "buy",
                    "market": question,
                    "amount_usd": signal.get("stake_usd"),
                    "strategy": "btc_edge",
                    "reason": signal.get("selection_reason") or "btc_edge_signal",
                })
    for noise in tick_result.get("noise_trades") or []:
        if isinstance(noise, dict):
            noise_signal = noise.get("signal") if isinstance(noise.get("signal"), dict) else {}
            question = noise_signal.get("question") or noise.get("question")
            if question:
                actions.append({
                    "type": "buy",
                    "market": question,
                    "amount_usd": noise_signal.get("stake_usd"),
                    "strategy": "noise_fallback",
                    "reason": "noise_fallback",
                })
    for exit_record in tick_result.get("exits") or []:
        if isinstance(exit_record, dict) and exit_record.get("action") == "sell":
            actions.append({
                "type": "sell",
                "market": exit_record.get("question") or exit_record.get("market_id"),
                "reason": exit_record.get("reason"),
            })
    for rejected in tick_result.get("rejected_signals") or []:
        if isinstance(rejected, dict):
            actions.append({
                "type": "skip",
                "market": rejected.get("question") or rejected.get("market_id"),
                "reason": rejected.get("reason") or rejected.get("selection_reason"),
            })
    return actions


def smart_money_loop(settings: Settings) -> None:
    strategy_loop(settings, "smart_money", smart_money_once)


def _mask(value: str | None, keep: int = 4) -> str:
    if not value:
        return "(missing)"
    if len(value) <= keep * 2:
        return "***"
    return f"{value[:keep]}...{value[-keep:]}"


def run_doctor(settings: Settings) -> dict[str, object]:
    """Read-only health check: validates .env, auth, endpoints, local state.

    Posts no orders. Safe to run with or without live trading enabled.
    """
    from pathlib import Path

    from eth_account import Account

    from . import _ui

    print(_ui.bold("=== .env ==="))
    pk = settings.private_key or ""
    pk_ok = len(pk) == 66 and pk.startswith("0x")
    pk_marker = _ui.ok() if pk_ok else _ui.ko()
    pk_detail = "(0x + 64 hex)" if pk_ok else "(expected 66 chars: 0x + 64 hex)"
    print(f"  {pk_marker} PRIVATE_KEY    len={len(pk)} {_ui.dim(pk_detail)}")

    fa = settings.funder_address or ""
    fa_ok = len(fa) == 42 and fa.startswith("0x")
    fa_marker = _ui.ok() if fa_ok else _ui.ko()
    fa_detail = fa if fa_ok else "(invalid)"
    print(f"  {fa_marker} FUNDER_ADDRESS len={len(fa)} value={_ui.dim(fa_detail)}")

    api_complete = bool(settings.api_key and settings.api_secret and settings.api_passphrase)
    for name in ("api_key", "api_secret", "api_passphrase"):
        val = getattr(settings, name)
        marker = _ui.ok() if val else _ui.ko()
        print(f"  {marker} {name.upper():15} {_ui.dim(_mask(val))}")

    sig_label = {0: "EOA", 1: "Magic.link proxy", 2: "Gnosis Safe"}.get(settings.signature_type, "?")
    print(f"  {_ui.dim('·')} SIGNATURE_TYPE {settings.signature_type} ({sig_label})")
    if settings.live_trading_enabled:
        print(f"  {_ui.warn()} LIVE_TRADING   {_ui.bold(_ui.red('ENABLED'))} — bot will place real orders if auto-loop runs")
    else:
        print(f"  {_ui.ok()} LIVE_TRADING   {_ui.dim('disabled (safe — no orders will be placed)')}")
    if settings.dry_run:
        print(f"  {_ui.ok()} DRY_RUN        {_ui.bold(_ui.yellow('ENABLED'))} — simulated orders only, ledger at {settings.state_path}")
    print()

    print(_ui.bold("=== Auth & balance ==="))
    if pk_ok:
        try:
            eoa = Account.from_key(pk).address
            print(f"  {_ui.ok()} EOA derived    {eoa}")
        except Exception as exc:
            print(f"  {_ui.ko()} EOA derived    {type(exc).__name__}: {exc}")
    else:
        print(f"  {_ui.skip()} EOA derived    {_ui.dim('skipped (invalid private key)')}")
    print(f"  {_ui.dim('·')} Funder         {fa or _ui.dim('(missing)')}")

    balance: float | None = None
    if pk_ok and fa_ok and api_complete:
        try:
            client = build_client(settings)
            balance = client.live_available_balance()
            print(f"  {_ui.ok()} USDC balance   ${balance:.4f}")
        except Exception as exc:
            print(f"  {_ui.ko()} USDC balance   {type(exc).__name__}: {str(exc)[:120]}")
    else:
        print(f"  {_ui.skip()} USDC balance   {_ui.dim('skipped (credentials incomplete — run bootstrap-creds)')}")
    print()

    print(_ui.bold("=== Endpoints ==="))
    endpoint_results: dict[str, str] = {}
    for label, fn in [
        ("Gamma   (markets)   ", lambda: GammaClient(settings.gamma_base_url).get_markets(limit=1)),
        ("DataAPI (leaderboard)", lambda: DataApiClient(settings.data_api_base_url).leaderboard(
            category=(settings.smart_categories.split(",")[0].strip() if settings.smart_categories else "OVERALL"),
            time_period=settings.smart_time_period,
            limit=1,
        )),
    ]:
        t0 = time.time()
        try:
            fn()
            elapsed_ms = (time.time() - t0) * 1000
            print(f"  {_ui.ok()} {label} {_ui.dim(f'{elapsed_ms:.0f}ms')}")
            endpoint_results[label.strip()] = "ok"
        except Exception as exc:
            print(f"  {_ui.ko()} {label} {type(exc).__name__}: {str(exc)[:80]}")
            endpoint_results[label.strip()] = "error"
    print()

    print(_ui.bold("=== Local state ==="))
    for path_attr, label in [
        ("state_path", "paper_state.json       "),
        ("trade_journal_path", "trade_journal.jsonl    "),
        ("strategy_overrides_path", "strategy_overrides.json"),
    ]:
        p = Path(getattr(settings, path_attr))
        if p.exists():
            print(f"  {_ui.ok()} {label} {_ui.dim(f'exists ({p.stat().st_size} bytes) at {p}')}")
        else:
            print(f"  {_ui.skip()} {label} {_ui.dim(f'not yet created at {p}')}")
    print()

    setup_ok = pk_ok and fa_ok and api_complete and all(v == "ok" for v in endpoint_results.values())
    if not setup_ok:
        print(f"{_ui.bold('Verdict:')} {_ui.ko()} Setup incomplete — fix the items marked above.")
    elif settings.dry_run:
        print(f"{_ui.bold('Verdict:')} {_ui.ok()} {_ui.green('READY for DRY-RUN.')} auto-loop will simulate orders without spending any cash.")
    elif settings.live_trading_enabled:
        print(f"{_ui.bold('Verdict:')} {_ui.warn()} {_ui.yellow('READY for LIVE trading.')} Bot WILL place real orders if auto-loop runs.")
    else:
        print(f"{_ui.bold('Verdict:')} {_ui.ok()} {_ui.green('READY for read-only / dashboard.')} Set POLYMARKET_ENABLE_LIVE_TRADING=1 to enable live trading (or POLYMARKET_DRY_RUN=1 to simulate).")

    return {
        "private_key_ok": pk_ok,
        "funder_ok": fa_ok,
        "api_credentials_complete": api_complete,
        "live_trading_enabled": settings.live_trading_enabled,
        "dry_run": settings.dry_run,
        "balance_usd": balance,
        "endpoints": endpoint_results,
        "setup_ok": setup_ok,
    }


app = typer.Typer(
    name="pmbot",
    no_args_is_help=True,
    add_completion=False,
    help="Polymarket smart-money copy-trading bot.",
)
app.add_typer(dry_run_app, name="dry-run")


@app.command("list")
def cmd_list_all(
    all_: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="Inclure les runs sans activité (jamais lancés ou reset non relancés).",
    ),
) -> None:
    """Lister le ledger live + tous les runs dry-run.

    Alias top-level de ``pmbot dry-run list`` — ce dernier reste valable
    pour rétro-compatibilité. La sortie inclut une ligne ``(live)`` quand
    ``data/paper_state.json`` existe, suivie des runs nommés.
    """
    from .dry_run_cli import cmd_list as _cmd_list

    _cmd_list(all_=all_)


def _version_callback(value: bool) -> None:
    if value:
        from . import __version__

        typer.echo(f"pmbot {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the pmbot version and exit.",
    ),
) -> None:
    """Polymarket smart-money copy-trading bot."""


@app.command("auto-loop")
def cli_auto_loop(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Mode simulation: aucun ordre envoyé à Polymarket."
    ),
    live: bool = typer.Option(
        False, "--live", help="Mode trading réel. Demande confirmation interactive."
    ),
    profile: str = typer.Option(
        "baseline",
        "--profile",
        help="Nom du profil TOML dans configs/profiles/ (sans extension).",
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Skip le prompt de confirmation live."
    ),
    run: str = typer.Option(
        "default",
        "--run",
        help="Nom du dossier de simulation dans data/dry_runs/. Dry-run only.",
    ),
    no_persistence: bool = typer.Option(
        False,
        "--no-persistence",
        help="Désactive le filtre de persistance d'edge (pour A/B test).",
    ),
) -> None:
    """Run the strategy loop in --dry-run or --live mode.

    The strategy is selected by ``[run].mode`` in the profile:
    ``smart_money`` (default) or ``mirror`` (copy-trade a single wallet
    configured in the ``[mirror]`` section).
    """

    if no_persistence:
        os.environ["POLYMARKET_PERSISTENCE_ENABLED"] = "false"

    # 1) Validate mode flags.
    if dry_run and live:
        typer.echo("--dry-run and --live are mutually exclusive.", err=True)
        raise typer.Exit(code=2)
    if not dry_run and not live:
        typer.echo(
            "Specify --dry-run or --live (modes are mutually exclusive and required).",
            err=True,
        )
        raise typer.Exit(code=2)
    if live and run != "default":
        typer.echo("--run is dry-run only.", err=True)
        raise typer.Exit(code=2)

    # 2) Warn on legacy env vars (no longer used to drive mode).
    if os.getenv("POLYMARKET_DRY_RUN") or os.getenv("POLYMARKET_ENABLE_LIVE_TRADING"):
        typer.echo(
            "⚠ POLYMARKET_DRY_RUN / POLYMARKET_ENABLE_LIVE_TRADING are no longer used. "
            "Pass --dry-run or --live instead.",
            err=True,
        )

    # 3) Load profile, apply to env (env vars already set take precedence).
    repo_root = Path(__file__).resolve().parent.parent
    profile_path = repo_root / "configs" / "profiles" / f"{profile}.toml"
    try:
        loaded = load_profile(profile_path)
    except ProfileValidationError as exc:
        typer.echo(f"profile error: {exc}", err=True)
        raise typer.Exit(code=2)
    apply_profile_to_env(loaded)

    # 4) Dry-run: provision the named run directory and inject paths into env
    #    BEFORE Settings() reads them (so every component points to the run dir).
    paths: DryRunPaths | None = None
    if dry_run:
        try:
            paths = ensure_run_directory(
                repo_root / "data",
                run,
                starting_cash=loaded.starting_cash,
                profile_source=profile_path.name,
            )
        except ValueError as exc:
            typer.echo(f"invalid --run name: {exc}", err=True)
            raise typer.Exit(code=2)
        os.environ["POLYMARKET_STATE_PATH"] = str(paths.state)
        os.environ["POLYMARKET_TRADE_JOURNAL_PATH"] = str(paths.journal)
        os.environ["POLYMARKET_STRATEGY_OVERRIDES_PATH"] = str(paths.overrides)
        os.environ["POLYMARKET_TICK_STATE_PATH"] = str(paths.tick_state)
        os.environ["POLYMARKET_TICK_HISTORY_PATH"] = str(paths.tick_history)
        os.environ["POLYMARKET_DRY_RUN"] = "1"
        os.environ["POLYMARKET_RUN_NAME"] = run
    else:
        os.environ.pop("POLYMARKET_DRY_RUN", None)
        os.environ["POLYMARKET_RUN_NAME"] = "live"

    settings = Settings()

    # 5) Snapshot effective config.
    if dry_run and paths is not None:
        snapshot_target = paths.config_snapshot
    else:
        snapshot_target = settings.state_path.parent / "live_config_snapshot.toml"
    write_snapshot_toml(snapshot_target, source_label=profile_path.name)

    # 6) Live: confirmation gate.
    if live:
        recap = build_live_recap(settings, profile_label=profile_path.name)
        approved = prompt_live_confirmation(recap_text=recap, skip=yes)
        if not approved:
            typer.echo("Live launch aborted.", err=True)
            raise typer.Exit(code=1)
        typer.echo(f"LIVE — profile={profile_path.name} ledger={settings.state_path}", err=True)
    else:
        typer.echo(
            f"[DRY-RUN] run={run} profile={profile_path.name} "
            f"ledger={settings.state_path} starting_cash=${loaded.starting_cash:g}",
            err=True,
        )

    mode = (settings.run_mode or "smart_money").lower()
    if mode == "mirror":
        from . import mirror as mirror_module

        if not settings.mirror_target:
            typer.echo(
                "mirror mode requires [mirror].target in the profile (or POLYMARKET_MIRROR_TARGET).",
                err=True,
            )
            raise typer.Exit(code=2)
        mirror_module.mirror_loop(settings)
    elif mode == "news":
        from . import news_strategy as news_module

        news_module.news_loop(settings)
    elif mode == "edge":
        from . import edge_strategy as edge_module

        edge_module.edge_loop(settings)
    else:
        smart_money_loop(settings)


@app.command()
def dashboard(
    run: str | None = typer.Option(
        None, "--run", help="Cibler un run dry-run nommé (data/dry_runs/<run>/)."
    ),
    port: int | None = typer.Option(
        None, "--port", help="Override le port du dashboard (défaut 8765)."
    ),
) -> None:
    """Serve the read-only HTML dashboard.

    Sans flag : lit le ledger live (data/paper_state.json) sur :8765.
    Avec --run X : lit data/dry_runs/X/state.json + journal du run.
    """
    if run is not None:
        from polymarket_bot.dry_run_runs import DryRunPaths
        repo_data = Path(__file__).resolve().parent.parent / "data"
        paths = DryRunPaths.for_run(repo_data, run)
        if not paths.metadata.is_file():
            typer.echo(f"run '{run}' not found in {paths.root}", err=True)
            raise typer.Exit(code=1)
        os.environ["POLYMARKET_STATE_PATH"] = str(paths.state)
        os.environ["POLYMARKET_TRADE_JOURNAL_PATH"] = str(paths.journal)
        os.environ["POLYMARKET_STRATEGY_OVERRIDES_PATH"] = str(paths.overrides)
        os.environ["POLYMARKET_TICK_STATE_PATH"] = str(paths.tick_state)
        os.environ["POLYMARKET_TICK_HISTORY_PATH"] = str(paths.tick_history)
        os.environ["POLYMARKET_DRY_RUN"] = "1"
    if port is not None:
        os.environ["POLYMARKET_DASHBOARD_PORT"] = str(port)
    serve(Settings())


@app.command()
def doctor() -> None:
    """Read-only health check: validates .env, auth, endpoints, local state."""
    run_doctor(Settings())


@app.command()
def status() -> None:
    """Snapshot rapide du bot : mode, ledger, équité, positions ouvertes."""
    print_status(Settings())


@app.command()
def positions() -> None:
    """Affiche les positions ouvertes en table CLI, triées par PnL décroissant."""
    print_positions(Settings())


@app.command("leaderboard")
def cli_leaderboard(
    runs: str = typer.Option(
        "news,edge",
        "--runs",
        help="CSV of dry-run names to compare (e.g. 'news,edge').",
    ),
    interval: int = typer.Option(
        15,
        "--interval",
        help="Refresh interval in minutes (default 15). Use --once for a single snapshot.",
    ),
    once: bool = typer.Option(
        False,
        "--once",
        help="Print the leaderboard once and exit (no polling loop).",
    ),
    telegram: bool = typer.Option(
        False,
        "--telegram",
        help="Also broadcast each refresh to Telegram (requires TELEGRAM_* env vars).",
    ),
) -> None:
    """Live leaderboard ranking dry-run strategies by ROI.

    Reads each ``data/dry_runs/<name>/`` (state.json, journal.jsonl,
    metadata.json) and prints a ranked scoreboard. Designed to run as
    a sidecar process alongside the trading bots.
    """
    from .leaderboard import (
        format_leaderboard,
        gather_run_stats,
        run_leaderboard_loop,
    )

    base_dir = Path(__file__).resolve().parent.parent / "data"
    run_names = [r.strip() for r in runs.split(",") if r.strip()]
    if not run_names:
        typer.echo("--runs is empty", err=True)
        raise typer.Exit(code=2)
    if once:
        stats = []
        for name in run_names:
            s = gather_run_stats(base_dir, name)
            if s is not None:
                stats.append(s)
        typer.echo(format_leaderboard(stats))
        return
    run_leaderboard_loop(base_dir, run_names, max(60, interval * 60), telegram=telegram)


@app.command("journal-stats")
def cli_journal_stats() -> None:
    """Print aggregated trade-journal statistics as JSON."""
    payload = journal_stats(Settings())
    structured_suggestions = payload.get("suggestions") or []
    payload["suggestions"] = format_suggestions(structured_suggestions)
    payload["suggestions_structured"] = structured_suggestions
    typer.echo(json.dumps(payload, indent=2, default=str))


@app.command("tune-strategy")
def cli_tune_strategy() -> None:
    """Run the auto-tuner once and print the resulting overrides as JSON."""
    settings = Settings()
    overrides, journal_size = maybe_tune(settings)
    typer.echo(
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


@app.command("bootstrap-creds")
def cli_bootstrap_creds() -> None:
    """Derive CLOB API credentials from the private key and save them to .env."""
    typer.echo(json.dumps(bootstrap_creds(Settings()), indent=2))


@app.command("reset-ledger")
def cli_reset_ledger() -> None:
    """Reset the local paper-trading ledger (data/paper_state.json)."""
    typer.echo(json.dumps(reset_ledger(Settings()), indent=2))


if __name__ == "__main__":
    app()
