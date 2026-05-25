"""News strategy: momentum on markets expiring within a short window.

Self-contained alternative to the smart-money copy strategy. The only hard
rule is the expiry window — every other knob is a tunable filter. Pulls
Gamma markets that close within ``settings.news_max_hours``, picks
outcomes with positive momentum and tight execution, and opens small
positions until the per-tick cap is hit.

Features:
- Per-asset dedupe (max one BTC, ETH, SOL, XRP… position at a time).
- Smart-money flow bonus: candidates that leaderboard wallets are also
  buying in the last lookback window get a score multiplier and bigger
  stake. Best-effort; if the fetch fails the strategy still runs.
- Conviction-weighted sizing: $5 baseline, up to $12 on strong signals.
- Partial take-profit at +25% (sells half), then trailing stop on the
  remainder so winners can ride to resolution.
- Time-adaptive stop-loss: -25% with > 1h to expire, tightens to -15%
  inside 1h, -10% inside 30 min.
- Cash floor: stops opening new positions when cash falls below
  ``news_cash_floor_pct`` of equity.

Reuses ``execute_live_trade`` / ``execute_live_sell`` / ``Portfolio`` so
the dashboard, journal, and notification pipeline work unchanged.
"""

from __future__ import annotations

import datetime as dt
import re
from datetime import timedelta
from typing import Any

from . import notifications
from .config import Settings
from .gamma import GammaClient
from .models import Candidate, as_float, is_excluded_market, parse_dt, parse_json_list, utc_now
from .portfolio import Portfolio
from .pricing import ensure_open_positions_in_pool
from .smart_money import SmartMoneyData, fetch_smart_money_data
from .trading import build_client, execute_live_sell, execute_live_trade


# Asset detection: maps regex on question text to a stable asset key.
# Order matters — longer / more specific patterns first.
_ASSET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bbitcoin\b|\bbtc\b", re.I), "BTC"),
    (re.compile(r"\bethereum\b|\beth\b", re.I), "ETH"),
    (re.compile(r"\bsolana\b|\bsol\b", re.I), "SOL"),
    (re.compile(r"\bxrp\b|\bripple\b", re.I), "XRP"),
    (re.compile(r"\bdogecoin\b|\bdoge\b", re.I), "DOGE"),
    (re.compile(r"\bcardano\b|\bada\b", re.I), "ADA"),
    (re.compile(r"\bavalanche\b|\bavax\b", re.I), "AVAX"),
    (re.compile(r"\bpolygon\b|\bmatic\b", re.I), "MATIC"),
    (re.compile(r"\bchainlink\b|\blink\b", re.I), "LINK"),
    (re.compile(r"\bpolkadot\b|\bdot\b", re.I), "DOT"),
]


def _step(settings: Settings, msg: str) -> None:
    if not settings.quiet:
        print(msg, flush=True)


def _asset_key(question: str, event_slug: str, slug: str = "") -> str | None:
    """Map a market to a stable underlying-asset key for dedupe.

    Crypto markets share an asset (BTC/ETH/SOL/...) across multiple
    expiry windows. Without grouping, the bot would stack parallel
    bets on the same underlying — even on opposite sides (Solana Up
    AND Solana Down), which is just paying fees twice.

    We scan question + event_slug + slug together so a synced position
    with an empty question (Polymarket's positions API sometimes omits
    titles) still matches via the slug. Returns ``None`` only when
    nothing is parseable.
    """
    blob = f"{question or ''} {event_slug or ''} {slug or ''}"
    for pattern, key in _ASSET_PATTERNS:
        if pattern.search(blob):
            return f"crypto:{key}"
    if event_slug:
        return f"event:{event_slug}"
    return None


def _position_asset_key(position: dict[str, Any]) -> str | None:
    question = str(position.get("question") or "")
    event_slug = str(position.get("event_slug") or "")
    slug = str(position.get("slug") or "")
    return _asset_key(question, event_slug, slug)


def _build_news_candidates(
    markets: list[dict[str, Any]],
    settings: Settings,
) -> list[tuple[Candidate, float]]:
    """Filter + score Gamma markets for the news strategy."""
    now = utc_now()
    earliest = now + timedelta(hours=settings.news_min_hours)
    horizon = now + timedelta(hours=settings.news_max_hours)
    scored: list[tuple[Candidate, float]] = []

    for market in markets:
        if is_excluded_market(market):
            continue
        end_date = parse_dt(market.get("endDate"))
        if end_date is None or end_date < earliest or end_date > horizon:
            continue
        if not bool(market.get("acceptingOrders")):
            continue

        liquidity = as_float(market.get("liquidity") or market.get("liquidityNum"))
        volume_24h = as_float(market.get("volume24hr") or market.get("volume24hrClob"))
        if liquidity < settings.news_min_liquidity_usd:
            continue
        if volume_24h < settings.news_min_volume_24h_usd:
            continue

        best_bid_raw = market.get("bestBid")
        best_ask_raw = market.get("bestAsk")
        if best_bid_raw is None or best_ask_raw is None:
            continue
        market_best_bid = as_float(best_bid_raw, default=None) if best_bid_raw is not None else None
        market_best_ask = as_float(best_ask_raw, default=None) if best_ask_raw is not None else None
        if market_best_bid is None or market_best_ask is None:
            continue
        tick_size_raw = market.get("orderPriceMinTickSize")
        tick_size = as_float(tick_size_raw, default=None) if tick_size_raw is not None else None
        if tick_size is None or tick_size <= 0:
            continue

        outcomes = [str(item) for item in parse_json_list(market.get("outcomes"))]
        prices = [as_float(item, -1.0) for item in parse_json_list(market.get("outcomePrices"))]
        token_ids = [str(item) for item in parse_json_list(market.get("clobTokenIds"))]
        if not outcomes or len(outcomes) != len(prices) or len(outcomes) != 2:
            continue

        neg_risk = bool(market.get("negRisk"))
        slug = str(market.get("slug") or market.get("id") or "")
        event_slug = _event_slug(market)
        market_id = str(market.get("id") or "")
        question = str(market.get("question") or "")
        url = (
            f"https://polymarket.com/event/{event_slug or slug}"
            if (event_slug or slug)
            else "https://polymarket.com"
        )

        hours_to_close = max((end_date - now).total_seconds() / 3600.0, 0.0)
        one_day_change = as_float(market.get("oneDayPriceChange"), default=0.0)

        for index, outcome in enumerate(outcomes):
            price = prices[index]
            if price <= 0.0 or price >= 1.0:
                continue
            best_bid, best_ask = _quote_for_outcome(
                index, len(outcomes), market_best_bid, market_best_ask
            )
            if best_bid is None or best_ask is None:
                continue
            if best_ask < settings.news_min_price or best_ask > settings.news_max_price:
                continue
            spread = best_ask - best_bid
            if spread < 0 or spread > settings.news_max_spread:
                continue
            mid = (best_bid + best_ask) / 2.0
            relative_spread = spread / mid if mid > 0 else 1.0
            if relative_spread > settings.news_max_relative_spread:
                continue

            outcome_momentum = one_day_change if index == 0 else -one_day_change
            if settings.news_require_positive_momentum and outcome_momentum <= 0:
                continue
            if abs(outcome_momentum) < settings.news_min_abs_momentum:
                continue

            urgency = 4.0 / max(hours_to_close, 0.25)
            volume_score = volume_24h / max(price * 50_000.0, 1.0)
            momentum_score = outcome_momentum * 50.0
            score = momentum_score + volume_score + urgency

            candidate = Candidate(
                market_id=market_id,
                question=question,
                slug=slug,
                end_date=end_date,
                hours_to_close=hours_to_close,
                liquidity=liquidity,
                volume=as_float(market.get("volume") or market.get("volumeNum")),
                outcome=outcome,
                price=price,
                token_id=token_ids[index] if index < len(token_ids) else None,
                score=score,
                url=url,
                best_bid=best_bid,
                best_ask=best_ask,
                tick_size=tick_size,
                neg_risk=neg_risk,
                accepts_orders=True,
                event_slug=event_slug,
            )
            scored.append((candidate, score))

    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


def _quote_for_outcome(
    index: int,
    outcome_count: int,
    market_best_bid: float | None,
    market_best_ask: float | None,
) -> tuple[float | None, float | None]:
    if market_best_bid is None or market_best_ask is None:
        return None, None
    if outcome_count != 2:
        return None, None
    if index == 0:
        return market_best_bid, market_best_ask
    if index == 1:
        return round(1.0 - market_best_ask, 4), round(1.0 - market_best_bid, 4)
    return None, None


def _event_slug(market: dict[str, Any]) -> str:
    event_slug = market.get("eventSlug")
    if event_slug:
        return str(event_slug)
    events = market.get("events")
    if isinstance(events, list) and events:
        first = events[0]
        if isinstance(first, dict) and first.get("slug"):
            return str(first.get("slug"))
    event = market.get("event")
    if isinstance(event, dict) and event.get("slug"):
        return str(event.get("slug"))
    return ""


def _load_news_markets(settings: Settings) -> list[dict[str, Any]]:
    client = GammaClient(settings.gamma_base_url)
    now = utc_now()
    horizon = now + timedelta(hours=settings.news_max_hours)
    batches: list[list[dict[str, Any]]] = []
    try:
        batches.append(
            client.get_markets(
                limit=settings.news_scan_limit,
                end_date_min=now,
                end_date_max=horizon,
                order="end_date",
                ascending=True,
            )
        )
    except Exception as exc:
        print(f"⚠️  Gamma news batch failed: {type(exc).__name__}: {exc}")
    try:
        batches.append(
            client.get_markets(
                limit=settings.news_scan_limit,
                end_date_min=now,
                end_date_max=horizon,
                order="volume",
                ascending=False,
            )
        )
    except Exception as exc:
        print(f"⚠️  Gamma news volume batch failed: {type(exc).__name__}: {exc}")
    merged: dict[str, dict[str, Any]] = {}
    for batch in batches:
        for market in batch:
            key = str(market.get("id") or market.get("conditionId") or "")
            if key and key not in merged:
                merged[key] = market
    return list(merged.values())


def _smart_money_flow_by_token(
    settings: Settings,
) -> dict[str, float]:
    """Best-effort: total USD smart-money BUY flow per token, last lookback window.

    Reuses ``fetch_smart_money_data`` so leaderboard wallets, persistence
    filter, and concurrency are shared with the smart-money strategy.
    On any failure we return an empty dict — the news strategy still
    runs, just without the boost.
    """
    if not settings.news_smart_money_boost_enabled:
        return {}
    try:
        data: SmartMoneyData = fetch_smart_money_data(settings)
    except Exception as exc:
        print(f"   news: smart-money flow fetch failed: {type(exc).__name__}: {exc}")
        return {}
    flow: dict[str, float] = {}
    for trade in data.trades or []:
        if (trade.side or "").upper() != "BUY":
            continue
        asset = getattr(trade, "asset", None)
        if not asset:
            continue
        flow[asset] = flow.get(asset, 0.0) + float(getattr(trade, "usdc_size", 0.0) or 0.0)
    return flow


def _conviction_tier(
    candidate: Candidate,
    score: float,
    smart_money_flow_usd: float,
    settings: Settings,
) -> tuple[str, float]:
    """Return (tier_name, stake_usd) for a candidate.

    Three tiers based on conviction:

    - **high**: smart-money flow ≥ threshold AND (price < 0.40 OR score is top-tier)
    - **mid**: smart-money flow ≥ threshold/2 OR price < 0.40
    - **low**: everything else
    """
    base = settings.news_stake_usd
    high = settings.news_max_stake_usd
    low = max(settings.news_min_stake_usd, base * 0.75)
    has_flow = smart_money_flow_usd >= settings.news_smart_money_min_flow_usd
    half_flow = smart_money_flow_usd >= settings.news_smart_money_min_flow_usd * 0.5
    low_price = (candidate.best_ask or 1.0) < 0.40

    if has_flow and (low_price or score >= 6.0):
        return "high", high
    if has_flow or half_flow or low_price:
        return "mid", base
    return "low", low


def _adaptive_stop_pct(hours_to_close: float | None, settings: Settings) -> float:
    """Tighten the stop-loss as expiry approaches.

    Premise: with 3h left we can recover from a drawdown; with 20 min
    left we can't. Returns the absolute fraction (e.g. 0.25 = -25%).
    """
    h = float(hours_to_close or 0.0)
    if h <= settings.news_very_tight_stop_hours:
        return settings.news_very_tight_stop_pct
    if h <= settings.news_tight_stop_hours:
        return settings.news_tight_stop_pct
    return settings.news_stop_loss_pct


def _position_age_minutes(position: dict[str, Any]) -> float:
    opened_at = position.get("opened_at")
    if not opened_at:
        return 0.0
    try:
        opened_dt = dt.datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return 0.0
    return max(0.0, (utc_now() - opened_dt).total_seconds() / 60.0)


def _minutes_to_close(position: dict[str, Any]) -> float | None:
    end_iso = position.get("end_date")
    if not end_iso:
        return None
    end_dt = parse_dt(str(end_iso))
    if end_dt is None:
        return None
    return (end_dt - utc_now()).total_seconds() / 60.0


def _hours_to_close_from_position(position: dict[str, Any]) -> float | None:
    minutes = _minutes_to_close(position)
    if minutes is None:
        return None
    return minutes / 60.0


def _news_sell_plan(
    position: dict[str, Any],
    current_pnl_pct: float,
    settings: Settings,
) -> dict[str, Any] | None:
    """Multi-tier exit plan: partial TP, trailing stop, near-expiry flush.

    Order of checks:
    1. Partial take-profit at +TP% (sell half, mark tier hit).
    2. Trailing stop after partial TP arms (sell remainder if peak
       gives back ``trailing_giveback_pct`` while still > 0).
    3. Near-expiry positive flush.
    """
    shares = float(position.get("shares", 0.0) or 0.0)
    if shares <= 0:
        return None

    # Universal min-hold: no sell of any kind before stop_loss_min_age_minutes.
    age_minutes = _position_age_minutes(position)
    if age_minutes < settings.news_stop_loss_min_age_minutes:
        return None

    tier_hit = position.get("news_tier_hit") or ""
    peak_pnl_pct = float(position.get("peak_pnl_pct", current_pnl_pct) or 0.0)

    # 1. Partial take-profit (first time only).
    if (
        tier_hit != "tp1"
        and settings.news_partial_tp_fraction > 0
        and current_pnl_pct >= settings.news_take_profit_pct
    ):
        return {
            "reason": "news_take_profit",
            "shares": shares * settings.news_partial_tp_fraction,
            "tier": "tp1",
        }

    # 2. Trailing stop on remainder, after partial TP.
    if tier_hit == "tp1" and peak_pnl_pct >= settings.news_trailing_arm_pct:
        floor_pct = peak_pnl_pct * (1.0 - settings.news_trailing_giveback_pct)
        if current_pnl_pct <= max(0.0, floor_pct):
            return {
                "reason": "news_trailing_stop",
                "shares": shares,
            }

    # 3. Near-expiry positive flush: protect a gain right before close.
    # Only fires when current PnL is strictly above the configured floor
    # (default 0%) so we never close a losing trade just because time is
    # short. Mirror of the smart-money near-expiry-positive-exit rule.
    minutes_left = _minutes_to_close(position)
    if (
        minutes_left is not None
        and minutes_left <= settings.news_near_expiry_minutes
        and current_pnl_pct > settings.news_near_expiry_min_profit
    ):
        return {"reason": "news_near_expiry", "shares": shares}

    return None


def _execute_news_exits(
    client: Any,
    settings: Settings,
    portfolio: Portfolio,
    pool: list[Candidate],
) -> list[dict[str, Any]]:
    by_token = {c.token_id: c for c in pool if c.token_id}
    exits: list[dict[str, Any]] = []
    for position in list(portfolio.positions):
        if position.get("status") != "open" or not position.get("live"):
            continue
        token_id = position.get("token_id")
        candidate = by_token.get(token_id)
        if candidate is None or candidate.best_bid is None or candidate.best_bid <= 0:
            continue
        entry_price = float(position.get("entry_price", 0.0) or 0.0)
        if entry_price <= 0:
            continue
        current_pnl_pct = (candidate.best_bid - entry_price) / entry_price
        position["peak_pnl_pct"] = max(
            float(position.get("peak_pnl_pct", current_pnl_pct)), current_pnl_pct
        )
        plan = _news_sell_plan(position, current_pnl_pct, settings)
        if plan is None and (
            settings.news_resolved_exit_threshold > 0
            and candidate.best_bid >= settings.news_resolved_exit_threshold
        ):
            plan = {
                "reason": "news_resolved_market",
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
            print(
                f"⚠️  news sell skipped on {position.get('question')}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            exits.append(
                {
                    "market_id": position.get("market_id"),
                    "question": position.get("question"),
                    "action": "skip_sell",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        if plan.get("tier"):
            position["news_tier_hit"] = str(plan["tier"])
        portfolio.save(settings.state_path)
        exits.append(
            {
                "market_id": position.get("market_id"),
                "question": position.get("question"),
                "outcome": position.get("outcome"),
                "action": "sell",
                "reason": plan["reason"],
                "pnl_pct": round(current_pnl_pct, 4),
                "order": result.order,
                "response": result.response,
            }
        )
    return exits


def _open_asset_keys(portfolio: Portfolio) -> set[str]:
    keys: set[str] = set()
    for position in portfolio.positions:
        if position.get("status") != "open":
            continue
        key = _position_asset_key(position)
        if key:
            keys.add(key)
    return keys


def news_once(settings: Settings) -> dict[str, Any]:
    """Single tick of the news strategy."""
    print("▶  news tick start", flush=True)
    _step(settings, "   loading expiring markets...")
    markets = _load_news_markets(settings)
    _step(settings, f"   markets: {len(markets)} raw")
    scored = _build_news_candidates(markets, settings)
    _step(settings, f"   eligible after filters: {len(scored)}")

    portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)

    # Live-position sync: prevents phantom positions in the local ledger
    # from triggering "balance is not enough" SELLs on the next tick.
    if settings.dry_run:
        _step(settings, "   [DRY-RUN] skipping live-position sync")
    elif settings.sync_live_positions:
        from .main import _sync_live_positions

        _step(settings, "   syncing live positions...")
        sync_actions = _sync_live_positions(settings, portfolio)
        if sync_actions:
            closed = sum(1 for a in sync_actions if a.get("action") == "closed_stale_local_position")
            imported = sum(1 for a in sync_actions if a.get("action") == "imported_live_position")
            _step(settings, f"   sync: {closed} stale closed, {imported} imported")

    candidates_only = [c for c, _ in scored]
    pool = ensure_open_positions_in_pool(settings, portfolio, candidates_only)
    portfolio.mark_to_market(pool)
    summary = portfolio.summary()
    _step(settings, f"   open positions: {summary['open_positions']} | cash ${summary['cash']:.2f}")

    client = build_client(settings)
    exit_report = _execute_news_exits(client, settings, portfolio, pool)
    sells = sum(1 for e in exit_report if e.get("action") == "sell")
    if sells:
        _step(settings, f"   news exits executed: {sells}")

    if not settings.dry_run:
        try:
            live_cash = client.live_available_balance()
            portfolio.cash = round(live_cash, 2)
        except Exception as exc:
            print(f"   live cash refresh failed: {type(exc).__name__}: {exc}")

    # Pull smart-money flow once per tick (best-effort).
    flow_by_token = _smart_money_flow_by_token(settings)
    if flow_by_token:
        _step(settings, f"   smart-money flow on {len(flow_by_token)} token(s)")

    executed_trades: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    stop_reason: str | None = None

    summary = portfolio.summary()
    equity = float(summary.get("equity", 0.0) or 0.0)
    cash_floor_usd = equity * settings.news_cash_floor_pct if equity > 0 else 0.0
    available_cash = portfolio.cash
    if available_cash < max(1.0, cash_floor_usd):
        portfolio.save(settings.state_path)
        return {
            "trade": None,
            "strategy": "news",
            "status": "cash_floor_reached" if cash_floor_usd > 0 else "waiting_for_funds",
            "available_cash": available_cash,
            "cash_floor_usd": cash_floor_usd,
            "exits": exit_report,
            "trades": [],
            "orders_placed": 0,
            "scan_counts": {"candidates": len(scored)},
            "summary": summary,
        }

    open_assets = _open_asset_keys(portfolio)

    for candidate, score in scored:
        if settings.news_max_orders_per_tick > 0 and len(executed_trades) >= settings.news_max_orders_per_tick:
            stop_reason = "max_orders_per_tick_reached"
            break
        if not candidate.token_id:
            continue
        if portfolio.has_open_position(candidate.market_id):
            continue
        if portfolio.has_open_token(candidate.token_id):
            continue
        if portfolio.has_pending_token(candidate.token_id):
            continue
        if portfolio.has_open_event_position(candidate):
            continue

        # Per-asset dedupe: skip if we already hold BTC/ETH/SOL/XRP/...
        asset_key = _asset_key(candidate.question, candidate.event_slug or "", candidate.slug or "")
        if asset_key and asset_key in open_assets:
            rejected.append(
                {
                    "market_id": candidate.market_id,
                    "question": candidate.question,
                    "outcome": candidate.outcome,
                    "reason": f"duplicate_asset:{asset_key}",
                }
            )
            continue

        flow_usd = flow_by_token.get(candidate.token_id, 0.0)
        tier, stake_target = _conviction_tier(candidate, score, flow_usd, settings)

        # Cash floor: don't burn through to zero. Cap stake by what we
        # have left above the floor.
        cash_above_floor = max(0.0, portfolio.cash - cash_floor_usd)
        stake_cap = min(stake_target, max(1.0, cash_above_floor))
        if stake_cap < settings.news_min_stake_usd:
            stop_reason = "cash_floor_reached"
            break

        signal_payload = {
            "question": candidate.question,
            "selection_reason": (
                f"news {tier} on {candidate.outcome} "
                f"score={score:.2f} flow=${flow_usd:.0f} "
                f"ask={candidate.best_ask} h2c={candidate.hours_to_close:.2f}"
            ),
            "selection_metrics": {
                "score": round(score, 3),
                "tier": tier,
                "smart_money_flow_usd": round(flow_usd, 2),
                "current_ask": candidate.best_ask,
                "current_bid": candidate.best_bid,
                "spread": round(
                    (candidate.best_ask or 0.0) - (candidate.best_bid or 0.0), 4
                ),
                "hours_to_close": round(candidate.hours_to_close or 0.0, 3),
                "liquidity": candidate.liquidity,
                "volume": candidate.volume,
                "asset_key": asset_key,
            },
            "tag": "news",
        }
        try:
            result = execute_live_trade(
                client,
                settings,
                candidate,
                portfolio,
                min_trade_usd=1.0,
                max_trade_usd=stake_cap,
                strategy="news",
                signal=signal_payload,
            )
            executed_trades.append(
                {
                    "strategy": "news",
                    "signal": signal_payload,
                    "order": result.order,
                    "response": result.response,
                }
            )
            if asset_key:
                open_assets.add(asset_key)
            portfolio.save(settings.state_path)
        except ValueError as exc:
            message = str(exc)
            rejected.append(
                {
                    "market_id": candidate.market_id,
                    "question": candidate.question,
                    "outcome": candidate.outcome,
                    "reason": message,
                }
            )
            lower = message.lower()
            if "balance" in lower or ("below" in lower and "minimum" in lower):
                stop_reason = message
                break
            continue
        except Exception as exc:
            print(f"   news buy error: {type(exc).__name__}: {exc}", flush=True)
            try:
                notifications.notify_error(
                    "order_rejected",
                    str(exc)[:500],
                    dedupe_key=f"news_order_rejected:{candidate.token_id}",
                )
            except Exception:
                pass
            rejected.append(
                {
                    "market_id": candidate.market_id,
                    "question": candidate.question,
                    "outcome": candidate.outcome,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

    portfolio.save(settings.state_path)
    return {
        "trade": executed_trades[-1] if executed_trades else None,
        "strategy": "news",
        "trades": executed_trades,
        "orders_placed": len(executed_trades),
        "stop_reason": stop_reason,
        "exits": exit_report,
        "rejected_signals": rejected,
        "scan_counts": {
            "candidates": len(scored),
            "raw_markets": len(markets),
            "smart_money_tokens": len(flow_by_token),
        },
        "summary": portfolio.summary(),
    }


def news_loop(settings: Settings) -> None:
    """Run :func:`news_once` on the standard tick cadence."""
    from .main import strategy_loop

    strategy_loop(settings, "news", news_once)
