"""Three lightweight strategies for the dry-run race.

- ``random_once``     — control group: random market, random outcome, $5 stake.
- ``contrarian_once`` — bet against today's momentum (mean-reversion thesis).
- ``favorite_once``   — buy any heavy favorite (best_bid ≥ favorite_min_bid).

All three share the same:
- 4h expiry hard rule
- $5 default stake, max 3 trades/tick
- Simple exit ladder: TP +25%, SL -50% (after 5min), near-expiry flush, resolved exit
- ``execute_live_trade`` / ``execute_live_sell`` integration so the journal and
  dashboard work unchanged

They exist primarily to give the news + edge strategies a fair comparison
benchmark in the dry-run race.
"""

from __future__ import annotations

import datetime as dt
import random
from datetime import timedelta
from typing import Any

from . import notifications
from .config import Settings
from .gamma import GammaClient
from .models import Candidate, as_float, parse_dt, parse_json_list, utc_now
from .news_strategy import _asset_key, _event_slug, _quote_for_outcome
from .portfolio import Portfolio
from .pricing import ensure_open_positions_in_pool
from .trading import build_client, execute_live_sell, execute_live_trade


def _step(settings: Settings, msg: str) -> None:
    if not settings.quiet:
        print(msg, flush=True)


def _load_short_expiry_markets(settings: Settings) -> list[dict[str, Any]]:
    client = GammaClient(settings.gamma_base_url)
    now = utc_now()
    horizon = now + timedelta(hours=settings.race_max_hours)
    batches: list[list[dict[str, Any]]] = []
    for kwargs in (
        {
            "limit": settings.race_scan_limit,
            "end_date_min": now,
            "end_date_max": horizon,
            "order": "end_date",
            "ascending": True,
        },
        {
            "limit": settings.race_scan_limit,
            "end_date_min": now,
            "end_date_max": horizon,
            "order": "volume",
            "ascending": False,
        },
    ):
        try:
            batches.append(client.get_markets(**kwargs))
        except Exception as exc:
            print(f"⚠️  race: gamma batch failed: {type(exc).__name__}: {exc}")
    merged: dict[str, dict[str, Any]] = {}
    for batch in batches:
        for market in batch:
            key = str(market.get("id") or market.get("conditionId") or "")
            if key and key not in merged:
                merged[key] = market
    return list(merged.values())


def _build_eligible_candidates(
    markets: list[dict[str, Any]],
    settings: Settings,
) -> list[tuple[Candidate, float]]:
    """Per-outcome candidate with a momentum signal attached for ranking.

    Returns ``(candidate, momentum_for_this_outcome)``. Momentum is the
    YES-side ``oneDayPriceChange`` flipped for the NO side. Caller picks
    whichever sign matches their thesis (contrarian flips, favorite
    ignores momentum entirely).
    """
    now = utc_now()
    earliest = now + timedelta(minutes=5)
    horizon = now + timedelta(hours=settings.race_max_hours)
    out: list[tuple[Candidate, float]] = []
    for market in markets:
        end_date = parse_dt(market.get("endDate"))
        if end_date is None or end_date < earliest or end_date > horizon:
            continue
        if not bool(market.get("acceptingOrders")):
            continue
        liquidity = as_float(market.get("liquidity") or market.get("liquidityNum"))
        volume_24h = as_float(market.get("volume24hr") or market.get("volume24hrClob"))
        if liquidity < settings.race_min_liquidity_usd:
            continue
        if volume_24h < settings.race_min_volume_24h_usd:
            continue

        bid_raw = market.get("bestBid")
        ask_raw = market.get("bestAsk")
        if bid_raw is None or ask_raw is None:
            continue
        market_best_bid = as_float(bid_raw, default=None)
        market_best_ask = as_float(ask_raw, default=None)
        tick_raw = market.get("orderPriceMinTickSize")
        tick_size = as_float(tick_raw, default=None) if tick_raw is not None else None
        if market_best_bid is None or market_best_ask is None or tick_size is None or tick_size <= 0:
            continue

        outcomes = [str(item) for item in parse_json_list(market.get("outcomes"))]
        prices = [as_float(item, -1.0) for item in parse_json_list(market.get("outcomePrices"))]
        token_ids = [str(item) for item in parse_json_list(market.get("clobTokenIds"))]
        if len(outcomes) != 2 or len(prices) != 2:
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
            best_bid, best_ask = _quote_for_outcome(index, 2, market_best_bid, market_best_ask)
            if best_bid is None or best_ask is None:
                continue
            if best_ask < settings.race_min_price or best_ask > settings.race_max_price:
                continue
            spread = best_ask - best_bid
            if spread < 0 or spread > settings.race_max_spread:
                continue
            outcome_momentum = one_day_change if index == 0 else -one_day_change
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
                score=0.0,
                url=url,
                best_bid=best_bid,
                best_ask=best_ask,
                tick_size=tick_size,
                neg_risk=neg_risk,
                accepts_orders=True,
                event_slug=event_slug,
            )
            out.append((candidate, outcome_momentum))
    return out


# ---------------------------------------------------------------------------
# Selection logic per strategy
# ---------------------------------------------------------------------------


def select_random(eligible: list[tuple[Candidate, float]], n: int) -> list[Candidate]:
    """Pure random picks. Each candidate has equal probability."""
    candidates = [c for c, _ in eligible]
    if len(candidates) <= n:
        return candidates
    return random.sample(candidates, n)


def select_contrarian(eligible: list[tuple[Candidate, float]], n: int, min_momentum: float) -> list[Candidate]:
    """Bet against momentum: pick outcomes where the OTHER side has moved up most.

    If "Yes" moved up 5¢ today, buy "No" (we think the move overshot).
    Mean-reversion thesis — equally falsifiable, lets us A/B against
    the news strategy which chases momentum.
    """
    # Negative momentum on THIS outcome = the OTHER outcome moved up.
    # We want strong negative momentum to bet on this side reverting.
    contrarian_candidates = [(c, -mom) for c, mom in eligible if -mom >= min_momentum]
    contrarian_candidates.sort(key=lambda t: t[1], reverse=True)
    # Dedupe by market_id (keep highest contrarian score per market).
    seen: set[str] = set()
    picks: list[Candidate] = []
    for c, _ in contrarian_candidates:
        if c.market_id in seen:
            continue
        seen.add(c.market_id)
        picks.append(c)
        if len(picks) >= n:
            break
    return picks


def select_favorite(eligible: list[tuple[Candidate, float]], n: int, min_bid: float) -> list[Candidate]:
    """Buy heavy favorites: best_bid ≥ min_bid AND ask is buyable.

    Hypothesis: heavy favorites win their fair share. Whether the
    spread + slippage eats the edge is exactly what we want to test.
    """
    favorites = [c for c, _ in eligible if (c.best_bid or 0) >= min_bid]
    favorites.sort(key=lambda c: (c.best_bid or 0), reverse=True)
    seen: set[str] = set()
    picks: list[Candidate] = []
    for c in favorites:
        if c.market_id in seen:
            continue
        seen.add(c.market_id)
        picks.append(c)
        if len(picks) >= n:
            break
    return picks


def select_breakout(
    eligible: list[tuple[Candidate, float]],
    n: int,
    min_momentum: float,
    min_volume_24h: float,
) -> list[Candidate]:
    """Momentum-continuation: ride strong moves that are volume-confirmed.

    Stronger version of the news strategy. Requires BOTH a meaningful
    intraday price move AND high recent volume — the volume filter
    rejects "thin spike" markets where the move is just one whale
    pushing an illiquid orderbook around.
    """
    qualified = [
        (c, mom)
        for c, mom in eligible
        if mom >= min_momentum and c.volume >= min_volume_24h
    ]
    qualified.sort(key=lambda t: t[1] * (t[0].volume or 0.0), reverse=True)
    seen: set[str] = set()
    picks: list[Candidate] = []
    for c, _ in qualified:
        if c.market_id in seen:
            continue
        seen.add(c.market_id)
        picks.append(c)
        if len(picks) >= n:
            break
    return picks


def select_panic_fade(
    eligible: list[tuple[Candidate, float]],
    n: int,
    min_panic_move: float,
    min_volume_24h: float,
) -> list[Candidate]:
    """Fade emotional overreaction: bet against EXTREME intraday moves.

    Inspired by HFT microstructure literature (the "panic fade" thesis):
    short-term overreaction in low-liquidity prediction markets reverts
    when the aggression exhausts. Our polling-based version can't see
    sub-second taker exhaustion, but a ≥15% one-day move is a strong
    proxy for emotional flow that often partially mean-reverts before
    expiry.

    Only fires on volume-confirmed panics — a 15% move on $100 of
    volume is one whale tantrum, not market-wide overreaction.
    """
    qualified = [
        (c, -mom)  # We want to fade the WINNING side, so flip momentum sign.
        for c, mom in eligible
        if -mom >= min_panic_move and (c.volume or 0) >= min_volume_24h
    ]
    qualified.sort(key=lambda t: t[1], reverse=True)
    seen: set[str] = set()
    picks: list[Candidate] = []
    for c, _ in qualified:
        if c.market_id in seen:
            continue
        seen.add(c.market_id)
        picks.append(c)
        if len(picks) >= n:
            break
    return picks


def select_underdog_momentum(
    eligible: list[tuple[Candidate, float]],
    n: int,
    max_ask: float,
    min_momentum: float,
    min_volume_24h: float,
) -> list[Candidate]:
    """Asymmetric payoff hunter: cheap underdogs gaining momentum.

    Thesis: when an underdog (ask ≤ ~30¢) starts gaining ground intraday
    AND volume is unusually high, that's often informed flow front-running
    a resolution. The payoff is asymmetric: ~3-4x return if we win,
    ~1x loss if we don't. Even at a 30% true win rate the EV is
    positive after fees.

    Fresh angle — none of the other strategies in the race specifically
    target cheap-but-rising markets with volume confirmation.
    """
    qualified = [
        (c, mom)
        for c, mom in eligible
        if (c.best_ask or 1.0) <= max_ask
        and mom >= min_momentum
        and (c.volume or 0) >= min_volume_24h
    ]
    # Rank by edge magnitude: how much momentum per dollar of risk.
    qualified.sort(key=lambda t: t[1] / max(t[0].best_ask or 0.01, 0.01), reverse=True)
    seen: set[str] = set()
    picks: list[Candidate] = []
    for c, _ in qualified:
        if c.market_id in seen:
            continue
        seen.add(c.market_id)
        picks.append(c)
        if len(picks) >= n:
            break
    return picks


def select_late_favorite(
    eligible: list[tuple[Candidate, float]],
    n: int,
    min_bid: float,
    max_hours: float,
) -> list[Candidate]:
    """Last-mile favorites: bid ≥ min_bid AND < max_hours to expiry.

    The pure "resolution edge" play. Prediction-market research shows
    favorites in the final minutes of a market resolve at very high
    rates (the price already implies the outcome). Whether we can
    capture this edge after the spread + Polymarket's tick size is
    the empirical question.
    """
    favorites = [
        c
        for c, _ in eligible
        if (c.best_bid or 0) >= min_bid
        and (c.hours_to_close or 99) <= max_hours
    ]
    # Rank by closest-to-expiry first — those have the tightest
    # convergence window and least uncertainty.
    favorites.sort(key=lambda c: (c.hours_to_close or 99))
    seen: set[str] = set()
    picks: list[Candidate] = []
    for c in favorites:
        if c.market_id in seen:
            continue
        seen.add(c.market_id)
        picks.append(c)
        if len(picks) >= n:
            break
    return picks


# ---------------------------------------------------------------------------
# Shared exit logic
# ---------------------------------------------------------------------------


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


def _simple_exit_plan(position: dict[str, Any], current_pnl_pct: float, settings: Settings) -> dict[str, Any] | None:
    shares = float(position.get("shares", 0.0) or 0.0)
    if shares <= 0:
        return None
    if current_pnl_pct >= settings.race_tp_pct:
        return {"reason": "race_take_profit", "shares": shares}
    age = _position_age_minutes(position)
    if current_pnl_pct <= -settings.race_sl_pct and age >= settings.race_sl_min_age_minutes:
        return {"reason": "race_stop_loss", "shares": shares}
    mtc = _minutes_to_close(position)
    if mtc is not None and mtc <= settings.race_near_expiry_minutes and current_pnl_pct >= 0:
        return {"reason": "race_near_expiry", "shares": shares}
    return None


def _execute_race_exits(
    client: Any,
    settings: Settings,
    portfolio: Portfolio,
    pool: list[Candidate],
    strategy_name: str,
) -> list[dict[str, Any]]:
    by_token = {c.token_id: c for c in pool if c.token_id}
    out: list[dict[str, Any]] = []
    for position in list(portfolio.positions):
        if position.get("status") != "open" or not position.get("live"):
            continue
        if str(position.get("strategy") or "") != strategy_name:
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
        plan = _simple_exit_plan(position, current_pnl_pct, settings)
        if plan is None and (
            settings.race_resolved_exit_threshold > 0
            and candidate.best_bid >= settings.race_resolved_exit_threshold
        ):
            plan = {"reason": "race_resolved", "shares": float(position.get("shares", 0.0))}
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
                f"⚠️  {strategy_name} sell skipped on {position.get('question')}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            continue
        portfolio.save(settings.state_path)
        out.append(
            {
                "market_id": position.get("market_id"),
                "question": position.get("question"),
                "action": "sell",
                "reason": plan["reason"],
                "pnl_pct": round(current_pnl_pct, 4),
                "order": result.order,
                "response": result.response,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Tick orchestrator (shared)
# ---------------------------------------------------------------------------


def _open_asset_keys(portfolio: Portfolio) -> set[str]:
    keys: set[str] = set()
    for position in portfolio.positions:
        if position.get("status") != "open":
            continue
        key = _asset_key(
            str(position.get("question") or ""),
            str(position.get("event_slug") or ""),
            str(position.get("slug") or ""),
        )
        if key:
            keys.add(key)
    return keys


def _run_race_tick(
    settings: Settings,
    strategy_name: str,
    select_fn,
) -> dict[str, Any]:
    print(f"▶  {strategy_name} tick start", flush=True)
    markets = _load_short_expiry_markets(settings)
    _step(settings, f"   markets: {len(markets)} raw")
    eligible = _build_eligible_candidates(markets, settings)
    _step(settings, f"   eligible: {len(eligible)}")

    portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)
    if not settings.dry_run and settings.sync_live_positions:
        from .main import _sync_live_positions

        _sync_live_positions(settings, portfolio)

    pool = ensure_open_positions_in_pool(settings, portfolio, [c for c, _ in eligible])
    portfolio.mark_to_market(pool)

    client = build_client(settings)
    exits = _execute_race_exits(client, settings, portfolio, pool, strategy_name)

    if not settings.dry_run:
        try:
            portfolio.cash = round(client.live_available_balance(), 2)
        except Exception as exc:
            print(f"   live cash refresh failed: {type(exc).__name__}: {exc}")

    picks = select_fn(eligible)
    open_assets = _open_asset_keys(portfolio)
    executed: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    cash_floor = portfolio.summary().get("equity", 0) * settings.race_cash_floor_pct

    for candidate in picks:
        if len(executed) >= settings.race_max_orders_per_tick:
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
        asset_key = _asset_key(candidate.question, candidate.event_slug or "", candidate.slug or "")
        if asset_key and asset_key in open_assets:
            rejected.append(
                {
                    "question": candidate.question,
                    "reason": f"duplicate_asset:{asset_key}",
                }
            )
            continue
        cash_above_floor = max(0.0, portfolio.cash - cash_floor)
        if cash_above_floor < 1.0:
            break
        stake = min(settings.race_stake_usd, cash_above_floor)
        signal_payload = {
            "question": candidate.question,
            "selection_reason": (
                f"{strategy_name} pick {candidate.outcome} "
                f"ask={candidate.best_ask} h2c={candidate.hours_to_close:.2f}"
            ),
            "selection_metrics": {
                "current_ask": candidate.best_ask,
                "current_bid": candidate.best_bid,
                "hours_to_close": round(candidate.hours_to_close or 0.0, 3),
                "liquidity": candidate.liquidity,
            },
            "tag": strategy_name,
        }
        try:
            result = execute_live_trade(
                client,
                settings,
                candidate,
                portfolio,
                min_trade_usd=1.0,
                max_trade_usd=stake,
                strategy=strategy_name,
                signal=signal_payload,
            )
            executed.append({"strategy": strategy_name, "order": result.order, "response": result.response, "signal": signal_payload})
            if asset_key:
                open_assets.add(asset_key)
            portfolio.save(settings.state_path)
        except Exception as exc:
            rejected.append({"question": candidate.question, "reason": f"{type(exc).__name__}: {exc}"})

    portfolio.save(settings.state_path)
    return {
        "trade": executed[-1] if executed else None,
        "strategy": strategy_name,
        "trades": executed,
        "orders_placed": len(executed),
        "exits": exits,
        "rejected_signals": rejected,
        "scan_counts": {"raw_markets": len(markets), "eligible": len(eligible), "picks": len(picks)},
        "summary": portfolio.summary(),
    }


# ---------------------------------------------------------------------------
# Public per-strategy entrypoints
# ---------------------------------------------------------------------------


def random_once(settings: Settings) -> dict[str, Any]:
    return _run_race_tick(
        settings,
        "random",
        lambda eligible: select_random(eligible, settings.race_max_orders_per_tick),
    )


def contrarian_once(settings: Settings) -> dict[str, Any]:
    return _run_race_tick(
        settings,
        "contrarian",
        lambda eligible: select_contrarian(
            eligible,
            settings.race_max_orders_per_tick,
            settings.race_contrarian_min_momentum,
        ),
    )


def favorite_once(settings: Settings) -> dict[str, Any]:
    return _run_race_tick(
        settings,
        "favorite",
        lambda eligible: select_favorite(
            eligible,
            settings.race_max_orders_per_tick,
            settings.race_favorite_min_bid,
        ),
    )


def breakout_once(settings: Settings) -> dict[str, Any]:
    return _run_race_tick(
        settings,
        "breakout",
        lambda eligible: select_breakout(
            eligible,
            settings.race_max_orders_per_tick,
            settings.race_breakout_min_momentum,
            settings.race_breakout_min_volume,
        ),
    )


def panic_fade_once(settings: Settings) -> dict[str, Any]:
    return _run_race_tick(
        settings,
        "panic_fade",
        lambda eligible: select_panic_fade(
            eligible,
            settings.race_max_orders_per_tick,
            settings.race_panic_fade_min_move,
            settings.race_panic_fade_min_volume,
        ),
    )


def underdog_once(settings: Settings) -> dict[str, Any]:
    return _run_race_tick(
        settings,
        "underdog",
        lambda eligible: select_underdog_momentum(
            eligible,
            settings.race_max_orders_per_tick,
            settings.race_underdog_max_ask,
            settings.race_underdog_min_momentum,
            settings.race_underdog_min_volume,
        ),
    )


def panic_fade_loop(settings: Settings) -> None:
    from .main import strategy_loop

    strategy_loop(settings, "panic_fade", panic_fade_once)


def underdog_loop(settings: Settings) -> None:
    from .main import strategy_loop

    strategy_loop(settings, "underdog", underdog_once)


def late_favorite_once(settings: Settings) -> dict[str, Any]:
    return _run_race_tick(
        settings,
        "late_favorite",
        lambda eligible: select_late_favorite(
            eligible,
            settings.race_max_orders_per_tick,
            settings.race_late_favorite_min_bid,
            settings.race_late_favorite_max_hours,
        ),
    )


def random_loop(settings: Settings) -> None:
    from .main import strategy_loop

    strategy_loop(settings, "random", random_once)


def contrarian_loop(settings: Settings) -> None:
    from .main import strategy_loop

    strategy_loop(settings, "contrarian", contrarian_once)


def favorite_loop(settings: Settings) -> None:
    from .main import strategy_loop

    strategy_loop(settings, "favorite", favorite_once)


def breakout_loop(settings: Settings) -> None:
    from .main import strategy_loop

    strategy_loop(settings, "breakout", breakout_once)


def late_favorite_loop(settings: Settings) -> None:
    from .main import strategy_loop

    strategy_loop(settings, "late_favorite", late_favorite_once)
