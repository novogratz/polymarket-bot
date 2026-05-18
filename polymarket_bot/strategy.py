"""Candidate ranking and stake helpers.

Turns Gamma market payloads into :class:`Candidate` instances, applies
liquidity / volume / horizon filters, and computes a watchlist score based
on tradability and urgency. The score is intentionally not an
expected-value estimate; it only orders markets for downstream filtering.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from .config import Settings
from .models import Candidate, as_float, is_excluded_market, parse_dt, parse_json_list, utc_now


def rank_markets(markets: list[dict[str, Any]], settings: Settings) -> list[Candidate]:
    now = utc_now()
    horizon = now + timedelta(hours=settings.soon_hours)
    candidates: list[Candidate] = []

    for market in markets:
        if is_excluded_market(market):
            continue
        end_date = parse_dt(market.get("endDate"))
        if end_date is None or end_date < now or end_date > horizon:
            continue

        liquidity = as_float(market.get("liquidity") or market.get("liquidityNum"))
        volume = as_float(market.get("volume") or market.get("volumeNum"))
        if liquidity < settings.min_liquidity_usd or volume < settings.min_volume_usd:
            continue

        market_best_bid = as_float(market.get("bestBid"), default=None) if market.get("bestBid") is not None else None
        market_best_ask = as_float(market.get("bestAsk"), default=None) if market.get("bestAsk") is not None else None
        tick_size = as_float(market.get("orderPriceMinTickSize"), default=None) if market.get("orderPriceMinTickSize") is not None else None
        neg_risk = bool(market.get("negRisk"))
        accepts_orders = bool(market.get("acceptingOrders"))
        outcomes = [str(item) for item in parse_json_list(market.get("outcomes"))]
        prices = [as_float(item, -1.0) for item in parse_json_list(market.get("outcomePrices"))]
        token_ids = [str(item) for item in parse_json_list(market.get("clobTokenIds"))]
        if not outcomes or len(outcomes) != len(prices):
            continue

        hours = max((end_date - now).total_seconds() / 3600.0, 0.0)
        for index, outcome in enumerate(outcomes):
            price = prices[index]
            if price <= 0.0 or price >= 1.0:
                continue

            # This is a liquidity/urgency watchlist score, not an expected-value claim.
            urgency = 1.0 / max(hours, 1.0)
            tradability = min(liquidity / 10_000.0, 3.0) + min(volume / 100_000.0, 2.0)
            fair_price_bias = 1.0 - abs(price - 0.5)
            score = (tradability * 2.0) + (urgency * 5.0) + fair_price_bias
            slug = str(market.get("slug") or market.get("id") or "")
            event_slug = _event_slug(market)
            best_bid, best_ask = _quote_for_outcome(index, len(outcomes), market_best_bid, market_best_ask)
            candidates.append(
                Candidate(
                    market_id=str(market.get("id") or ""),
                    question=str(market.get("question") or ""),
                    slug=slug,
                    end_date=end_date,
                    hours_to_close=hours,
                    liquidity=liquidity,
                    volume=volume,
                    outcome=outcome,
                    price=price,
                    token_id=token_ids[index] if index < len(token_ids) else None,
                    score=score,
                    url=f"https://polymarket.com/event/{event_slug or slug}" if (event_slug or slug) else "https://polymarket.com",
                    best_bid=best_bid,
                    best_ask=best_ask,
                    tick_size=tick_size,
                    neg_risk=neg_risk,
                    accepts_orders=accepts_orders,
                    event_slug=event_slug,
                )
            )

    return sorted(candidates, key=lambda item: item.score, reverse=True)


def build_pricing_candidates(markets: list[dict[str, Any]]) -> list[Candidate]:
    """Build Candidate instances purely for mark-to-market / exit pricing.

    Unlike :func:`rank_markets`, this skips the horizon and
    liquidity/volume filters — we need the current price of every open
    position regardless of how illiquid or close-to-expiry its market has
    become. Score is set to 0.0 so these candidates never bubble to the
    top of an entry ranking if they accidentally leak into one.
    """
    now = utc_now()
    candidates: list[Candidate] = []

    for market in markets:
        end_date = parse_dt(market.get("endDate"))
        liquidity = as_float(market.get("liquidity") or market.get("liquidityNum"))
        volume = as_float(market.get("volume") or market.get("volumeNum"))

        market_best_bid = as_float(market.get("bestBid"), default=None) if market.get("bestBid") is not None else None
        market_best_ask = as_float(market.get("bestAsk"), default=None) if market.get("bestAsk") is not None else None
        tick_size = as_float(market.get("orderPriceMinTickSize"), default=None) if market.get("orderPriceMinTickSize") is not None else None
        neg_risk = bool(market.get("negRisk"))
        accepts_orders = bool(market.get("acceptingOrders"))
        outcomes = [str(item) for item in parse_json_list(market.get("outcomes"))]
        prices = [as_float(item, -1.0) for item in parse_json_list(market.get("outcomePrices"))]
        token_ids = [str(item) for item in parse_json_list(market.get("clobTokenIds"))]
        if not outcomes or len(outcomes) != len(prices):
            continue

        hours = max((end_date - now).total_seconds() / 3600.0, 0.0) if end_date else 0.0
        slug = str(market.get("slug") or market.get("id") or "")
        event_slug = _event_slug(market)

        for index, outcome in enumerate(outcomes):
            price = prices[index]
            if price <= 0.0 or price >= 1.0:
                continue
            best_bid, best_ask = _quote_for_outcome(index, len(outcomes), market_best_bid, market_best_ask)
            candidates.append(
                Candidate(
                    market_id=str(market.get("id") or ""),
                    question=str(market.get("question") or ""),
                    slug=slug,
                    end_date=end_date,
                    hours_to_close=hours,
                    liquidity=liquidity,
                    volume=volume,
                    outcome=outcome,
                    price=price,
                    token_id=token_ids[index] if index < len(token_ids) else None,
                    score=0.0,
                    url=f"https://polymarket.com/event/{event_slug or slug}" if (event_slug or slug) else "https://polymarket.com",
                    best_bid=best_bid,
                    best_ask=best_ask,
                    tick_size=tick_size,
                    neg_risk=neg_risk,
                    accepts_orders=accepts_orders,
                    event_slug=event_slug,
                )
            )

    return candidates


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


def stake_for_candidate(candidate: Candidate, cash: float, settings: Settings) -> float:
    del candidate
    return round(max(0.0, min(cash, settings.max_position_usd)), 2)


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
