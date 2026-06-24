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
import json
import os
import random
import re
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import notifications
from .categories import classify_market, disabled_categories
from .config import Settings
from .forecast import build_context, evaluate_market, resolution_clarity
from .gamma import GammaClient
from .models import (
    SOCCER_MONEYLINE_MIN_ASK,
    Candidate,
    as_float,
    is_crypto_market,
    is_excluded_market,
    parse_dt,
    parse_json_list,
    utc_now,
)
from .news_strategy import _asset_key, _event_slug, _quote_for_outcome
from .portfolio import Portfolio
from .pricing import _fetch_clob_quotes, ensure_open_positions_in_pool
from .trading import build_client, execute_live_sell, execute_live_trade, live_best_bid


def _step(settings: Settings, msg: str) -> None:
    if not settings.quiet:
        print(msg, flush=True)


def _load_short_expiry_markets(settings: Settings, max_hours: float | None = None) -> list[dict[str, Any]]:
    client = GammaClient(settings.gamma_base_url)
    now = utc_now()
    horizon = now + timedelta(hours=max_hours if max_hours is not None else settings.race_max_hours)
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


# ---------------------------------------------------------------------------
# Forward-test logging — record every near-favorite the scan sees (a WIDE net,
# before the strict entry filters) so we can later measure realised edge per
# (price band, hours, momentum) on the markets the strategy actually meets.
# Historical CLOB price history is too sparse to validate the grinder edge, so
# this builds a clean forward dataset instead. Reconcile with
# scripts/reconcile_forward_log.py. No extra HTTP — iterates fetched markets.
# ---------------------------------------------------------------------------

_FWD_OBS_LO = 0.80
_FWD_OBS_HI = 0.995
_FWD_LOGGED_TOKENS: set[str] | None = None


def _forward_log_path() -> Path:
    return Path(os.getenv("POLYMARKET_FORWARD_LOG_PATH", "data/forward_eligible_log.jsonl"))


def _forward_log_enabled() -> bool:
    return os.getenv("POLYMARKET_FORWARD_LOG_ENABLED", "1").lower() in ("1", "true", "yes")


def _load_logged_tokens(path: Path) -> set[str]:
    seen: set[str] = set()
    if not path.is_file():
        return seen
    try:
        with path.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tok = str(rec.get("token_id") or "")
                if tok:
                    seen.add(tok)
    except OSError:
        pass
    return seen


def _log_forward_observations(markets: list[dict[str, Any]], settings: Settings) -> int:
    """Append first-seen observations of near-favorite outcomes (wide band).

    Deduped by token_id (across restarts via the existing file) so each token
    is captured once at the price/h2c the bot first observed it in-band.
    """
    global _FWD_LOGGED_TOKENS
    if not _forward_log_enabled():
        return 0
    path = _forward_log_path()
    if _FWD_LOGGED_TOKENS is None:
        _FWD_LOGGED_TOKENS = _load_logged_tokens(path)
    now = utc_now()
    rows: list[dict[str, Any]] = []
    for market in markets:
        if is_excluded_market(market):
            continue
        if not bool(market.get("acceptingOrders")):
            continue
        end_date = parse_dt(market.get("endDate"))
        if end_date is None or end_date <= now:
            continue
        market_best_bid = as_float(market.get("bestBid"), default=None)
        market_best_ask = as_float(market.get("bestAsk"), default=None)
        if market_best_bid is None or market_best_ask is None:
            continue
        outcomes = [str(item) for item in parse_json_list(market.get("outcomes"))]
        prices = [as_float(item, -1.0) for item in parse_json_list(market.get("outcomePrices"))]
        token_ids = [str(item) for item in parse_json_list(market.get("clobTokenIds"))]
        if len(outcomes) != 2 or len(token_ids) != 2:
            continue
        hours_to_close = max((end_date - now).total_seconds() / 3600.0, 0.0)
        one_day_change = as_float(market.get("oneDayPriceChange"), default=0.0)
        liquidity = as_float(market.get("liquidity") or market.get("liquidityNum"))
        volume_24h = as_float(market.get("volume24hr") or market.get("volume24hrClob"))
        for index, outcome in enumerate(outcomes):
            token = token_ids[index]
            if not token or token in _FWD_LOGGED_TOKENS:
                continue
            best_bid, best_ask = _quote_for_outcome(index, 2, market_best_bid, market_best_ask)
            if best_bid is None or best_ask is None:
                continue
            if best_ask < _FWD_OBS_LO or best_ask > _FWD_OBS_HI:
                continue
            rows.append(
                {
                    "ts": now.isoformat(),
                    "token_id": token,
                    "market_id": str(market.get("id") or ""),
                    "question": str(market.get("question") or ""),
                    "outcome": outcome,
                    "outcome_index": index,
                    "slug": str(market.get("slug") or ""),
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "spread": round(best_ask - best_bid, 4),
                    "outcome_price": prices[index] if index < len(prices) else None,
                    "hours_to_close": round(hours_to_close, 3),
                    "end_date": end_date.isoformat(),
                    "one_day_change": one_day_change,
                    "one_hour_change": as_float(market.get("oneHourPriceChange"), default=0.0),
                    "liquidity": liquidity,
                    "volume_24h": volume_24h,
                }
            )
            _FWD_LOGGED_TOKENS.add(token)
    if rows:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")
        except OSError:
            pass
    return len(rows)


def _build_eligible_candidates(
    markets: list[dict[str, Any]],
    settings: Settings,
    max_hours: float | None = None,
    disabled_categories: set[str] | None = None,
    forecast_ctx: dict[str, Any] | None = None,
) -> list[tuple[Candidate, float]]:
    """Per-outcome candidate with a momentum signal attached for ranking.

    Returns ``(candidate, momentum_for_this_outcome)``. Momentum is the
    YES-side ``oneDayPriceChange`` flipped for the NO side. Caller picks
    whichever sign matches their thesis (contrarian flips, favorite
    ignores momentum entirely). ``max_hours`` overrides the base window for
    the dynamic entry-window ladder. ``disabled_categories`` (v4) drops any
    market whose category has been auto-disabled by the data-driven governance.
    """
    now = utc_now()
    earliest = now + timedelta(minutes=5)
    horizon = now + timedelta(hours=max_hours if max_hours is not None else settings.race_max_hours)
    # v4 (user 2026-06-21): when unban_all_markets is on, every category is
    # allowed — governance shifts to the data-driven category auto-disable.
    unban_all = bool(getattr(settings, "unban_all_markets", False))
    disabled = disabled_categories or set()
    # v4 forecasting EV/quality gates (user 2026-06-21) — opt-in, both 0 = off.
    min_edge = float(getattr(settings, "race_min_edge", 0.0) or 0.0)
    min_quality = float(getattr(settings, "race_min_quality_score", 0.0) or 0.0)
    gates_on = forecast_ctx is not None and (min_edge > 0 or min_quality > 0)
    # v4 resolution-safety filter (user 2026-06-21) — ALWAYS-ON (no history
    # needed), so it protects against ambiguous/subjective settlement even
    # under unban_all. 0 disables it.
    min_clarity = float(getattr(settings, "race_min_resolution_clarity", 0.0) or 0.0)
    out: list[tuple[Candidate, float]] = []
    for market in markets:
        if not unban_all and is_excluded_market(market):
            continue
        # Crypto is ALWAYS banned, even under unban_all_markets (user 2026-06-24:
        # "ban crypto for all bots 1 2 3"). Like the resolution-safety filter it
        # ignores the unban flag — crypto Up/Down binaries have no convergence
        # edge and were a top loss category.
        if is_crypto_market(market):
            continue
        # v4 data-driven governance: drop auto-disabled categories.
        if disabled and classify_market(market) in disabled:
            continue
        # v4 resolution safety: skip subjective / ambiguous-settlement markets.
        if min_clarity > 0 and resolution_clarity(str(market.get("question") or "")) < min_clarity:
            continue
        end_date = parse_dt(market.get("endDate"))
        if end_date is None:
            continue
        # Entry window (user 2026-06-14): keep a market only if its GAME
        # STARTS within the next ``max_hours`` OR it CLOSES within the next
        # ``max_hours``. A game in progress that doesn't close inside the
        # window is dropped — only fast-resolving bets qualify.
        game_start = parse_dt(market.get("gameStartTime"))
        closes_soon = earliest <= end_date <= horizon
        starts_soon = game_start is not None and now <= game_start <= horizon
        if not (closes_soon or starts_soon):
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
        # NOTE (2026-06-10): no price-movement gates. The day-change gate
        # (>10% moved today) and the day-momentum floor (falling >5% today)
        # were removed along with the short-lived 1h gates — recently-moving
        # markets are often exactly the ones converging toward resolution and
        # the user wants them tradeable. one_day_change is still computed for
        # the selector momentum tuple and the forward-observation log.
        one_day_change = as_float(market.get("oneDayPriceChange"), default=0.0)
        # NOTE (2026-06-10): no oneHourPriceChange gate. A 1h flux filter was
        # added and removed the same day — recently-moving markets are often
        # exactly the ones converging toward resolution, and the user wants
        # them tradeable. The field is still logged in the forward-observation
        # net so its edge contribution can be measured before any future gate.

        # Esports is banned outright (user 2026-06-19) — no per-lane floor
        # needed; such markets never reach candidate selection.
        lane_min_price = settings.race_min_price
        # Soccer/sport "Will <X> win on <date>?" moneylines (user 2026-06-17):
        # gap-bombs below 0.92. EVERY moneyline loss ever entered at ≤ 0.90;
        # the 0.90+ band has zero losses. The SL can't save a goal-gap
        # (Difaâ "No" 0.89 → 0.02 sold by the SL → resolved 1.0) — control it
        # at entry. Both Yes and No sides gap, so floor the whole market.
        if _is_soccer_moneyline_text(question, slug):
            lane_min_price = max(lane_min_price, SOCCER_MONEYLINE_MIN_ASK)

        # v4 (user 2026-06-21): absolute entry-price ceiling — never buy above
        # the hard cap (0.96) no matter what race_max_price says, so 0.97/0.98/
        # 0.99 are never tradeable. 0 disables the clamp.
        lane_max_price = settings.race_max_price
        hard_cap = float(getattr(settings, "race_max_price_hard_cap", 0.0) or 0.0)
        if hard_cap > 0:
            lane_max_price = min(lane_max_price, hard_cap)

        for index, outcome in enumerate(outcomes):
            price = prices[index]
            if price <= 0.0 or price >= 1.0:
                continue
            best_bid, best_ask = _quote_for_outcome(index, 2, market_best_bid, market_best_ask)
            if best_bid is None or best_ask is None:
                continue
            if best_ask < lane_min_price or best_ask > lane_max_price:
                continue
            spread = best_ask - best_bid
            if spread < 0 or spread > settings.race_max_spread:
                continue
            # v4 EV / quality gates (opt-in): only trade positive-EV, high-
            # quality opportunities. The forecaster's edge =
            # predicted_probability − ask; quality blends edge, volume,
            # resolution clarity, and historical category/bucket ROI.
            if gates_on:
                ev = evaluate_market(
                    category=classify_market(market),
                    ask=best_ask,
                    volume_usd=volume_24h,
                    question=question,
                    ctx=forecast_ctx,
                    preferred_volume_usd=float(getattr(settings, "race_preferred_volume_usd", 5000.0)),
                )
                if min_edge > 0 and ev["edge"] < min_edge:
                    continue
                if min_quality > 0 and ev["quality"] < min_quality:
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
    # Rank by momentum strength; volume is a gate (already filtered above),
    # not a weight — multiplying mom×volume let a 0.5%×$500k move tie a
    # 5%×$50k move, defeating the momentum-continuation thesis.
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


def select_counter_panic_fade(
    eligible: list[tuple[Candidate, float]],
    n: int,
    min_panic_move: float,
    min_volume_24h: float,
) -> list[Candidate]:
    """Reverse of panic_fade: ride extreme intraday moves instead of fading.

    Same trigger (≥min_panic_move move, volume-confirmed) but bets WITH the
    move on the winning side, assuming momentum continues through expiry.
    """
    qualified = [
        (c, mom)
        for c, mom in eligible
        if mom >= min_panic_move and (c.volume or 0) >= min_volume_24h
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
# Framework Rules — 17 race-mode strategies derived from the spec.
#
# Many of these are *degraded* versions of the spec, because the polling-based
# data layer doesn't expose live orderbook depth, per-trade size, refill
# timing, or correlated-wallet history. Each docstring states what was
# approximated. Treat them as candidate variations to A/B-race, not as
# faithful implementations of the original ideas.
#
# Cross-cutting rules NOT implemented as entry modes (they're overlays):
#   #10 Spoof Detection Avoidance — needs orderbook depth telemetry.
#   #18 Volatility Regime Switching — needs a regime classifier on top.
#   #20 Confidence Weighted Positioning — sizing helper, see sizing layer.
# ---------------------------------------------------------------------------


def _dedupe_top_n(
    scored: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    scored.sort(key=lambda t: t[1], reverse=True)
    seen: set[str] = set()
    out: list[Candidate] = []
    for c, _ in scored:
        if c.market_id in seen:
            continue
        seen.add(c.market_id)
        out.append(c)
        if len(out) >= n:
            break
    return out


def select_hybrid_smart_money(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#0 MAIN: momentum + volume + tight spread + mid-price band."""
    qualified: list[tuple[Candidate, float]] = []
    for c, mom in eligible:
        if mom < 0.03 or (c.volume or 0) < 500.0:
            continue
        bid, ask = c.best_bid or 0.0, c.best_ask or 1.0
        if not (0 <= ask - bid <= 0.04):
            continue
        if not (0.15 <= ask <= 0.85):
            continue
        qualified.append((c, mom * (c.volume or 1.0)))
    return _dedupe_top_n(qualified, n)


def select_smart_wallet_consensus(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#1: high 24h volume as degraded proxy for multi-wallet activity."""
    qualified = [
        (c, c.volume or 0.0)
        for c, mom in eligible
        if (c.volume or 0) >= 1250.0 and mom > 0
    ]
    return _dedupe_top_n(qualified, n)


def select_whale_entry(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#2: outsized volume + positive momentum (whale-trade proxy)."""
    qualified = [
        (c, (c.volume or 0.0) * max(mom, 0.0))
        for c, mom in eligible
        if (c.volume or 0) >= 2500.0 and mom >= 0.01
    ]
    return _dedupe_top_n(qualified, n)


def select_elite_momentum_consensus(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """Smart high-frequency strategy: momentum + volume + tight spreads in liquid markets.
    
    Thesis: Best edge is continuation in markets that are:
    - Moving with purpose (momentum >= 2%)
    - Backed by real volume (>= $1000)
    - Not at extremes (price 0.15-0.85)
    - Cheap to trade (spread <= 0.06)
    - Near-term but not expiring (2-48h to close)
    
    Scores by momentum * volume * spread_efficiency to rank conviction.
    Designed to fire regularly while maintaining quality filters.
    """
    qualified: list[tuple[Candidate, float]] = []
    for c, mom in eligible:
        bid, ask = c.best_bid or 0.0, c.best_ask or 1.0
        spread = ask - bid
        hours = c.hours_to_close or 99.0
        
        if mom < 0.02:
            continue
        if (c.volume or 0) < 1000.0:
            continue
        if not (0.15 <= ask <= 0.85):
            continue
        if spread > 0.06 or spread < 0:
            continue
        if not (2.0 <= hours <= 48.0):
            continue
        
        score = mom * (c.volume or 1.0) * max(1.0 - spread, 0.5)
        qualified.append((c, score))
    
    return _dedupe_top_n(qualified, n)


def select_wallet_cluster(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#3: degraded — true correlation needs offline co-trading graph."""
    qualified = [
        (c, mom + (c.volume or 0.0) / 100000.0)
        for c, mom in eligible
        if (c.volume or 0) >= 750.0 and mom >= 0.01
    ]
    return _dedupe_top_n(qualified, n)


def select_early_momentum(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#4: small early move (2-8%) with volume floor — catch before expansion."""
    qualified = [
        (c, mom)
        for c, mom in eligible
        if 0.01 <= mom <= 0.05 and (c.volume or 0) >= 250.0
    ]
    return _dedupe_top_n(qualified, n)


def select_liquidity_vacuum(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#5: thin liquidity + breakout momentum — exploit shallow asks."""
    qualified = [
        (c, mom)
        for c, mom in eligible
        if (c.liquidity or 0) < 5000.0 and mom >= 0.005
    ]
    return _dedupe_top_n(qualified, n)


def select_mean_reversion_fade(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#6: fade modest emotional dumps (-5%..-10%, smaller than panic_fade)."""
    qualified = [
        (c, -mom)
        for c, mom in eligible
        if -0.10 <= mom <= -0.015 and (c.volume or 0) >= 250.0
    ]
    return _dedupe_top_n(qualified, n)


def select_range_channel(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#7: low-volatility mid-range markets (no time-series → coarse proxy)."""
    qualified = [
        (c, -abs(mom))
        for c, mom in eligible
        if abs(mom) <= 0.02
        and 0.30 <= (c.best_ask or 1.0) <= 0.55
        and (c.volume or 0) >= 125.0
    ]
    return _dedupe_top_n(qualified, n)


def select_aggressive_buyer(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#8: strong momentum + high volume (aggressive bidding proxy)."""
    qualified = [
        (c, mom * (c.volume or 1.0))
        for c, mom in eligible
        if mom >= 0.03 and (c.volume or 0) >= 1000.0
    ]
    return _dedupe_top_n(qualified, n)


def select_orderbook_imbalance(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#9: tight spread + mid-price + positive momentum (depth proxy)."""
    qualified: list[tuple[Candidate, float]] = []
    for c, mom in eligible:
        bid, ask = c.best_bid or 0.0, c.best_ask or 1.0
        if not (0 <= ask - bid <= 0.05):
            continue
        if mom < 0.01:
            continue
        if not (0.15 <= ask <= 0.85):
            continue
        qualified.append((c, mom))
    return _dedupe_top_n(qualified, n)


def select_late_momentum_chase(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#11: confirmed continuation — high momentum within 90 min of expiry."""
    qualified = [
        (c, mom)
        for c, mom in eligible
        if mom >= 0.015
        and (c.hours_to_close or 99) <= 4.0
        and (c.volume or 0) >= 100.0
    ]
    return _dedupe_top_n(qualified, n)


def select_weak_holder_flush(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#12: buy panic dumps (≥10% drop) with volume — like panic_fade, harsher."""
    qualified = [
        (c, -mom)
        for c, mom in eligible
        if mom <= -0.05 and (c.volume or 0) >= 500.0
    ]
    return _dedupe_top_n(qualified, n)


def select_claude_oversold_bounce(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """claude #1: extreme dump (mom ≤-15%) at low price (ask ≤0.30) bounces.

    Combines panic_fade thesis with a low-price filter. The asymmetry
    is structural: from $0.20 the upside to $0.30 is +50% while
    downside to $0.10 is only -50% (and zero is a hard floor). Higher
    win rate from buying near-the-floor capitulations.
    """
    qualified = [
        (c, -mom)
        for c, mom in eligible
        if mom <= -0.075
        and (c.best_ask or 1.0) <= 0.30
        and (c.volume or 0) >= 375.0
    ]
    return _dedupe_top_n(qualified, n)


def select_claude_late_pump(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """claude #2: chase strong momentum in the last 30 minutes.

    Late breakouts (mom ≥+10%, ≤30min to expiry, vol ≥$2k) have no
    time left to mean-revert. Markets that move sharply just before
    resolution usually resolve on the same side. Pure trend snipe.
    """
    qualified = [
        (c, mom)
        for c, mom in eligible
        if mom >= 0.015
        and (c.hours_to_close or 99.0) <= 3.0
        and (c.volume or 0) >= 100.0
    ]
    return _dedupe_top_n(qualified, n)


def select_claude_extreme_consensus(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """claude #3: ALL 4 multi-signal conditions agree (#19 only needs 3).

    Stricter than multi_signal_consensus: momentum ≥+3% AND vol ≥$3k
    AND spread ≤3¢ AND ask in 20-80¢. Fewer fires but every entry
    has full multi-factor confirmation.
    """
    qualified: list[tuple[Candidate, float]] = []
    for c, mom in eligible:
        bid, ask = c.best_bid or 0.0, c.best_ask or 1.0
        if (
            mom >= 0.015
            and (c.volume or 0) >= 750.0
            and 0 <= ask - bid <= 0.03
            and 0.20 <= ask <= 0.80
        ):
            qualified.append((c, mom))
    return _dedupe_top_n(qualified, n)


def select_claude_balanced_mid(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """claude #4: coin-flip markets (ask 0.45-0.55) with confirming momentum.

    50/50 markets that break direction often run hard — indecision
    was masking real flow. Entering on the earliest +5% confirmation
    with $3k volume catches the start of the directional move.
    """
    qualified = [
        (c, mom)
        for c, mom in eligible
        if 0.35 <= (c.best_ask or 0.5) <= 0.65
        and mom >= 0.01
        and (c.volume or 0) >= 150.0
    ]
    return _dedupe_top_n(qualified, n)


def select_claude_resolution_clock(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """claude #5: ultra-late favorite snipe (bid ≥0.80, ≤15min left).

    Final 15 minutes, strong favorites resolve. Limited upside
    (entry 0.85 → resolve 1.00 = +17%) but high win rate and very
    short hold. Friction is the main risk; min_edge filter helps.
    """
    qualified = [
        (c, c.best_bid or 0.0)
        for c, _ in eligible
        if (c.best_bid or 0.0) >= 0.65
        and (c.hours_to_close or 99.0) <= 1.0
    ]
    return _dedupe_top_n(qualified, n)


def select_claude_endgame_sweep(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """claude #6: broad-favorite chaser, bid 0.65-0.985 + ≤2h.

    NOTE: name kept for continuity but no longer matches the original
    Datawallet "endgame" thesis (which validated 0.92+ only). At 0.65
    we're betting on moderate favorites with TP/SL asymmetry doing
    the work: +25% TP on a 65% favorite has positive EV after fees
    (gross ~+12%/trade) IF the win rate matches the implied
    probability. This is speculative — no research backing at this
    bid floor.
    """
    qualified = [
        (c, c.best_bid or 0.0)
        for c, _ in eligible
        if 0.65 <= (c.best_bid or 0.0) <= 0.985
        and (c.best_ask or 1.0) <= 0.97
        and (c.hours_to_close or 99.0) <= 4.0
        and round((c.best_ask or 1.0) - (c.best_bid or 0.0), 4) <= 0.05
        and (c.volume or 0) >= 50.0
    ]
    return _dedupe_top_n(qualified, n)


def select_claude_fade_extreme(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """claude #7: fade the extreme underdog on liquid sentiment markets.

    WEEX top-1% wallet pattern: buy ask ≤0.12 on $10k+ vol markets.
    Thesis — sentiment-driven names overshoot. Asymmetric reward
    (entry 0.10 → 0.50 = +400%) compensates the lower win rate.
    """
    qualified = [
        (c, -(c.best_ask or 1.0))  # rank by cheapest first
        for c, _ in eligible
        if (c.best_ask or 1.0) <= 0.12
        and (c.volume or 0) >= 2500.0
    ]
    return _dedupe_top_n(qualified, n)


def select_claude_mid_volume_band(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """claude #8: mid-volume calibration sweet spot (TradeTheOutcome).

    Markets with vol ≥$20k + mid-priced (10-90¢) + relative spread
    ≤4% sit in the band where retail noise persists but liquidity
    allows exits. Calibration peaks here.
    """
    qualified: list[tuple[Candidate, float]] = []
    for c, _ in eligible:
        ask = c.best_ask or 1.0
        bid = c.best_bid or 0.0
        if (c.volume or 0) < 5000.0:
            continue
        if not (0.10 <= ask <= 0.90):
            continue
        if ask <= 0 or (ask - bid) / ask > 0.04:
            continue
        qualified.append((c, c.volume or 0.0))
    return _dedupe_top_n(qualified, n)


def select_claude_blue_chip(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """claude #9: top-volume markets with very tight absolute spread.

    Calibration is 88-93% on markets with vol >$50k. Tight 2¢
    absolute spread (vs relative) clears Polymarket fees cleanly.
    Lowest-noise band, fewest fires.
    """
    qualified = [
        (c, c.volume or 0.0)
        for c, _ in eligible
        if (c.volume or 0) >= 12500.0
        and round((c.best_ask or 1.0) - (c.best_bid or 0.0), 4) <= 0.03
        and 0.05 <= (c.best_ask or 1.0) <= 0.95
    ]
    return _dedupe_top_n(qualified, n)


def select_claude_mid_endgame(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """claude #11: mid-favorite endgame (bid 0.80-0.95 + ≤1h to close).

    Fills the gap between resolution_clock (0.80+ / ≤15min, very tight)
    and endgame_sweep (0.92+ / ≤2h). Wider price band catches more
    fires; 1h window keeps it short enough to limit news risk.
    Spread ≤2¢ + vol ≥$500 = executable.
    """
    qualified = [
        (c, c.best_bid or 0.0)
        for c, _ in eligible
        if (c.best_bid or 0.0) >= 0.80
        and (c.best_ask or 1.0) <= 0.95
        and (c.hours_to_close or 99.0) <= 1.0
        and round((c.best_ask or 1.0) - (c.best_bid or 0.0), 4) <= 0.03
        and (c.volume or 0) >= 125.0
    ]
    return _dedupe_top_n(qualified, n)


def select_claude_volume_spike(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """claude #10: disproportionate trading interest (vol/liquidity ≥3).

    When volume traded is ≥3x the resting orderbook depth, retail
    is pushing the market harder than the makers expected. Signal
    of news/sentiment flow. Mid-priced only to avoid extremes.
    """
    qualified: list[tuple[Candidate, float]] = []
    for c, _ in eligible:
        liq = c.liquidity or 0.0
        vol = c.volume or 0.0
        ask = c.best_ask or 1.0
        if liq <= 0 or vol / liq < 3.0:
            continue
        if not (0.15 <= ask <= 0.85):
            continue
        qualified.append((c, vol / liq))
    return _dedupe_top_n(qualified, n)


def select_weak_holder_flush_inverse(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """Inverse of #12: buy the *other* side of the same dump.

    Each market emits YES and NO as separate candidates with signed
    momentum. WHF buys the dumped outcome (mom ≤ -10%) expecting a
    bounce; this picks the opposing outcome (mom ≥ +10%) — betting
    the move is real and the dumped side keeps bleeding.
    """
    qualified = [
        (c, mom)
        for c, mom in eligible
        if mom >= 0.05 and (c.volume or 0) >= 500.0
    ]
    return _dedupe_top_n(qualified, n)


def select_probability_drift(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#13: slow steady directional move (1.5-6% momentum, mid-price)."""
    qualified = [
        (c, mom)
        for c, mom in eligible
        if 0.01 <= mom <= 0.05
        and (c.volume or 0) >= 150.0
        and 0.15 <= (c.best_ask or 1.0) <= 0.85
    ]
    return _dedupe_top_n(qualified, n)


def select_resolution_compression(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#14: favorites near expiry, looser bid floor than late_favorite."""
    qualified = [
        (c, -(c.hours_to_close or 99))
        for c, _ in eligible
        if (c.best_bid or 0) >= 0.55 and (c.hours_to_close or 99) <= 2.0
    ]
    return _dedupe_top_n(qualified, n)


def select_liquidity_absorption(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#15: small drop (-2..-6%) absorbed by volume — refill-time proxy."""
    qualified = [
        (c, c.volume or 0.0)
        for c, mom in eligible
        # Tightened: was -0.15..0 which accepted -14% as "small drop";
        # docstring says -2%..-6%, so honor the thesis. High-volume on
        # the small dip is the absorption signal (buyers stepped in).
        if -0.06 <= mom <= -0.02 and (c.volume or 0) >= 1000.0
    ]
    return _dedupe_top_n(qualified, n)


def select_momentum_exhaustion(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#16: near-zero momentum + prior high volume (exhaustion proxy)."""
    qualified = [
        (c, c.volume or 0.0)
        for c, mom in eligible
        if abs(mom) <= 0.015 and (c.volume or 0) >= 1250.0
    ]
    return _dedupe_top_n(qualified, n)


def select_micro_scalping(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#17: tightest-spread mid-price markets — polling-rate scalping only."""
    qualified: list[tuple[Candidate, float]] = []
    for c, _mom in eligible:
        bid, ask = c.best_bid or 0.0, c.best_ask or 1.0
        spread = ask - bid
        if not (0 <= spread <= 0.02):
            continue
        if not (0.30 <= ask <= 0.70):
            continue
        qualified.append((c, -spread))
    return _dedupe_top_n(qualified, n)


def select_multi_signal_consensus(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#19: require ≥3 of {momentum, volume, tight spread, mid-price}."""
    qualified: list[tuple[Candidate, float]] = []
    for c, mom in eligible:
        signals = 0
        if mom >= 0.015:
            signals += 1
        if (c.volume or 0) >= 750.0:
            signals += 1
        bid, ask = c.best_bid or 0.0, c.best_ask or 1.0
        if 0 <= ask - bid <= 0.03:
            signals += 1
        if 0.20 <= ask <= 0.80:
            signals += 1
        if signals >= 3:
            qualified.append((c, signals + mom))
    return _dedupe_top_n(qualified, n)


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


def _is_sports_total_position(position: dict[str, Any]) -> bool:
    question = str(position.get("question") or "").lower()
    outcome = str(position.get("outcome") or "").lower()
    return (
        outcome in {"over", "under"}
        and (
            "o/u" in question
            or "over/under" in question
            or "total" in question
        )
    )


# Soccer moneyline questions print as "Will <Team> win on <YYYY-MM-DD>?" with
# Yes/No outcomes (totals print "X vs. Y: O/U 4.5" with Over/Under outcomes,
# election markets say "win the most seats" — neither matches).
_SOCCER_MONEYLINE_RE = re.compile(r"^will .+ win on \d{4}-\d{2}-\d{2}\?$")

# Keywords that EXCLUDE a "Will <X> win on <date>?" market from the SL lane.
# The SL is for live SPORT moneylines (a team can collapse mid-game on a goal);
# elections/politics and the other-sport markets that share the question format
# must NOT stop out — they ride to resolution. (2026-06-16: flipped from a
# brittle soccer-league WHITELIST to this exclusion model after América FC's
# "Will América FC win on 2026-06-16?" got no SL — its slug lacked any league
# keyword — and rode 0.88 → 0.30.)
_NON_SPORT_MONEYLINE_KEYWORDS = (
    # politics / elections — never stop out (rides to resolution)
    "election", "primary", "governor", "senate", "president", "presidential",
    "mayor", "mayoral", "nominee", "congress", "parliament", "referendum",
    "ballot", "caucus", "approval",
    # non-team / awards / metrics that can phrase as "win on <date>"
    "award", "nobel", "palme",
)


def _is_soccer_moneyline_text(question: str, slug: str) -> bool:
    """True for a SPORT team-win moneyline market by its text alone.

    Exclusion model (2026-06-16): the question must match
    "Will <X> win on YYYY-MM-DD?" and neither question nor slug may carry a
    non-sport keyword (elections/politics/awards). Any soccer club / national
    team passes regardless of league. Used both by the entry floor (both Yes
    and No sides of such a market gap on a goal) and the SL gate.
    """
    q = str(question or "").strip().lower()
    if not _SOCCER_MONEYLINE_RE.match(q):
        return False
    haystack = f"{q} {str(slug or '').lower()}"
    return not any(kw in haystack for kw in _NON_SPORT_MONEYLINE_KEYWORDS)


def _is_soccer_moneyline_position(position: dict[str, Any]) -> bool:
    """True for a live SPORT team-win Yes/No moneyline — the ONLY SL lane.

      1. outcome must be Yes or No
      2. the market text must be a sport moneyline (_is_soccer_moneyline_text)
    Any soccer club / national team passes regardless of league, so games
    like "Will América FC win on 2026-06-16?" are now covered.
    """
    outcome = str(position.get("outcome") or "").strip().lower()
    if outcome not in {"yes", "no"}:
        return False
    return _is_soccer_moneyline_text(
        str(position.get("question") or ""),
        str(position.get("slug") or position.get("event_slug") or ""),
    )


def _simple_exit_plan(
    position: dict[str, Any],
    current_pnl_pct: float,
    settings: Settings,
    decision_bid: float = 0.0,
) -> dict[str, Any] | None:
    shares = float(position.get("shares", 0.0) or 0.0)
    if shares <= 0:
        return None
    # Arb positions hold to resolution — they exit via resolved_exit (≥0.97)
    # or the universal sweep (≤0.03). TP/SL would close one leg prematurely,
    # leaving the other leg unhedged.
    if position.get("is_arb"):
        return None
    # Universal min-hold: no sell of any kind before sl_min_age_minutes.
    age = _position_age_minutes(position)
    if age < settings.race_sl_min_age_minutes:
        return None
    if current_pnl_pct >= settings.race_tp_pct:
        return {"reason": "race_take_profit", "shares": shares}

    # ── CONTROLLED multi-tick stop-loss — SOCCER MONEYLINE ONLY (2026-06-07) ──
    # The blanket SL was removed 2026-05-31 because a ONE-tick thin-book phantom
    # bid (an Under at true 0.94 momentarily showing 0.46) made it dump winning
    # favorites for a fake -48%. The fix is confirmation, not absence: the loss
    # must persist for `race_sl_confirm_ticks` CONSECUTIVE ticks before we sell,
    # so a single-tick blip can never trigger it. Disabled when sl_pct >= 1.0.
    # The streak counter lives on the position dict (persisted in the ledger).
    # Scope (user rule 2026-06-07): ONLY team-win Yes/No bets get the SL. All
    # other markets — O/U 4.5 totals, anything non-moneyline — NEVER stop out.
    sl_pct = float(settings.race_sl_pct or 0.0)
    if 0.0 < sl_pct < 1.0 and _is_soccer_moneyline_position(position):
        if current_pnl_pct <= -sl_pct:
            # ── ANTI-GAP GUARD (2026-06-17) ──────────────────────────────
            # The SL must only stop out into an ORDERLY decline. A bid that
            # has gapped far below the -sl_pct level is a goal-gap crash, not
            # a controlled stop — and those mean-revert: Difaâ "No" went
            # 0.8949 → 0.02 (Difaâ scored), the SL sold the bottom at 2¢, then
            # Maghreb won so "No" resolved to 1.0 — a +$2.55 winner booked as
            # a -$21.25 loss. If the live bid is below race_sl_min_exit_price,
            # HOLD to on-chain resolution instead of dumping into the crash.
            floor = float(settings.race_sl_min_exit_price or 0.0)
            if floor > 0.0 and decision_bid > 0.0 and decision_bid < floor:
                position["sl_confirm_count"] = 0
                return None
            count = int(position.get("sl_confirm_count", 0) or 0) + 1
            position["sl_confirm_count"] = count
            confirm_needed = max(1, int(settings.race_sl_confirm_ticks))
            if count >= confirm_needed:
                return {"reason": "race_stop_loss_confirmed", "shares": shares}
        elif position.get("sl_confirm_count"):
            # recovered back above the threshold — reset the confirmation streak
            position["sl_confirm_count"] = 0
    return None


# Minutes past close before we even consider realizing a stuck position.
# Per user rule (2026-05-31): a losing position is NEVER sold (the loss-floor in
# execute_live_sell blocks any sell below entry). It rides to natural on-chain
# settlement; only ~8h after expiry do we register the loss LOCALLY (the sell
# attempt is floor-blocked → written off in the ledger, no order placed). 8h
# gives the chain ample time to settle a real win to ~1.0 first.
RACE_EXPIRY_GRACE_MIN = 480  # 8h
# Winners ride to resolved_exit (bid ≥ 0.97). Long grace as a backstop only.
RACE_EXPIRY_GRACE_MIN_WINNING = 480

# EOD pre-sell REMOVED (2026-05-31): it force-flattened every open position
# ~5 min before the daily report to make the summary show clean closed P&L —
# dumping winning favorites mid-game for a cosmetic report. The report now
# shows open-position equity instead; positions ride to resolution.


def _lookup_open_market(token_id: str, settings: Settings) -> dict[str, Any] | None:
    """Return the live market dict if it is STILL OPEN (accepting orders).

    Sports markets routinely carry an ``endDate`` set BEFORE kickoff (observed
    2026-05-31: endDate 01:00 UTC but gameStartTime 02:00 UTC), and a position
    past its stored endDate drops out of the scan — so the only reliable way to
    know whether a market is genuinely closed is to look it up live. Returns
    None when the market is closed/resolved or the lookup fails (callers must
    treat None conservatively, never as "safe to dump").
    """
    if not token_id:
        return None
    try:
        markets = GammaClient(settings.gamma_base_url).get_markets_by_clob_token_ids([str(token_id)])
    except Exception:
        return None
    for m in markets:
        toks = [str(t) for t in parse_json_list(m.get("clobTokenIds"))]
        if str(token_id) in toks and bool(m.get("acceptingOrders")) and not bool(m.get("closed")):
            return m
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
        # Manage own-tagged + live_sync positions (the latter being
        # positions synced from the CLOB account that this strategy
        # didn't open itself). Without this, switching live strategies
        # orphans the previous one's open positions until expiry.
        position_strategy = str(position.get("strategy") or "")
        if position_strategy != strategy_name and position_strategy != "live_sync":
            continue
        token_id = position.get("token_id")
        candidate = by_token.get(token_id)
        position_price = as_float(position.get("current_price"), default=0.0)

        # ── Live-book bid probe (2026-06-10) ─────────────────────────────
        # Gamma's flipped quote and the synced curPrice both lag the CLOB:
        # winners sat at a real 0.99 bid while this loop saw 0.95 and never
        # fired the resolved exit. The live book is the executable truth —
        # lift the decision price to it so the resolved sell triggers (and
        # prices) off what is actually fillable right now.
        if not settings.dry_run and token_id:
            live_bid = live_best_bid(client, str(token_id))
            if live_bid is not None and live_bid > position_price:
                position_price = live_bid

        # ── Past-expiry force-close ──────────────────────────────────────
        # A position whose market is comfortably past its close time has
        # effectively resolved off-chain but can linger in limbo: it drops out
        # of the scan (no fresh candidate) and sits priced mid-band (below the
        # resolved threshold), so it slips through TP/SL/resolved/near-expiry
        # and stays open forever — the "5 stale open positions" bug. Realize
        # it at the best price we have so the ledger reflects reality.
        mtc = _minutes_to_close(position)
        if mtc is not None and mtc < 0:
            exit_candidate = by_token.get(token_id)

            # ── FIX #1+#2 (2026-05-31): never force-close a STILL-OPEN market ──
            # Gamma's endDate is often set before kickoff for sports (endDate
            # 01:00 vs gameStartTime 02:00). Trusting it dumped a winning Under
            # (real value ~0.91) into a 0.10 thin-game bid for -$16. A market
            # past its stored endDate drops from the scan, so confirm live.
            open_mkt: Any = True if (exit_candidate is not None and exit_candidate.accepts_orders) else _lookup_open_market(token_id, settings)
            if open_mkt:
                # Still trading → NOT expired. Push end_date past the real game
                # window (gameStartTime + 6h) so we stop re-triggering, and let
                # it ride to a genuine resolved_exit / universal sweep.
                gst = parse_dt(str(open_mkt.get("gameStartTime") or "")) if isinstance(open_mkt, dict) else None
                position["end_date"] = ((gst or utc_now()) + timedelta(hours=6)).isoformat()
                portfolio.save(settings.state_path)
                print(
                    f"🛡️  {strategy_name} held '{position.get('question')}' — market still OPEN "
                    f"past endDate (early endDate vs gameStartTime); not force-closing.",
                    flush=True,
                )
                continue

            # Market is confirmed closed / unavailable → realize.
            salvage = max(
                position_price,
                float((exit_candidate.best_bid or 0.0) if exit_candidate else 0.0),
                0.0,
            )
            # ── FIX #3: judge winning by ENTRY price, not a thin-book bid ──
            # The grinder only buys favorites (entry ≥ 0.85), so a collapsed bid
            # must never reclassify one as a loser and trigger a fire-sale. A
            # favorite gets the 6h grace to resolve naturally; only a genuinely
            # low-entry/confirmed-loser position uses the short grace.
            entry_px = as_float(position.get("entry_price"), default=0.0)
            winning = max(salvage, entry_px) >= 0.50
            grace = RACE_EXPIRY_GRACE_MIN_WINNING if winning else RACE_EXPIRY_GRACE_MIN
            if mtc > -grace:
                continue
            if exit_candidate is None and token_id:
                exit_candidate = Candidate(
                    market_id=str(position.get("market_id") or ""),
                    question=str(position.get("question") or ""),
                    slug=str(position.get("slug") or ""),
                    end_date=parse_dt(str(position.get("end_date") or "")) if position.get("end_date") else None,
                    hours_to_close=0.0,
                    liquidity=0.0,
                    volume=0.0,
                    outcome=str(position.get("outcome") or ""),
                    price=salvage,
                    token_id=str(token_id),
                    score=0.0,
                    url=str(position.get("url") or "https://polymarket.com"),
                    best_bid=min(salvage, 0.99),
                    best_ask=None,
                    tick_size=as_float(position.get("tick_size"), default=0.01),
                    neg_risk=bool(position.get("neg_risk")),
                    accepts_orders=True,
                    event_slug=str(position.get("event_slug") or ""),
                )
            shares = float(position.get("shares", 0.0) or 0.0)
            if exit_candidate is None or shares <= 0:
                continue
            try:
                result = execute_live_sell(
                    client, settings, exit_candidate, portfolio, position,
                    shares=shares, reason="race_expired_close",
                )
                portfolio.save(settings.state_path)
                if position.get("status") == "closed":
                    from .main import _append_trade_journal
                    _append_trade_journal(settings, position, "race_expired_close")
                out.append({
                    "market_id": position.get("market_id"),
                    "question": position.get("question"),
                    "action": "sell",
                    "reason": "race_expired_close",
                    "order": result.order,
                    "response": result.response,
                })
                print(
                    f"⏳ {strategy_name} closed expired '{position.get('question')}' "
                    f"@ {salvage:.3f} ({-mtc:.0f}min past close)",
                    flush=True,
                )
            except Exception as exc:
                # Market closed → live SELL likely rejected. Write off locally
                # at the salvage price so it stops lingering.
                portfolio.record_live_exit(
                    position, shares=shares, exit_price=salvage, order_id=None,
                    order_response={"writeoff": True, "reason": f"expired:{exc}"},
                    reason="race_expired_close",
                )
                portfolio.save(settings.state_path)
                if position.get("status") == "closed":
                    from .main import _append_trade_journal
                    _append_trade_journal(settings, position, "race_expired_close")
                out.append({
                    "market_id": position.get("market_id"),
                    "question": position.get("question"),
                    "action": "writeoff",
                    "reason": "race_expired_close",
                    "exit_price": salvage,
                })
                print(
                    f"⏳ {strategy_name} wrote off expired '{position.get('question')}' "
                    f"@ {salvage:.3f} (SELL blocked: {exc})",
                    flush=True,
                )
            continue
        # ─────────────────────────────────────────────────────────────────

        # ── Resting limit sell (Option B) ────────────────────────────────
        # When price >= race_limit_sell_trigger (e.g. 0.95), place a single
        # GTC limit sell at race_limit_sell_price (e.g. 0.98) and track it.
        # Each tick: if shares gone → fill confirmed, record exit and move on.
        # If still pending → skip normal exit logic (let the resting order work).
        # Market auto-redemption at 1.0 is handled by live sync (shares hit 0).
        _lim_trigger = settings.race_limit_sell_trigger
        _lim_price = settings.race_limit_sell_price
        if _lim_trigger > 0 and candidate is not None and candidate.token_id:
            pending_oid = str(position.get("pending_exit_order_id") or "")
            if pending_oid:
                # Check if the resting sell filled by querying on-chain shares
                _filled = False
                if not settings.dry_run:
                    try:
                        on_chain = client.live_share_balance(str(token_id or ""))
                        if on_chain is not None and on_chain <= 0.001:
                            _filled = True
                    except Exception:
                        pass
                else:
                    _filled = True  # dry-run: simulate immediate fill
                if _filled:
                    _exit_price = float(position.get("pending_exit_price") or _lim_price)
                    _shares = float(position.get("shares") or 0.0)
                    portfolio.record_live_exit(
                        position,
                        shares=_shares,
                        exit_price=_exit_price,
                        order_id=pending_oid,
                        order_response={"orderID": pending_oid, "status": "matched_resting"},
                        reason="race_limit_sell_filled",
                    )
                    out.append({
                        "market_id": position.get("market_id"),
                        "question": position.get("question"),
                        "action": "sell",
                        "reason": "race_limit_sell_filled",
                        "exit_price": _exit_price,
                        "shares": _shares,
                    })
                    print(
                        f"✅ Resting limit sell FILLED: '{position.get('question')}' @ {_exit_price}",
                        flush=True,
                    )
                else:
                    # Still resting — skip normal exit logic this tick
                    continue
            elif position_price >= _lim_trigger:
                # Place the resting GTC limit sell
                _shares = float(position.get("shares") or 0.0)
                if _shares > 0.001:
                    try:
                        if not settings.dry_run:
                            _order, _resp = client.place_live_order(
                                candidate=candidate,
                                price=_lim_price,
                                size=round(_shares, 6),
                                side="SELL",
                            )
                            _oid = (_resp.get("orderID") if isinstance(_resp, dict) else None) or ""
                        else:
                            _oid = f"dry-limit-sell-{int(position_price * 1000)}"
                        if _oid:
                            position["pending_exit_order_id"] = _oid
                            position["pending_exit_price"] = _lim_price
                            portfolio.save(settings.state_path)
                            out.append({
                                "market_id": position.get("market_id"),
                                "question": position.get("question"),
                                "action": "limit_sell_placed",
                                "reason": "race_limit_sell_resting",
                                "exit_price": _lim_price,
                                "shares": _shares,
                                "order_id": _oid,
                            })
                            print(
                                f"📋 Resting limit sell @ {_lim_price} for "
                                f"'{position.get('question')}' "
                                f"({_shares:.4f} shares, order={_oid[:12]}...)",
                                flush=True,
                            )
                            continue  # skip normal exit logic this tick
                    except Exception as _exc:
                        print(
                            f"⚠️  Resting limit sell failed for '{position.get('question')}': {_exc}",
                            flush=True,
                        )
                        # fall through to normal exit logic

        # v4 (user 2026-06-21): one flat 0.99 winner exit across every lane —
        # the fast-lane 0.98 downgrade is removed. A winner sells only at a
        # real 0.99 bid, else rides to on-chain settlement at 1.00.
        resolved_threshold = settings.race_resolved_exit_threshold
        # Dynamic take-profit (user 2026-06-15): the exit must clear the entry
        # by race_min_profit_margin, capped at 0.99. With resolved_exit at 0.99
        # every winner targets 0.99. If the bid never prints, the position
        # simply rides to on-chain settlement at 1.00.
        pos_entry = float(position.get("entry_price", 0.0) or 0.0)
        if resolved_threshold > 0 and pos_entry > 0:
            resolved_threshold = min(
                0.99, max(resolved_threshold, pos_entry + settings.race_min_profit_margin)
            )
        position_resolved = (
            resolved_threshold > 0 and position_price >= resolved_threshold
        )
        if candidate is None and position_resolved and token_id:
            candidate = Candidate(
                market_id=str(position.get("market_id") or ""),
                question=str(position.get("question") or ""),
                slug=str(position.get("slug") or ""),
                end_date=parse_dt(str(position.get("end_date") or "")) if position.get("end_date") else None,
                hours_to_close=0.0,
                liquidity=0.0,
                volume=0.0,
                outcome=str(position.get("outcome") or ""),
                price=position_price,
                token_id=str(token_id),
                score=0.0,
                url=str(position.get("url") or "https://polymarket.com"),
                best_bid=min(position_price, 0.99),
                best_ask=None,
                tick_size=as_float(position.get("tick_size"), default=0.01),
                neg_risk=bool(position.get("neg_risk")),
                accepts_orders=True,
                event_slug=str(position.get("event_slug") or ""),
            )
        elif candidate is not None and position_resolved and (
            candidate.best_bid is None or candidate.best_bid < position_price
        ):
            candidate = replace(candidate, best_bid=min(position_price, 0.99))
        if candidate is None or candidate.best_bid is None or candidate.best_bid <= 0:
            continue
        entry_price = float(position.get("entry_price", 0.0) or 0.0)
        if entry_price <= 0:
            continue
        decision_bid = max(float(candidate.best_bid or 0.0), position_price)
        if decision_bid > float(candidate.best_bid or 0.0):
            candidate = replace(candidate, best_bid=min(decision_bid, 0.99))
        current_pnl_pct = (decision_bid - entry_price) / entry_price
        position["peak_pnl_pct"] = max(
            float(position.get("peak_pnl_pct", current_pnl_pct)), current_pnl_pct
        )
        if resolved_threshold > 0 and candidate.best_bid >= resolved_threshold:
            plan = {"reason": "race_big_win_resolved", "shares": float(position.get("shares", 0.0))}
        else:
            # eod_close REMOVED (2026-05-31): it flattened EVERY open position
            # ~5 min before the daily report just to show clean closed P&L —
            # dumping winning favorites mid-game at whatever (often thin-book)
            # bid existed, losing real money for a cosmetic report. Positions
            # now ride to resolution; the report shows open-position equity.
            plan = _simple_exit_plan(position, current_pnl_pct, settings, decision_bid=decision_bid)
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
            msg = str(exc).lower()
            # Stuck-balance: the CLOB says we don't have enough shares to
            # sell. Try cancelling resting orders first (may free shares);
            # if that fails (most cancel methods 405/400 on current CLOB),
            # force-close the local position so the bot stops spamming
            # this market forever. User can recover manually if shares
            # turn up later.
            if "balance is not enough" in msg or "allowance" in msg:
                token_id_str = str(position.get("token_id") or "")
                cancelled: list[str] = []
                if token_id_str:
                    try:
                        cancelled = client.cancel_active_orders_for_token(token_id_str)
                    except Exception as cancel_exc:
                        print(
                            f"⚠️  cancel attempt failed for {position.get('question')}: "
                            f"{type(cancel_exc).__name__}: {cancel_exc}",
                            flush=True,
                        )
                if cancelled:
                    print(
                        f"   {strategy_name} cancelled {len(cancelled)} resting order(s) on "
                        f"'{position.get('question')}'; will retry sell next tick",
                        flush=True,
                    )
                    out.append({
                        "market_id": position.get("market_id"),
                        "question": position.get("question"),
                        "action": "cancel_and_retry",
                        "cancelled_orders": cancelled,
                    })
                    continue
                # Cancel failed → force-close locally so we stop retrying.
                # Use current best_bid as the salvage price (likely tiny).
                salvage_price = max(float(candidate.best_bid or 0.0), 0.0)
                portfolio.record_live_exit(
                    position,
                    shares=float(plan["shares"]),
                    exit_price=salvage_price,
                    order_id=None,
                    order_response={"force_close": True, "reason": "stuck_balance"},
                    reason=f"{plan['reason']}_stuck",
                )
                portfolio.save(settings.state_path)
                if position.get("status") == "closed":
                    from .main import _append_trade_journal
                    _append_trade_journal(settings, position, f"{plan['reason']}_stuck")
                out.append({
                    "market_id": position.get("market_id"),
                    "question": position.get("question"),
                    "action": "force_close_stuck",
                    "reason": f"{plan['reason']}_stuck",
                    "exit_price": salvage_price,
                })
                print(
                    f"🗑️  {strategy_name} force-closed stuck position "
                    f"'{position.get('question')}' (CLOB balance < needed, cancel failed)",
                    flush=True,
                )
                continue
            # Auth/credential errors are transient: the position is still
            # alive on CLOB, we just couldn't sign the SELL. Don't write
            # it off — surface the error and let the next tick retry.
            exc_msg = str(exc).lower()
            if (
                "signature" in exc_msg
                or "unauthorized" in exc_msg
                or "invalid api" in exc_msg
                or "api key" in exc_msg
                or "401" in exc_msg
                or "403" in exc_msg
            ):
                print(
                    f"🔐  {strategy_name} SELL blocked by auth error on "
                    f"'{position.get('question')}': {exc} — position NOT "
                    f"written off, will retry. Run `uv run pmbot "
                    f"bootstrap-creds` to refresh credentials if this persists.",
                    flush=True,
                )
                continue

            if "below minimum" in exc_msg:
                print(
                    f"⚠️  {strategy_name} SELL below local minimum on "
                    f"'{position.get('question')}': {exc} — position NOT "
                    f"written off, will retry or sync from live account.",
                    flush=True,
                )
                continue

            # Winner floor (2026-06-10): the sell was refused because it
            # would price a resolved winner below 0.99. Hold — never write
            # off; the position retries next tick or settles at 1.00.
            if "winner_floor" in exc_msg:
                print(
                    f"🛡️  {strategy_name} held '{position.get('question')}' — "
                    f"winner floor refused a sub-0.99 sell; waiting for a real "
                    f"0.99 bid or on-chain settlement.",
                    flush=True,
                )
                continue

            # Auto-write-off: close locally when we can't get a SELL through.
            # Cases: past scheduled end_date (expired), resolved loser (≤0.05),
            # OR resolved winner (≥threshold) — markets that resolve BEFORE their
            # scheduled end_date (e.g. game finishes early) so expired=False but
            # the CLOB is closed. Without this the position lingers until end_date.
            mtc_now = _minutes_to_close(position)
            expired = mtc_now is not None and mtc_now <= 0
            resolved_loser = candidate.best_bid is not None and candidate.best_bid <= 0.05
            if expired or resolved_loser or position_resolved:
                writeoff_price = max(float(candidate.best_bid or 0.0), 0.0)
                # sync_closed=True prevents _sync_live_positions from re-opening
                # this position next tick (which would produce duplicate journal
                # entries and incorrect PnL accounting).
                position["sync_closed"] = True
                portfolio.record_live_exit(
                    position,
                    shares=float(plan["shares"]),
                    exit_price=writeoff_price,
                    order_id=None,
                    order_response={"writeoff": True, "reason": str(exc)},
                    reason=f"{plan['reason']}_writeoff",
                )
                portfolio.save(settings.state_path)
                if position.get("status") == "closed":
                    from .main import _append_trade_journal
                    _append_trade_journal(settings, position, f"{plan['reason']}_writeoff")
                out.append(
                    {
                        "market_id": position.get("market_id"),
                        "question": position.get("question"),
                        "action": "writeoff",
                        "reason": f"{plan['reason']}_writeoff",
                        "exit_price": writeoff_price,
                    }
                )
                print(
                    f"✏️  {strategy_name} writeoff on {position.get('question')}: "
                    f"price={writeoff_price:.3f} (SELL blocked: {exc})",
                    flush=True,
                )
                continue
            print(
                f"⚠️  {strategy_name} sell skipped on {position.get('question')}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            continue
        portfolio.save(settings.state_path)
        if position.get("status") == "closed":
            from .main import _append_trade_journal
            _append_trade_journal(settings, position, str(plan["reason"]))
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


def select_grinder(eligible: list[tuple[Candidate, float]], n: int) -> list[Candidate]:
    """Grinder: heavy favorites very close to resolution.

    Thesis: a market sitting at bid ≥ 0.88 with < 1h to close is pricing
    near-certainty. Pay the spread, take a small TP, and rotate. The edge
    isn't analytical — it's the implied-probability gap between bid and
    the binary outcome resolving in the buyer's favor. Tight SL caps the
    catastrophic "favorite flips" case.

    Candidate band (price, spread, hours) is already enforced upstream in
    `_build_eligible_candidates` via the TOML's race_* filters. This
    selector just ranks the survivors by confidence × time-to-resolution.
    """
    qualified: list[tuple[Candidate, float]] = []
    for candidate, _ in eligible:
        bid = candidate.best_bid or 0.0
        ask = candidate.best_ask or 1.0
        hours = candidate.hours_to_close or 99.0
        if bid <= 0.0 or ask <= 0.0:
            continue
        # Score = confidence per remaining hour. Closer to resolution and
        # closer to 1.0 → higher rank.
        score = bid / max(hours, 1.0 / 60.0)
        qualified.append((candidate, score))
    return _dedupe_top_n(qualified, n)


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


# ---------------------------------------------------------------------------
# Dry → live grinder mirror
#
# The live and dry grinder twins run identical filters but tick on different
# phases (live 30s, dry 600s), so a market that sits in the tight grinder band
# for only a brief window can be caught by one and missed by the other. When
# enabled (POLYMARKET_LIVE_MIRROR_DRY=1), the dry grinder writes each fresh BUY
# to a shared signal file and the live grinder takes it on its next tick — but
# ONLY after re-validating against the live scan's current quote. A market that
# has resolved, stopped accepting orders, drifted far out of band, or moved past
# resolution is silently skipped, so this can never resurrect a stale position.
# ---------------------------------------------------------------------------

MIRROR_ENV_FLAG = "POLYMARKET_LIVE_MIRROR_DRY"
MIRROR_SIGNAL_PATH = os.path.join("data", "signals", "grinder_mirror.jsonl")
MIRROR_STALENESS_SEC = 900  # accept dry signals up to 15 min old (dry ticks 600s)
MIRROR_PRICE_TOLERANCE = 0.03  # relax the price band slightly on the mirror path
MIRROR_MAX_ASK = 0.99  # never chase a market already priced at near-certainty
MIRROR_SPREAD_MULT = 2.0  # relaxed (but still bounded) spread cap


def _mirror_enabled() -> bool:
    return os.environ.get(MIRROR_ENV_FLAG, "0") == "1"


def _emit_mirror_signal(candidate: Candidate, strategy_name: str) -> None:
    """Dry grinder records a fresh BUY for the live bot to mirror."""
    try:
        os.makedirs(os.path.dirname(MIRROR_SIGNAL_PATH), exist_ok=True)
        record = {
            "ts": utc_now().isoformat(),
            "strategy": strategy_name,
            "market_id": candidate.market_id,
            "token_id": candidate.token_id,
            "outcome": candidate.outcome,
            "slug": candidate.slug,
            "question": candidate.question,
            "best_bid": candidate.best_bid,
            "best_ask": candidate.best_ask,
            "end_date": candidate.end_date.isoformat() if candidate.end_date else None,
        }
        with open(MIRROR_SIGNAL_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as exc:
        print(f"   mirror-emit failed: {type(exc).__name__}: {exc}", flush=True)


def _load_mirror_candidates(
    markets: list[dict[str, Any]],
    settings: Settings,
    eligible: list[tuple[Candidate, float]],
    portfolio: Portfolio,
) -> list[tuple[Candidate, float]]:
    """Live grinder: pull fresh dry-grinder BUY signals it would otherwise miss.

    Each signalled market is re-validated against the live scan's *current*
    quote (accepting orders, near-resolution, in a relaxed-but-bounded band).
    Anything that fails — resolved, dropped from the scan, drifted, or past
    resolution — is skipped, so a stale dry position is never resurrected.
    """
    if not os.path.exists(MIRROR_SIGNAL_PATH):
        return []
    now = utc_now()
    cutoff = now - timedelta(seconds=MIRROR_STALENESS_SEC)
    wanted: dict[str, dict[str, Any]] = {}
    try:
        with open(MIRROR_SIGNAL_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = parse_dt(rec.get("ts"))
                if ts is None or ts < cutoff:
                    continue
                token = str(rec.get("token_id") or "")
                if token:
                    wanted[token] = rec  # newest signal per token wins
    except Exception:
        return []
    if not wanted:
        return []

    # Skip tokens live's own scan already caught, or that we already hold.
    have = {c.token_id for c, _ in eligible if c.token_id}
    open_tokens = {
        str(p.get("token_id"))
        for p in portfolio.positions
        if p.get("status") == "open" and p.get("token_id")
    }
    markets_by_id = {str(m.get("id") or ""): m for m in markets}
    earliest = now + timedelta(minutes=1)
    horizon = now + timedelta(hours=settings.race_max_hours)

    extra: list[tuple[Candidate, float]] = []
    for token, rec in wanted.items():
        if token in have or token in open_tokens:
            continue
        market = markets_by_id.get(str(rec.get("market_id") or ""))
        if market is None:
            continue  # dropped from the live scan -> likely resolved
        if not bool(market.get("acceptingOrders")):
            continue
        end_date = parse_dt(market.get("endDate"))
        if end_date is None or end_date < earliest or end_date > horizon:
            continue
        token_ids = [str(t) for t in parse_json_list(market.get("clobTokenIds"))]
        if token not in token_ids:
            continue
        index = token_ids.index(token)
        outcomes = [str(o) for o in parse_json_list(market.get("outcomes"))]
        if len(outcomes) != 2:
            continue
        market_best_bid = as_float(market.get("bestBid"), default=None)
        market_best_ask = as_float(market.get("bestAsk"), default=None)
        tick_raw = market.get("orderPriceMinTickSize")
        tick_size = as_float(tick_raw, default=None) if tick_raw is not None else None
        if market_best_bid is None or market_best_ask is None or not tick_size or tick_size <= 0:
            continue
        best_bid, best_ask = _quote_for_outcome(index, 2, market_best_bid, market_best_ask)
        if best_bid is None or best_ask is None:
            continue
        # Relaxed-but-bounded band: honour "live takes what dry took" even if
        # the price drifted slightly, but never chase past MIRROR_MAX_ASK and
        # keep the spread bounded.
        if best_ask < settings.race_min_price - MIRROR_PRICE_TOLERANCE:
            continue
        if best_ask > min(settings.race_max_price + MIRROR_PRICE_TOLERANCE, MIRROR_MAX_ASK):
            continue
        spread = best_ask - best_bid
        if spread < 0 or spread > settings.race_max_spread * MIRROR_SPREAD_MULT:
            continue

        hours_to_close = max((end_date - now).total_seconds() / 3600.0, 0.0)
        prices = [as_float(p, -1.0) for p in parse_json_list(market.get("outcomePrices"))]
        price = prices[index] if 0 <= index < len(prices) and prices[index] > 0 else best_ask
        event_slug = _event_slug(market)
        slug = str(market.get("slug") or market.get("id") or "")
        candidate = Candidate(
            market_id=str(market.get("id") or ""),
            question=str(market.get("question") or ""),
            slug=slug,
            end_date=end_date,
            hours_to_close=hours_to_close,
            liquidity=as_float(market.get("liquidity") or market.get("liquidityNum")),
            volume=as_float(market.get("volume") or market.get("volumeNum")),
            outcome=outcomes[index],
            price=price,
            token_id=token,
            score=0.0,
            url=f"https://polymarket.com/event/{event_slug or slug}" if (event_slug or slug) else "https://polymarket.com",
            best_bid=best_bid,
            best_ask=best_ask,
            tick_size=tick_size,
            neg_risk=bool(market.get("negRisk")),
            accepts_orders=True,
            event_slug=event_slug,
        )
        extra.append((candidate, 0.0))
        print(
            f"   ↪ mirror: taking dry grinder signal {candidate.outcome} "
            f"ask={best_ask:.3f} h2c={hours_to_close:.2f} ({candidate.question[:50]})",
            flush=True,
        )
    return extra


def _run_btc_edge_pass(
    client,
    settings: Settings,
    portfolio: Portfolio,
    cash_floor: float,
) -> list[dict[str, Any]]:
    """Black-Scholes BTC/ETH threshold edge — model-based crypto lane.

    Runs as an independent pass inside the race tick (parallel to the arb
    pass). Only buys a threshold market when the modelled terminal
    probability beats the market price by ``btc_min_edge`` — i.e. positive
    expected value, not "buy any favorite". Sized at most ``btc_max_trade_usd``
    ($5 cap by default) and never below the cash floor. At most one trade
    per tick (the single highest-edge signal).
    """
    from .bitcoin import CoinbaseBtcClient, choose_btc_edge_trade
    from .main import load_btc_candidates

    try:
        candidates = load_btc_candidates(settings)
        model = CoinbaseBtcClient().model(settings)
    except Exception as exc:
        print(f"   btc edge skipped: {type(exc).__name__}: {exc}", flush=True)
        return []

    eligible = [
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
    signal = choose_btc_edge_trade(eligible, settings, model)
    if signal is None:
        return []

    cash_above_floor = max(0.0, portfolio.cash - cash_floor)
    if cash_above_floor < max(1.0, settings.btc_min_trade_usd):
        return []
    stake = min(settings.btc_max_trade_usd, cash_above_floor)

    signal_payload = signal.to_dict()
    signal_payload["tag"] = "btc_edge"
    signal_payload["selection_reason"] = (
        f"btc_edge {signal.side} strike={signal.strike} "
        f"fair={signal.fair_probability:.3f} edge={signal.edge:.3f} "
        f"ask={signal.candidate.best_ask}"
    )
    print(
        f"   ₿ btc edge signal: {signal.side} fair={signal.fair_probability:.3f} "
        f"edge={signal.edge:.3f} ask={signal.candidate.best_ask} stake=${stake:.2f}",
        flush=True,
    )
    try:
        result = execute_live_trade(
            client,
            settings,
            signal.candidate,
            portfolio,
            min_trade_usd=settings.btc_min_trade_usd,
            max_trade_usd=stake,
            strategy="btc_edge",
            signal=signal_payload,
        )
        portfolio.save(settings.state_path)
        return [
            {
                "strategy": "btc_edge",
                "order": result.order,
                "response": result.response,
                "signal": signal_payload,
            }
        ]
    except Exception as exc:
        print(f"   btc edge trade failed: {type(exc).__name__}: {exc}", flush=True)
        return []


# Smallest top-up worth sending: below this the 5-share exchange minimum
# and the book fetch aren't worth the order.
_TOPUP_MIN_USD = 5.0


def _position_cap_usd(settings: Settings, equity: float) -> float:
    """HARD maximum total cost basis allowed on one position.

    ``equity × race_stake_pct`` (the 10% per-bet cap) — the absolute ceiling
    a position can ever reach, only fully filled via the dip double-down.
    Min'd with the configured ceilings when set.

    v4 (user 2026-06-21): when ``race_fixed_stake_usd`` > 0, the cap IS the
    fixed dollar stake — a position can never exceed one $5 bet (no averaging
    or double-down headroom).
    """
    fixed = float(getattr(settings, "race_fixed_stake_usd", 0.0) or 0.0)
    if fixed > 0:
        return fixed
    cap = equity * settings.race_stake_pct
    if settings.smart_max_position_ceiling_pct > 0:
        cap = min(cap, equity * settings.smart_max_position_ceiling_pct)
    if settings.smart_max_position_ceiling_usd > 0:
        cap = min(cap, settings.smart_max_position_ceiling_usd)
    return max(0.0, cap)


def _entry_cap_usd(settings: Settings, equity: float) -> float:
    """Cost-basis ceiling for a FRESH entry and any passive top-up.

    When ``race_initial_stake_pct`` is set (0 < it < race_stake_pct), entries
    and passive top-ups target ``equity × initial_stake_pct`` (e.g. 5%),
    reserving the headroom up to the full ``_position_cap_usd`` (10%) for the
    dip double-down. Otherwise this equals the hard cap (old behavior).

    v4 (user 2026-06-21): with ``race_fixed_stake_usd`` > 0 the entry cap IS
    the fixed stake (== the position cap, so there is no double-down headroom).
    """
    fixed = float(getattr(settings, "race_fixed_stake_usd", 0.0) or 0.0)
    if fixed > 0:
        return fixed
    initial = float(getattr(settings, "race_initial_stake_pct", 0.0) or 0.0)
    hard = _position_cap_usd(settings, equity)
    if initial <= 0.0 or initial >= settings.race_stake_pct:
        return hard
    return min(hard, max(0.0, equity * initial))


def _dynamic_stake_target(
    settings: Settings,
    equity: float,
    cash_above_floor: float,
    n_opportunities: int,
    hours_to_close: float | None,
) -> float:
    """Per-bet sizing target (user 2026-06-10): spread the available cash
    across the actionable opportunities, hard-capped per bet.

    - Hard cap: ``equity × race_stake_pct`` (20%) — never more on one bet.
    - Busy window (N actionable markets): each bet targets cash/N so every
      opportunity can be funded, instead of the first picks taking the
      full cap and starving the rest.
    - Slow market (1–2 opportunities): each bet gets the full cap.
    - The near-resolution boost (1.5× under 30 min, 1.25× under 1 h)
      scales the spread share but can never pierce the cap.
    """
    # v4 fixed-dollar sizing (user 2026-06-21): every bet is EXACTLY the
    # fixed stake, capped only by the cash actually available — no spread
    # across opportunities, no near-resolution boost, no scaling.
    fixed = float(getattr(settings, "race_fixed_stake_usd", 0.0) or 0.0)
    if fixed > 0:
        return max(0.0, min(fixed, cash_above_floor))
    # Entries (and passive top-ups) target the INITIAL cap; the dip
    # double-down later fills the reserved headroom up to the hard cap.
    per_bet_cap = _entry_cap_usd(settings, equity)
    spread_share = cash_above_floor / max(1, n_opportunities)
    h2c = hours_to_close if hours_to_close is not None else 99.0
    if h2c < 0.5:
        size_mult = 1.5
    elif h2c < 1.0:
        size_mult = 1.25
    else:
        size_mult = 1.0
    target = max(min(per_bet_cap, spread_share * size_mult), 1.0)
    # Configured ceilings still bound everything (0 = disabled).
    if settings.smart_max_position_ceiling_pct > 0:
        target = min(target, equity * settings.smart_max_position_ceiling_pct)
    if settings.smart_max_position_ceiling_usd > 0:
        target = min(target, settings.smart_max_position_ceiling_usd)
    return target


def _entry_window_ladder(settings: Settings, now: Any = None) -> list[float]:
    """Hour windows to try, narrowest first.

    User rules 2026-06-11/12: prefer bets within 4h of resolution; if nothing
    is actionable, widen in 2h steps to 12h (4 → 6 → 8 → 10 → 12), then jump
    straight to the cap (24h). If even the cap is empty and
    ``race_daily_expiry_fallback`` is on, one last rung extends to the end of
    TOMORROW (UTC) so daily markets ("Will X be Y on <date>?", stamped at
    midnight UTC like the Trump-approval one) stay reachable. With
    ``race_max_hours_cap`` ≤ base (or 0) the ladder is just [base].
    """
    base = max(float(settings.race_max_hours), 0.5)
    cap = float(settings.race_max_hours_cap or 0.0)
    ladder = [base]
    hours = base
    step_limit = min(cap, 12.0)
    while step_limit > hours + 1e-9:
        hours = min(hours + 2.0, step_limit)
        ladder.append(hours)
    if cap > ladder[-1] + 1e-9:
        ladder.append(cap)
    if settings.race_daily_expiry_fallback:
        if now is None:
            now = utc_now()
        end_of_tomorrow = (now + timedelta(days=2)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        daily_rung = (end_of_tomorrow - now).total_seconds() / 3600.0
        if daily_rung > ladder[-1] + 1e-9:
            ladder.append(round(daily_rung, 2))
    return ladder


# One bet per game (2026-06-11, was 2): a second position on the same event
# is never opened — _dedup_same_game collapses same-game picks before
# selection, this cap is the in-loop backstop.
EVENT_EXPOSURE_CAP = 1


# ── Game identity (2026-06-11) ───────────────────────────────────────────
# Polymarket splits ONE game across SEVERAL events: the Mexico–South Africa
# moneyline lived in `fifwc-mex-rsa-2026-06-11`, the O/U 4.5 in
# `…-more-markets`, and the first-to-score special in `…-first-to-score` —
# so the event-slug dedup let the bot stack $958 on a single game (three
# positions, 2026-06-11 12:00 UTC). A game is identified by BOTH:
#   - the event slug truncated at its date (`…-2026-06-11-more-markets` →
#     `…-2026-06-11`), unifying the per-game satellite events;
#   - the team names parsed from the question ("A vs. B: …" → both teams,
#     "Will A win on YYYY-MM-DD?" → A), catching markets whose slugs share
#     no prefix. Within the ≤12h entry window a team plays one game at most.
_GAME_SLUG_BASE_RE = re.compile(r"^(.*?\d{4}-\d{2}-\d{2})")
_VS_QUESTION_RE = re.compile(r"^\s*(.+?)\s+vs\.?\s+(.+?)\s*(?::|$)", re.IGNORECASE)
_WIN_ON_DATE_RE = re.compile(r"^\s*will\s+(.+?)\s+win\s+on\s+\d{4}-\d{2}-\d{2}", re.IGNORECASE)


def _game_keys(question: str, event_slug: str) -> set[str]:
    """Identity keys for the game behind a market (empty for non-games).

    The exact event slug is always included, so multi-market non-game events
    (elections, indices) still collapse to one bet via the same mechanism.
    """
    keys: set[str] = set()
    slug = str(event_slug or "").lower()
    if slug:
        keys.add(f"ev:{slug}")
        m = _GAME_SLUG_BASE_RE.match(slug)
        if m:
            keys.add(f"slug:{m.group(1)}")
    q = str(question or "").strip()
    m = _VS_QUESTION_RE.match(q)
    if m:
        keys.add(f"team:{m.group(1).strip().lower()}")
        keys.add(f"team:{m.group(2).strip().lower()}")
    else:
        m = _WIN_ON_DATE_RE.match(q)
        if m:
            keys.add(f"team:{m.group(1).strip().lower()}")
    return keys


def _candidate_game_keys(candidate: Candidate) -> set[str]:
    return _game_keys(candidate.question or "", candidate.event_slug or "")


def _open_game_keys(portfolio: Portfolio) -> set[str]:
    """Game keys of every OPEN position — blocks a second bet on a game the
    book already holds, across ticks and across satellite event slugs."""
    keys: set[str] = set()
    for position in portfolio.positions:
        if position.get("status") != "open":
            continue
        keys |= _game_keys(
            str(position.get("question") or ""), str(position.get("event_slug") or "")
        )
    return keys


def _dedup_same_game(
    candidates: list[tuple[Candidate, float]],
) -> list[tuple[Candidate, float]]:
    """One bet per game: keep a single candidate per game (NOT per event —
    one game spans several Polymarket events, see `_game_keys`).

    User rule 2026-06-11/14: never take two bets on the same game; keep the
    single best (highest-bid, i.e. most-resolved) candidate per game. The
    soccer under-4.5 priority was dropped 2026-06-14 — just take the best
    bet for each game. Candidates with no keys pass through.
    """
    chosen: list[tuple[Candidate, float]] = []
    keys_by_index: dict[int, set[str]] = {}
    claimed: dict[str, int] = {}
    passthrough: list[tuple[int, tuple[Candidate, float]]] = []
    for position_in_input, entry in enumerate(candidates):
        candidate, _ = entry
        keys = _candidate_game_keys(candidate)
        if not keys:
            passthrough.append((position_in_input, entry))
            continue
        conflict = next((claimed[k] for k in keys if k in claimed), None)
        if conflict is None:
            index = len(chosen)
            chosen.append(entry)
            keys_by_index[index] = set(keys)
            for k in keys:
                claimed[k] = index
            continue
        held_candidate, _ = chosen[conflict]
        if (candidate.best_bid or 0.0) > (held_candidate.best_bid or 0.0):
            chosen[conflict] = entry
        # Either way the loser's keys now point at the winner, so a third
        # market of the same game still conflicts.
        keys_by_index[conflict] |= keys
        for k in keys_by_index[conflict]:
            claimed[k] = conflict
    out = list(chosen)
    for _, entry in passthrough:
        out.append(entry)
    return out


def _actionable_candidates(
    eligible: list[tuple[Candidate, float]], portfolio: Portfolio, settings: Settings
) -> list[tuple[Candidate, float]]:
    """Drop candidates that can never execute this tick: order pending,
    blocked by the one-position-per-event guard, or a held token with no
    top-up room left under the per-position cap.

    Must run BEFORE the selector — it returns only the top
    race_max_orders_per_tick candidates, so a slot spent on an already-held
    market evicts the next-best actionable one from the tick entirely.
    Seen live on 2026-06-10: four Spurs/Knicks O/U lines (soonest to close,
    highest score) filled every pick slot tick after tick while the
    5th-ranked PPI market was never attempted; other bots without the
    Spurs position took it immediately.

    A held token whose cost basis still sits below the cap stays actionable
    (top-up lane, 2026-06-10): when a buy was depth-capped under its sizing
    target, the bot may complete the position once the book refills — it
    re-passes the same entry filters each time, so a top-up is just a new
    qualifying bet on the same outcome.
    """
    equity = float(portfolio.summary().get("equity", portfolio.cash))
    # Passive top-up uses the ENTRY cap (initial %); the headroom up to the
    # hard cap is reserved for the dip double-down only.
    cap = _entry_cap_usd(settings, equity)
    open_keys = _open_game_keys(portfolio)
    out: list[tuple[Candidate, float]] = []
    for c, m in eligible:
        if portfolio.has_pending_token(c.token_id):
            continue
        open_pos = portfolio.open_position_for_token(c.token_id)
        if open_pos is not None:
            if cap - float(open_pos.get("stake") or 0.0) < _TOPUP_MIN_USD:
                continue
        elif portfolio.has_open_event_position(c):
            continue
        elif _candidate_game_keys(c) & open_keys:
            # One bet per GAME across ticks (2026-06-11): an open position
            # on any market of this game blocks every other market of the
            # same game, even when Polymarket files them under different
            # event slugs (moneyline vs -more-markets vs -first-to-score).
            continue
        out.append((c, m))
    # One bet per game within the tick: collapse same-game candidates to a
    # single pick, preferring soccer's under-4.5 line over the rest.
    return _dedup_same_game(out)


def _execute_double_downs(
    client: Any,
    settings: Settings,
    portfolio: Portfolio,
    pool: list[Candidate],
    strategy_name: str,
) -> list[dict[str, Any]]:
    """Double down on ANY dipped open position (user 2026-06-14).

    When an OPEN position's LIVE ask has dipped a bit below its
    (volume-weighted) entry price — e.g. 0.96 → 0.89 — buy more of the same
    outcome, averaging the cost basis down on a favorite that just got
    cheaper. Applies to every grinder market, not only soccer Under-4.5.
    Strictly bounded:
      - once per position (``doubled_down`` flag);
      - the ask dipped at least ``min_dip`` below entry (a real dip);
      - the ask is still "alive" — ≥ ``min_price`` (0.60), the proxy for
        "the bet is still going well" (user 2026-06-14, Sweden-Tunisia
        Under: double down while the cote is still above 0.6). Below the
        floor the bet has turned and is never topped up;
      - the add never pushes total cost past the per-position cap (the 10%
        per-bet equity cap), so the user's hard sizing ceiling holds.
    """
    if not getattr(settings, "race_double_down_enabled", False):
        return []
    by_token = {c.token_id: c for c in pool if c.token_id}
    equity = float(portfolio.summary().get("equity", portfolio.cash))
    cap = _position_cap_usd(settings, equity)
    cash_floor = equity * settings.race_cash_floor_pct
    out: list[dict[str, Any]] = []
    for position in list(portfolio.positions):
        if position.get("status") != "open" or not position.get("live"):
            continue
        if str(position.get("strategy") or "") not in (strategy_name, "live_sync"):
            continue
        if position.get("doubled_down"):
            continue
        # Never add to a now-excluded category (e.g. an O/U 4.5 hold after the
        # 2026-06-14 ban): adding to a banned lane defeats the ban's purpose.
        if is_excluded_market({
            "question": position.get("question"),
            "slug": position.get("event_slug") or position.get("slug"),
        }):
            continue
        token_id = position.get("token_id")
        candidate = by_token.get(token_id)
        if candidate is None or candidate.best_ask is None or not candidate.token_id:
            continue
        ask = float(candidate.best_ask)
        entry = float(position.get("entry_price") or 0.0)
        if entry <= 0:
            continue
        dip = entry - ask
        # Dipped at all, and still "alive" — ask ≥ min_price (0.60). The
        # 0.60 floor is the proxy for "the bet is still going well" (user
        # 2026-06-14, Sweden-Tunisia Under: double down while the cote is
        # still above 0.6); below it the bet has turned and we never add.
        if dip < settings.race_double_down_min_dip:
            continue
        # Secondary safety cap (0 = unbounded). The min_price floor below is the
        # real "still a favorite" gate; both 0.83->0.78 and 0.85->0.70 pass.
        max_dip = getattr(settings, "race_double_down_max_dip", 0.0)
        if max_dip > 0 and dip > max_dip:
            continue
        if ask < settings.race_double_down_min_price:
            continue
        room = cap - float(position.get("stake") or 0.0)
        add = min(room, max(0.0, portfolio.cash - cash_floor))
        if add < _TOPUP_MIN_USD:
            continue
        try:
            result = execute_live_trade(
                client, settings, candidate, portfolio,
                min_trade_usd=1.0, max_trade_usd=add,
                strategy=strategy_name,
                signal={"question": candidate.question, "tag": f"{strategy_name}_double_down"},
            )
            position["doubled_down"] = True
            portfolio.save(settings.state_path)
            out.append({
                "market_id": position.get("market_id"),
                "question": position.get("question"),
                "action": "double_down",
                "reason": "dip_double_down",
                "dip": round(dip, 4),
                "add_usd": round(add, 2),
                "order": result.order,
                "response": result.response,
            })
            print(
                f"⏬ {strategy_name} double-down on '{position.get('question')}' "
                f"— ask {ask:.3f} dipped {dip*100:.1f}¢ below entry {entry:.3f}, "
                f"added ${add:.2f} toward the cap.",
                flush=True,
            )
        except Exception as exc:
            print(
                f"⚠️  {strategy_name} double-down skipped on "
                f"'{position.get('question')}': {type(exc).__name__}: {exc}",
                flush=True,
            )
    return out


def _run_race_tick(
    settings: Settings,
    strategy_name: str,
    select_fn,
) -> dict[str, Any]:
    print(f"▶  {strategy_name} tick start", flush=True)
    # Dynamic entry window (2026-06-11): scan out to the ladder cap so held
    # positions beyond the base window still get marked/managed, then pick
    # entries from the narrowest window that has actionable candidates.
    ladder = _entry_window_ladder(settings)
    markets = _load_short_expiry_markets(settings, max_hours=ladder[-1])
    _step(settings, f"   markets: {len(markets)} raw (≤{ladder[-1]:.0f}h)")
    # v4 data-driven category auto-disable (user 2026-06-21): a category with
    # ≥ min_samples realized trades and ROI < disable_roi is dropped from entry
    # selection — the governance that replaces manual bans under unban_all.
    # Fail-open (empty set) so the ledger read can never break the loop.
    disabled_cats: set[str] = set()
    min_samples = int(getattr(settings, "race_category_min_samples", 0) or 0)
    if min_samples > 0:
        try:
            from .main import _read_realized_records

            records = _read_realized_records(settings.trade_journal_path)
            disabled_cats = disabled_categories(
                records,
                min_samples=min_samples,
                roi_threshold=float(getattr(settings, "race_category_disable_roi", -0.05)),
            )
            if disabled_cats:
                _step(settings, f"   category auto-disable: {sorted(disabled_cats)}")
        except Exception:
            disabled_cats = set()
    # v4 forecasting EV/quality gates (opt-in): build the calibration context
    # once from the realized ledger when a gate is active. Fail-open.
    forecast_ctx = None
    gates_active = (
        float(getattr(settings, "race_min_edge", 0.0) or 0.0) > 0
        or float(getattr(settings, "race_min_quality_score", 0.0) or 0.0) > 0
    )
    if gates_active:
        try:
            from .main import _read_realized_records

            recs = _read_realized_records(settings.trade_journal_path)
            forecast_ctx = build_context(
                recs,
                prior_default=float(getattr(settings, "race_forecast_prior", 0.95)),
                pseudo_count=float(getattr(settings, "race_forecast_pseudo_count", 20.0)),
            )
        except Exception:
            forecast_ctx = None
    eligible = _build_eligible_candidates(
        markets,
        settings,
        max_hours=ladder[-1],
        disabled_categories=disabled_cats,
        forecast_ctx=forecast_ctx,
    )
    _step(settings, f"   eligible: {len(eligible)}")
    obs = _log_forward_observations(markets, settings)
    if obs:
        _step(settings, f"   forward-log: +{obs} new observations")

    portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)
    if not settings.dry_run and settings.sync_live_positions:
        from .main import _sync_live_positions

        _sync_live_positions(settings, portfolio)

    pool = ensure_open_positions_in_pool(settings, portfolio, [c for c, _ in eligible])
    portfolio.mark_to_market(pool)

    client = build_client(settings)

    # Expire stale pending orders (2026-06-15): a delayed/accepted-but-unfilled
    # BUY is held as pending so the dedup guard blocks re-buying the same
    # market every tick. If it never settles (killed in-play), free the token
    # after the TTL so the lane isn't blocked forever.
    if not settings.dry_run:
        try:
            from .main import _cancel_stale_pending_orders
            _cancel_stale_pending_orders(client, settings, portfolio)
        except Exception as exc:
            print(f"   stale-pending cleanup failed: {type(exc).__name__}: {exc}")

    exits = _execute_race_exits(client, settings, portfolio, pool, strategy_name)

    if not settings.dry_run:
        try:
            portfolio.cash = round(client.live_available_balance(), 2)
        except Exception as exc:
            print(f"   live cash refresh failed: {type(exc).__name__}: {exc}")

    # Under-4.5 double-down (user 2026-06-14): add to a dipped Under-4.5
    # position before placing new entries, bounded by the per-position cap.
    double_downs = _execute_double_downs(client, settings, portfolio, pool, strategy_name)
    if double_downs:
        _step(settings, f"   double-downs: {len(double_downs)}")
        if not settings.dry_run:
            try:
                portfolio.cash = round(client.live_available_balance(), 2)
            except Exception:
                pass

    # Daily drawdown gate — block new entries when realized PnL today
    # is ≤ -X% of starting equity. Existing positions still run exits.
    if settings.race_daily_drawdown_pct > 0:
        from .edge_strategy import _daily_realized_pnl
        starting_equity = max(settings.paper_balance_usd, settings.assumed_live_balance_usd, 1.0)
        realized_today = _daily_realized_pnl(settings.trade_journal_path)
        dd_limit = -starting_equity * settings.race_daily_drawdown_pct
        if realized_today <= dd_limit:
            print(
                f"🛑 {strategy_name}: daily drawdown limit hit "
                f"(${realized_today:+.2f} ≤ ${dd_limit:+.2f}) — entries paused",
                flush=True,
            )
            portfolio.save(settings.state_path)
            return {
                "trade": None,
                "strategy": strategy_name,
                "trades": [],
                "orders_placed": 0,
                "exits": exits,
                "double_downs": double_downs,
                "rejected_signals": [],
                "scan_counts": {"raw_markets": len(markets), "eligible": len(eligible), "picks": 0},
                "summary": portfolio.summary(),
                "status": "daily_drawdown_halt",
                "realized_today_usd": realized_today,
                "drawdown_limit_usd": dd_limit,
            }

    # Dry → live grinder mirror: live picks up fresh dry-grinder signals it
    # missed due to tick-phase, re-validated against the current live quote.
    if _mirror_enabled() and not settings.dry_run and strategy_name == "grinder":
        mirror_extra = _load_mirror_candidates(markets, settings, eligible, portfolio)
        if mirror_extra:
            eligible = eligible + mirror_extra
            _step(settings, f"   eligible (+{len(mirror_extra)} mirror): {len(eligible)}")

    # Walk the window ladder: take the narrowest window with actionable
    # candidates (4h preferred; widen 6 → 8 → 10 → 12 only when empty).
    actionable: list[tuple[Candidate, float]] = []
    window_hours = ladder[0]
    for window_hours in ladder:
        subset = [
            (c, m) for c, m in eligible if (c.hours_to_close or 0.0) <= window_hours
        ]
        actionable = _actionable_candidates(subset, portfolio, settings)
        if actionable:
            break
    if len(ladder) > 1:
        _step(
            settings,
            f"   entry window: ≤{window_hours:.0f}h → {len(actionable)} actionable",
        )
    if len(actionable) < len(eligible):
        _step(
            settings,
            f"   actionable: {len(actionable)}/{len(eligible)} (already-held/duplicate-event markets excluded from pick slots)",
        )

    # Opportunity count drives per-bet sizing: many actionable markets →
    # spread the cash; few → full per-bet cap (see _dynamic_stake_target).
    n_opportunities = max(1, len(actionable))

    picks = select_fn(actionable)

    # Noise fallback: if the selector returned nothing AND there are no
    # open positions, fire a random eligible candidate so the strategy
    # always trades. User explicitly wants action over discrimination.
    fallback_used = False
    if (
        not picks
        and settings.race_noise_fallback_enabled
        and eligible
    ):
        open_count = sum(1 for p in portfolio.positions if p.get("status") == "open" and p.get("live"))
        if open_count == 0 and actionable:
            picks = random.sample([c for c, _ in actionable], min(1, len(actionable)))
            fallback_used = True

    open_assets = _open_asset_keys(portfolio)
    executed: list[dict[str, Any]] = []
    executed_game_keys: set[str] = set()
    rejected: list[dict[str, Any]] = []
    cash_floor = portfolio.summary().get("equity", 0) * settings.race_cash_floor_pct

    # Event-exposure cap: how many open positions share the same event
    # slug already. Block any new pick that would push past the cap.
    event_exposure: dict[str, int] = {}
    for pos in portfolio.positions:
        if pos.get("status") == "open":
            ev = str(pos.get("event_slug") or "")
            if ev:
                event_exposure[ev] = event_exposure.get(ev, 0) + 1

    for candidate in picks:
        if len(executed) >= settings.race_max_orders_per_tick:
            break
        if not candidate.token_id:
            continue
        if portfolio.has_pending_token(candidate.token_id):
            rejected.append({"question": candidate.question, "reason": "pending_order"})
            continue
        # Top-up lane (2026-06-10): a token already held may be bought again
        # to complete a depth-capped entry, but only while the position's
        # total cost basis sits below the per-position cap
        # (equity × race_stake_pct). The cap is what bounds the old
        # "$45 → $4 in 22 ticks" averaging spiral that a blanket token
        # dedup used to prevent — without it (race_stake_pct ≤ 0) top-ups
        # stay disabled entirely.
        topup_pos = portfolio.open_position_for_token(candidate.token_id)
        topup_room = 0.0
        if topup_pos is not None:
            equity_now = float(portfolio.summary().get("equity", portfolio.cash))
            # Passive top-up fills only to the ENTRY cap; the dip double-down
            # owns the headroom up to the hard per-position cap.
            cap = _entry_cap_usd(settings, equity_now)
            topup_room = cap - float(topup_pos.get("stake") or 0.0)
            if cap <= 0 or topup_room < _TOPUP_MIN_USD:
                rejected.append({"question": candidate.question, "reason": "topup_cap_reached"})
                continue
        ev_slug = str(candidate.event_slug or "")
        if topup_pos is None and ev_slug and event_exposure.get(ev_slug, 0) >= EVENT_EXPOSURE_CAP:
            rejected.append({"question": candidate.question, "reason": f"event_exposure_cap:{ev_slug}"})
            continue
        # In-loop one-bet-per-game backstop (2026-06-11): a pick executed
        # earlier THIS tick claims its game keys; same-game picks bounce
        # even if they slipped past the upstream dedup.
        cand_keys = _candidate_game_keys(candidate)
        if topup_pos is None and cand_keys & executed_game_keys:
            rejected.append({"question": candidate.question, "reason": "same_game_already_bet"})
            continue
        asset_key = _asset_key(candidate.question, candidate.event_slug or "", candidate.slug or "")
        cash_above_floor = max(0.0, portfolio.cash - cash_floor)
        if cash_above_floor < 1.0:
            break
        equity = float(portfolio.summary().get("equity", portfolio.cash))
        target = _dynamic_stake_target(
            settings, equity, cash_above_floor, n_opportunities, candidate.hours_to_close
        )
        stake = min(target, cash_above_floor)
        if topup_pos is not None:
            stake = min(stake, topup_room)
        # Enforce Polymarket's 5-share minimum. At small bankrolls the
        # race_stake_pct alone may produce fewer than 5 shares; bump the
        # stake to exactly what's needed and skip if cash can't cover it.
        ask_price = float(candidate.best_ask or candidate.price or 0.0)
        if ask_price > 0 and settings.min_order_shares > 0:
            min_usd_for_shares = settings.min_order_shares * ask_price
            if stake < min_usd_for_shares:
                if cash_above_floor >= min_usd_for_shares:
                    stake = min_usd_for_shares
                else:
                    rejected.append({
                        "candidate": candidate.to_dict(),
                        "reason": f"stake ${stake:.2f} < min {settings.min_order_shares} shares × ${ask_price} = ${min_usd_for_shares:.2f}, insufficient cash",
                    })
                    continue
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
            executed_game_keys |= cand_keys
            if asset_key:
                open_assets.add(asset_key)
            if ev_slug and topup_pos is None:
                # A top-up grows an existing position record — counting it
                # again would inflate the per-event exposure tally.
                event_exposure[ev_slug] = event_exposure.get(ev_slug, 0) + 1
            portfolio.save(settings.state_path)
            # Dry grinder publishes its fresh BUY for the live bot to mirror.
            if _mirror_enabled() and settings.dry_run and strategy_name == "grinder":
                _emit_mirror_signal(candidate, strategy_name)
        except Exception as exc:
            rejected.append({"question": candidate.question, "reason": f"{type(exc).__name__}: {exc}"})

    # ── Binary arbitrage pass ────────────────────────────────────────────────
    # After normal grinder entries, scan all markets for YES+NO < threshold.
    # This is a second independent pass; it uses remaining cash.
    arb_results: list[dict[str, Any]] = []
    if settings.race_arb_threshold > 0:
        arb_pairs = _find_arb_pairs(markets, settings)
        # Filter out markets we already have a position in
        arb_pairs = [
            (m, yt, nt, ya, na)
            for m, yt, nt, ya, na in arb_pairs
            if not portfolio.has_open_token(yt) and not portfolio.has_open_token(nt)
        ]
        if arb_pairs:
            arb_results = _execute_arb_entries(client, settings, portfolio, arb_pairs, strategy_name)

    # ── BTC/ETH Black-Scholes edge pass ──────────────────────────────────────
    # Independent model-based crypto lane (parallel to the arb pass). Fills the
    # dead hours when no sports favorite sits in the grinder band. Only fires on
    # positive-EV signals (model prob beats market by btc_min_edge), $5 cap.
    btc_trades: list[dict[str, Any]] = []
    if settings.btc_edge_integrated:
        btc_cash_floor = float(portfolio.summary().get("equity", portfolio.cash)) * settings.race_cash_floor_pct
        btc_trades = _run_btc_edge_pass(client, settings, portfolio, btc_cash_floor)
        executed.extend(btc_trades)

    portfolio.save(settings.state_path)
    return {
        "trade": executed[-1] if executed else None,
        "strategy": strategy_name,
        "trades": executed,
        "orders_placed": len(executed),
        "exits": exits,
        "double_downs": double_downs,
        "rejected_signals": rejected,
        "arb_trades": arb_results,
        "btc_trades": btc_trades,
        "scan_counts": {"raw_markets": len(markets), "eligible": len(eligible), "picks": len(picks), "fallback_used": fallback_used},
        "summary": portfolio.summary(),
    }


# ---------------------------------------------------------------------------
# Binary intra-market arbitrage
# ---------------------------------------------------------------------------

def _find_arb_pairs(
    markets: list[dict[str, Any]],
    settings: Settings,
) -> list[tuple[dict[str, Any], str, str, float, float]]:
    """Scan for markets where YES_ask + NO_ask < race_arb_threshold.

    Queries the CLOB independently for both token ask prices. When their
    combined cost is below the threshold, buying both guarantees profit
    regardless of resolution ($1 payout on combined cost < $1).

    Returns list of (market, yes_token, no_token, yes_ask, no_ask).
    """
    if settings.race_arb_threshold <= 0:
        return []
    now = utc_now()
    horizon = now + timedelta(hours=settings.race_max_hours)
    eligible: list[tuple[dict[str, Any], str, str]] = []
    for market in markets:
        if not bool(market.get("acceptingOrders")):
            continue
        if is_excluded_market(market):
            continue
        end_date = parse_dt(market.get("endDate"))
        if end_date is None or end_date < now or end_date > horizon:
            continue
        token_ids = [str(t) for t in parse_json_list(market.get("clobTokenIds"))]
        if len(token_ids) != 2:
            continue
        eligible.append((market, token_ids[0], token_ids[1]))
    if not eligible:
        return []
    all_tokens = list({t for _, y, n in eligible for t in (y, n)})
    _, bid_ask = _fetch_clob_quotes(settings, all_tokens)
    arb_pairs = []
    for market, yes_token, no_token in eligible:
        yes_bid_ask = bid_ask.get(yes_token, (None, None))
        no_bid_ask = bid_ask.get(no_token, (None, None))
        # Ask price = what we pay to buy = bid_ask[1] (the "ask" side)
        # BUY side [0] = what we pay to buy (ask). SELL side [1] = what we receive.
        yes_ask = yes_bid_ask[0] if yes_bid_ask[0] is not None else None
        no_ask = no_bid_ask[0] if no_bid_ask[0] is not None else None
        if yes_ask is None or no_ask is None or yes_ask <= 0 or no_ask <= 0:
            continue
        if yes_ask + no_ask < settings.race_arb_threshold:
            arb_pairs.append((market, yes_token, no_token, yes_ask, no_ask))
    return arb_pairs


def _execute_arb_entries(
    client: Any,
    settings: Settings,
    portfolio: Portfolio,
    arb_pairs: list[tuple[dict[str, Any], str, str, float, float]],
    strategy_name: str,
) -> list[dict[str, Any]]:
    """Execute binary arb: buy both YES and NO legs for each eligible pair."""
    from .trading import execute_live_sell  # noqa: F401 (side-effect: validates import)

    results: list[dict[str, Any]] = []
    for market, yes_token, no_token, yes_ask, no_ask in arb_pairs:
        market_id = str(market.get("id") or "")
        question = str(market.get("question") or "")
        end_date = parse_dt(market.get("endDate"))
        hours_to_close = max((end_date - utc_now()).total_seconds() / 3600, 0.0) if end_date else 0.0
        tick_raw = market.get("orderPriceMinTickSize")
        tick_size = as_float(tick_raw, default=0.01) if tick_raw else 0.01
        slug = str(market.get("slug") or market_id)
        event_slug = _event_slug(market)
        url = f"https://polymarket.com/event/{event_slug or slug}"
        outcomes = [str(x) for x in parse_json_list(market.get("outcomes"))]
        yes_outcome = outcomes[0] if outcomes else "Yes"
        no_outcome = outcomes[1] if len(outcomes) > 1 else "No"
        neg_risk = bool(market.get("negRisk"))

        # Proportional sizing for true arb: face value P is the guaranteed
        # payout whichever leg wins. YES stake = P * yes_ask, NO stake =
        # P * no_ask. Profit = P * (1 - yes_ask - no_ask) regardless of
        # outcome. Cap: largest leg ≤ race_arb_max_stake_usd AND total
        # ≤ 40% of cash.
        max_leg = max(yes_ask, no_ask)
        max_face = min(
            settings.race_arb_max_stake_usd / max_leg,
            (portfolio.cash * 0.40) / (yes_ask + no_ask),
        )
        yes_stake = round(max_face * yes_ask, 4)
        no_stake = round(max_face * no_ask, 4)
        if yes_stake < 0.50 or no_stake < 0.05 or portfolio.cash < yes_stake + no_stake:
            continue

        total_cost = yes_ask + no_ask
        profit_pct = (1.0 - total_cost) / total_cost
        print(
            f"🎯 ARB: {question[:55]} | YES={yes_ask:.3f}+NO={no_ask:.3f}={total_cost:.3f}"
            f" → +{profit_pct:.1%} guaranteed",
            flush=True,
        )

        def _make_candidate(token_id: str, outcome: str, ask: float) -> Candidate:
            return Candidate(
                market_id=market_id,
                question=question,
                slug=slug,
                end_date=end_date,
                hours_to_close=hours_to_close,
                liquidity=as_float(market.get("liquidity") or market.get("liquidityNum")),
                volume=as_float(market.get("volume") or market.get("volumeNum")),
                outcome=outcome,
                price=ask,
                token_id=token_id,
                score=1.0,
                url=url,
                best_bid=round(1.0 - ask - 0.01, 4),
                best_ask=ask,
                tick_size=tick_size,
                neg_risk=neg_risk,
                accepts_orders=True,
                event_slug=event_slug,
            )

        yes_cand = _make_candidate(yes_token, yes_outcome, yes_ask)
        no_cand = _make_candidate(no_token, no_outcome, no_ask)

        # Place YES leg first
        yes_pos = portfolio.record_arb_leg(yes_cand, yes_stake, entry_price=yes_ask)
        if yes_pos is None:
            continue  # token already held or recently closed

        if settings.dry_run:
            yes_order = {"dry_run": True, "side": "BUY", "amount": yes_stake, "price": yes_ask}
            yes_response = {"success": True, "orderID": f"arb-yes-{int(utc_now().timestamp()*1000)}"}
        else:
            try:
                yes_order, yes_response = client.place_market_order(
                    candidate=yes_cand, amount=yes_stake, price=yes_ask, side="BUY"
                )
            except Exception as exc:
                portfolio.positions = [p for p in portfolio.positions if p.get("token_id") != yes_token or p.get("status") != "open"]
                portfolio.cash = round(portfolio.cash + yes_stake, 2)
                print(f"   arb YES leg failed: {exc}", flush=True)
                continue

        yes_pos["strategy"] = f"{strategy_name}_arb"
        yes_pos["is_arb"] = True

        # Place NO leg
        no_pos = portfolio.record_arb_leg(no_cand, no_stake, entry_price=no_ask)
        if no_pos is None:
            # YES filled but NO blocked — keep YES as a normal position
            results.append({"arb": "partial", "leg": "yes_only", "question": question})
            portfolio.save(settings.state_path)
            continue

        if settings.dry_run:
            no_order = {"dry_run": True, "side": "BUY", "amount": no_stake, "price": no_ask}
            no_response = {"success": True, "orderID": f"arb-no-{int(utc_now().timestamp()*1000)}"}
        else:
            try:
                from .trading import _build_direct_buy_order
                no_order, no_response = client.place_market_order(
                    candidate=no_cand, amount=no_stake, price=no_ask, side="BUY"
                )
            except Exception as exc:
                print(f"   arb NO leg failed: {exc} — YES-only position stays", flush=True)
                no_pos["is_arb"] = False  # downgrade to normal grinder position
                results.append({"arb": "partial", "leg": "yes_only", "question": question})
                portfolio.save(settings.state_path)
                continue

        no_pos["strategy"] = f"{strategy_name}_arb"
        no_pos["is_arb"] = True

        portfolio.save(settings.state_path)
        results.append({
            "arb": "full",
            "question": question,
            "yes_ask": yes_ask,
            "no_ask": no_ask,
            "total_cost": total_cost,
            "guaranteed_profit_pct": profit_pct,
            "yes_stake": yes_stake,
            "no_stake": no_stake,
        })
        print(
            f"✅ ARB PLACED: {question[:55]} | "
            f"YES@{yes_ask:.3f}×${yes_stake:.2f} + NO@{no_ask:.3f}×${no_stake:.2f} | "
            f"guaranteed +{profit_pct:.1%} | ${yes_stake+no_stake:.2f} deployed",
            flush=True,
        )
    return results


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


def championdumonde_breakout_once(settings: Settings) -> dict[str, Any]:
    return _run_race_tick(
        settings,
        "championdumonde_breakout",
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


def pmlepgm_counter_panic_fade_once(settings: Settings) -> dict[str, Any]:
    return _run_race_tick(
        settings,
        "pmlepgm_counter_panic_fade",
        lambda eligible: select_counter_panic_fade(
            eligible,
            settings.race_max_orders_per_tick,
            settings.race_panic_fade_min_move,
            settings.race_panic_fade_min_volume,
        ),
    )


def pmlepgm_counter_panic_fade_loop(settings: Settings) -> None:
    from .main import strategy_loop

    strategy_loop(settings, "pmlepgm_counter_panic_fade", pmlepgm_counter_panic_fade_once)


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


def championdumonde_breakout_loop(settings: Settings) -> None:
    from .main import strategy_loop

    strategy_loop(settings, "championdumonde_breakout", championdumonde_breakout_once)


def late_favorite_loop(settings: Settings) -> None:
    from .main import strategy_loop

    strategy_loop(settings, "late_favorite", late_favorite_once)


# ---------------------------------------------------------------------------
# Framework Rules — once/loop wrappers for the 17 race-mode strategies.
# ---------------------------------------------------------------------------


def _race_strategy(name: str, selector):
    def _once(settings: Settings) -> dict[str, Any]:
        return _run_race_tick(
            settings,
            name,
            lambda eligible: selector(eligible, settings.race_max_orders_per_tick),
        )

    def _loop(settings: Settings) -> None:
        from .main import strategy_loop

        strategy_loop(settings, name, _once)

    return _once, _loop


hybrid_smart_money_once, hybrid_smart_money_loop = _race_strategy(
    "hybrid_smart_money", select_hybrid_smart_money
)
smart_wallet_consensus_once, smart_wallet_consensus_loop = _race_strategy(
    "smart_wallet_consensus", select_smart_wallet_consensus
)
whale_entry_once, whale_entry_loop = _race_strategy(
    "whale_entry_detection", select_whale_entry
)
elite_momentum_consensus_once, elite_momentum_consensus_loop = _race_strategy(
    "elite_momentum_consensus", select_elite_momentum_consensus
)
wallet_cluster_once, wallet_cluster_loop = _race_strategy(
    "wallet_cluster_correlation", select_wallet_cluster
)
early_momentum_once, early_momentum_loop = _race_strategy(
    "early_momentum_detection", select_early_momentum
)
liquidity_vacuum_once, liquidity_vacuum_loop = _race_strategy(
    "liquidity_vacuum_breakout", select_liquidity_vacuum
)
mean_reversion_fade_once, mean_reversion_fade_loop = _race_strategy(
    "mean_reversion_fade", select_mean_reversion_fade
)
range_channel_once, range_channel_loop = _race_strategy(
    "range_channel_trading", select_range_channel
)
aggressive_buyer_once, aggressive_buyer_loop = _race_strategy(
    "aggressive_buyer_detection", select_aggressive_buyer
)
orderbook_imbalance_once, orderbook_imbalance_loop = _race_strategy(
    "orderbook_imbalance", select_orderbook_imbalance
)
late_momentum_chase_once, late_momentum_chase_loop = _race_strategy(
    "late_momentum_chase", select_late_momentum_chase
)
weak_holder_flush_once, weak_holder_flush_loop = _race_strategy(
    "weak_holder_flush", select_weak_holder_flush
)
weak_holder_flush_inverse_once, weak_holder_flush_inverse_loop = _race_strategy(
    "weak_holder_flush_inverse", select_weak_holder_flush_inverse
)
grinder_once, grinder_loop = _race_strategy("grinder", select_grinder)
def select_claude_anti_favorite(eligible: list[tuple[Candidate, float]], n: int) -> list[Candidate]:
    """Buy outcomes with NEGATIVE momentum in mid range. Bet on reversal."""
    qualified = [
        (c, -mom) for c, mom in eligible
        if mom <= -0.02 and 0.30 <= (c.best_ask or 0.5) <= 0.50
        and (c.volume or 0) >= 500.0
    ]
    return _dedupe_top_n(qualified, n)


def select_claude_mid_dump_fade(eligible: list[tuple[Candidate, float]], n: int) -> list[Candidate]:
    """Fade modest dumps in mid-priced markets."""
    qualified = [
        (c, -mom) for c, mom in eligible
        if -0.08 <= mom <= -0.03 and 0.30 <= (c.best_ask or 0.5) <= 0.70
        and (c.volume or 0) >= 1000.0
    ]
    return _dedupe_top_n(qualified, n)


def select_claude_resolution_sniper(eligible: list[tuple[Candidate, float]], n: int) -> list[Candidate]:
    """bid >= 0.92 + <= 90 min. Last-mile near-certain favorites (loosened from 0.97/30min)."""
    qualified = [
        (c, c.best_bid or 0.0) for c, _ in eligible
        if (c.best_bid or 0.0) >= 0.92
        and (c.hours_to_close or 99.0) <= 1.5
        and (c.volume or 0) >= 100.0
    ]
    return _dedupe_top_n(qualified, n)


def select_claude_high_vol_quiet(eligible: list[tuple[Candidate, float]], n: int) -> list[Candidate]:
    """High-volume markets with slight positive momentum (0-3%)."""
    qualified = [
        (c, mom) for c, mom in eligible
        if 0.0 <= mom <= 0.03 and (c.volume or 0) >= 5000.0
        and 0.15 <= (c.best_ask or 1.0) <= 0.85
    ]
    return _dedupe_top_n(qualified, n)


def select_claude_lottery_balanced(eligible: list[tuple[Candidate, float]], n: int) -> list[Candidate]:
    """Cheap long-shots (ask 0.10-0.20) that aren't actively dumping."""
    qualified = [
        (c, mom) for c, mom in eligible
        if 0.10 <= (c.best_ask or 0.0) <= 0.20
        and mom >= 0.0
        and (c.volume or 0) >= 500.0
    ]
    return _dedupe_top_n(qualified, n)


def select_claude_strong_breakout(eligible: list[tuple[Candidate, float]], n: int) -> list[Candidate]:
    """Big runs with room to continue: mom>=15% + ask<=0.70."""
    qualified = [
        (c, mom) for c, mom in eligible
        if mom >= 0.15 and (c.best_ask or 1.0) <= 0.70
        and (c.volume or 0) >= 2000.0
    ]
    return _dedupe_top_n(qualified, n)


def select_claude_frozen_favorite(eligible: list[tuple[Candidate, float]], n: int) -> list[Candidate]:
    """Locked-in favorites: abs(mom)<=0.5% + bid>=0.60 + <=3h."""
    qualified = [
        (c, c.best_bid or 0.0) for c, mom in eligible
        if abs(mom) <= 0.005 and (c.best_bid or 0.0) >= 0.60
        and (c.hours_to_close or 99.0) <= 3.0
        and (c.volume or 0) >= 500.0
    ]
    return _dedupe_top_n(qualified, n)


def select_claude_mid_rebound(eligible: list[tuple[Candidate, float]], n: int) -> list[Candidate]:
    """Coin-flip mild-dip rebound: ask 0.35-0.65 + mom -2 to -6%."""
    qualified = [
        (c, -mom) for c, mom in eligible
        if -0.06 <= mom <= -0.02 and 0.35 <= (c.best_ask or 0.5) <= 0.65
        and (c.volume or 0) >= 500.0
    ]
    return _dedupe_top_n(qualified, n)


def select_claude_high_vol_panic(eligible: list[tuple[Candidate, float]], n: int) -> list[Candidate]:
    """Heavy-volume panic: mom<=-10% + vol>=$5k. Bounce candidates."""
    qualified = [
        (c, -mom) for c, mom in eligible
        if mom <= -0.10 and (c.volume or 0) >= 5000.0
    ]
    return _dedupe_top_n(qualified, n)


def select_claude_high_vol_pop(eligible: list[tuple[Candidate, float]], n: int) -> list[Candidate]:
    """Heavy-volume rally: mom>=+10% + vol>=$5k. Continuation play."""
    qualified = [
        (c, mom) for c, mom in eligible
        if mom >= 0.10 and (c.volume or 0) >= 5000.0
    ]
    return _dedupe_top_n(qualified, n)


def select_pm_le_pgm_weak_holder_flush_inverse(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """Restored to the original WHF-inverse thesis that won the dry race.

    Buys the rising side (mom ≥ +10%, vol ≥ $2k) of a market where the
    other outcome is being panic-dumped. Will sit idle when no markets
    show that momentum — that's the point. The edge is in being
    selective, not in trading constantly.
    """
    qualified = [
        (c, mom)
        for c, mom in eligible
        if mom >= 0.10
        and (c.volume or 0) >= 2000.0
    ]
    return _dedupe_top_n(qualified, n)


pm_le_pgm_weak_holder_flush_inverse_once, pm_le_pgm_weak_holder_flush_inverse_loop = _race_strategy(
    "pm_le_pgm_weak_holder_flush_inverse", select_pm_le_pgm_weak_holder_flush_inverse
)
claude_anti_favorite_once, claude_anti_favorite_loop = _race_strategy("claude_anti_favorite", select_claude_anti_favorite)
claude_mid_dump_fade_once, claude_mid_dump_fade_loop = _race_strategy("claude_mid_dump_fade", select_claude_mid_dump_fade)
claude_resolution_sniper_once, claude_resolution_sniper_loop = _race_strategy("claude_resolution_sniper", select_claude_resolution_sniper)
claude_high_vol_quiet_once, claude_high_vol_quiet_loop = _race_strategy("claude_high_vol_quiet", select_claude_high_vol_quiet)
claude_lottery_balanced_once, claude_lottery_balanced_loop = _race_strategy("claude_lottery_balanced", select_claude_lottery_balanced)
claude_strong_breakout_once, claude_strong_breakout_loop = _race_strategy("claude_strong_breakout", select_claude_strong_breakout)
claude_frozen_favorite_once, claude_frozen_favorite_loop = _race_strategy("claude_frozen_favorite", select_claude_frozen_favorite)
claude_mid_rebound_once, claude_mid_rebound_loop = _race_strategy("claude_mid_rebound", select_claude_mid_rebound)
claude_high_vol_panic_once, claude_high_vol_panic_loop = _race_strategy("claude_high_vol_panic", select_claude_high_vol_panic)
claude_high_vol_pop_once, claude_high_vol_pop_loop = _race_strategy("claude_high_vol_pop", select_claude_high_vol_pop)
claude_oversold_bounce_once, claude_oversold_bounce_loop = _race_strategy(
    "claude_oversold_bounce", select_claude_oversold_bounce
)
claude_late_pump_once, claude_late_pump_loop = _race_strategy(
    "claude_late_pump", select_claude_late_pump
)
claude_extreme_consensus_once, claude_extreme_consensus_loop = _race_strategy(
    "claude_extreme_consensus", select_claude_extreme_consensus
)
claude_balanced_mid_once, claude_balanced_mid_loop = _race_strategy(
    "claude_balanced_mid", select_claude_balanced_mid
)
claude_resolution_clock_once, claude_resolution_clock_loop = _race_strategy(
    "claude_resolution_clock", select_claude_resolution_clock
)
claude_endgame_sweep_once, claude_endgame_sweep_loop = _race_strategy(
    "claude_endgame_sweep", select_claude_endgame_sweep
)
claude_fade_extreme_once, claude_fade_extreme_loop = _race_strategy(
    "claude_fade_extreme", select_claude_fade_extreme
)
claude_mid_volume_band_once, claude_mid_volume_band_loop = _race_strategy(
    "claude_mid_volume_band", select_claude_mid_volume_band
)
claude_blue_chip_once, claude_blue_chip_loop = _race_strategy(
    "claude_blue_chip", select_claude_blue_chip
)
claude_volume_spike_once, claude_volume_spike_loop = _race_strategy(
    "claude_volume_spike", select_claude_volume_spike
)
claude_mid_endgame_once, claude_mid_endgame_loop = _race_strategy(
    "claude_mid_endgame", select_claude_mid_endgame
)
# kzer used to be a YES+NO arb scanner (polymarket_bot/kzer_arb.py); arbs
# get eaten by faster bots in milliseconds and live execution was never
# wired up. Repurposed to run the same endgame-sweep selector as
# claude_endgame_sweep but tracked under its own ledger.
kzer_endgame_once, kzer_endgame_loop = _race_strategy(
    "kzerlepgm_ultimatestrategy", select_claude_endgame_sweep
)
probability_drift_once, probability_drift_loop = _race_strategy(
    "probability_drift", select_probability_drift
)
resolution_compression_once, resolution_compression_loop = _race_strategy(
    "resolution_compression", select_resolution_compression
)
liquidity_absorption_once, liquidity_absorption_loop = _race_strategy(
    "liquidity_absorption", select_liquidity_absorption
)
momentum_exhaustion_once, momentum_exhaustion_loop = _race_strategy(
    "momentum_exhaustion_reversal", select_momentum_exhaustion
)
micro_scalping_once, micro_scalping_loop = _race_strategy(
    "micro_scalping", select_micro_scalping
)
multi_signal_consensus_once, multi_signal_consensus_loop = _race_strategy(
    "multi_signal_consensus", select_multi_signal_consensus
)
