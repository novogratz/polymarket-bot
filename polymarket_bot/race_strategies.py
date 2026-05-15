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
from .models import Candidate, as_float, is_excluded_market, parse_dt, parse_json_list, utc_now
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
        if is_excluded_market(market):
            continue
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
        if mom < 0.03 or (c.volume or 0) < 1000.0:
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
        if (c.volume or 0) >= 2500.0 and mom > 0
    ]
    return _dedupe_top_n(qualified, n)


def select_whale_entry(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#2: outsized volume + positive momentum (whale-trade proxy)."""
    qualified = [
        (c, (c.volume or 0.0) * max(mom, 0.0))
        for c, mom in eligible
        if (c.volume or 0) >= 5000.0 and mom >= 0.02
    ]
    return _dedupe_top_n(qualified, n)


def select_wallet_cluster(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#3: degraded — true correlation needs offline co-trading graph."""
    qualified = [
        (c, mom + (c.volume or 0.0) / 100000.0)
        for c, mom in eligible
        if (c.volume or 0) >= 1500.0 and mom >= 0.02
    ]
    return _dedupe_top_n(qualified, n)


def select_early_momentum(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#4: small early move (2-8%) with volume floor — catch before expansion."""
    qualified = [
        (c, mom)
        for c, mom in eligible
        if 0.02 <= mom <= 0.08 and (c.volume or 0) >= 500.0
    ]
    return _dedupe_top_n(qualified, n)


def select_liquidity_vacuum(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#5: thin liquidity + breakout momentum — exploit shallow asks."""
    qualified = [
        (c, mom)
        for c, mom in eligible
        if (c.liquidity or 0) < 1500.0 and mom >= 0.05
    ]
    return _dedupe_top_n(qualified, n)


def select_mean_reversion_fade(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#6: fade modest emotional dumps (-5%..-10%, smaller than panic_fade)."""
    qualified = [
        (c, -mom)
        for c, mom in eligible
        if -0.10 <= mom <= -0.05 and (c.volume or 0) >= 750.0
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
        and (c.volume or 0) >= 250.0
    ]
    return _dedupe_top_n(qualified, n)


def select_aggressive_buyer(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#8: strong momentum + high volume (aggressive bidding proxy)."""
    qualified = [
        (c, mom * (c.volume or 1.0))
        for c, mom in eligible
        if mom >= 0.06 and (c.volume or 0) >= 2000.0
    ]
    return _dedupe_top_n(qualified, n)


def select_orderbook_imbalance(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#9: tight spread + mid-price + positive momentum (depth proxy)."""
    qualified: list[tuple[Candidate, float]] = []
    for c, mom in eligible:
        bid, ask = c.best_bid or 0.0, c.best_ask or 1.0
        if not (0 <= ask - bid <= 0.03):
            continue
        if mom < 0.02:
            continue
        if not (0.25 <= ask <= 0.75):
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
        if mom >= 0.08
        and (c.hours_to_close or 99) <= 1.5
        and (c.volume or 0) >= 1000.0
    ]
    return _dedupe_top_n(qualified, n)


def select_weak_holder_flush(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#12: buy panic dumps (≥10% drop) with volume — like panic_fade, harsher."""
    qualified = [
        (c, -mom)
        for c, mom in eligible
        if mom <= -0.10 and (c.volume or 0) >= 1000.0
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
        if mom <= -0.15
        and (c.best_ask or 1.0) <= 0.30
        and (c.volume or 0) >= 750.0
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
        if mom >= 0.10
        and (c.hours_to_close or 99.0) <= 0.5
        and (c.volume or 0) >= 1000.0
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
            mom >= 0.03
            and (c.volume or 0) >= 1500.0
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
        if 0.45 <= (c.best_ask or 0.5) <= 0.55
        and mom >= 0.05
        and (c.volume or 0) >= 1500.0
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
        if (c.best_bid or 0.0) >= 0.80
        and (c.hours_to_close or 99.0) <= 0.25
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
        and (c.volume or 0) >= 100.0
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
        and (c.volume or 0) >= 5000.0
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
        if (c.volume or 0) < 10000.0:
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
        if (c.volume or 0) >= 25000.0
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
        and (c.volume or 0) >= 250.0
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
        if mom >= 0.10 and (c.volume or 0) >= 1000.0
    ]
    return _dedupe_top_n(qualified, n)


def select_probability_drift(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#13: slow steady directional move (1.5-6% momentum, mid-price)."""
    qualified = [
        (c, mom)
        for c, mom in eligible
        if 0.015 <= mom <= 0.06
        and (c.volume or 0) >= 400.0
        and 0.20 <= (c.best_ask or 1.0) <= 0.80
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
        if -0.06 <= mom <= -0.02 and (c.volume or 0) >= 1500.0
    ]
    return _dedupe_top_n(qualified, n)


def select_momentum_exhaustion(
    eligible: list[tuple[Candidate, float]], n: int
) -> list[Candidate]:
    """#16: near-zero momentum + prior high volume (exhaustion proxy)."""
    qualified = [
        (c, c.volume or 0.0)
        for c, mom in eligible
        if abs(mom) <= 0.015 and (c.volume or 0) >= 2500.0
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
        if mom >= 0.03:
            signals += 1
        if (c.volume or 0) >= 1500.0:
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


def _simple_exit_plan(position: dict[str, Any], current_pnl_pct: float, settings: Settings) -> dict[str, Any] | None:
    shares = float(position.get("shares", 0.0) or 0.0)
    if shares <= 0:
        return None
    # Universal min-hold: no sell of any kind before sl_min_age_minutes.
    age = _position_age_minutes(position)
    if age < settings.race_sl_min_age_minutes:
        return None
    if current_pnl_pct >= settings.race_tp_pct:
        return {"reason": "race_take_profit", "shares": shares}
    if current_pnl_pct <= -settings.race_sl_pct:
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
        # Manage own-tagged + live_sync positions (the latter being
        # positions synced from the CLOB account that this strategy
        # didn't open itself). Without this, switching live strategies
        # orphans the previous one's open positions until expiry.
        position_strategy = str(position.get("strategy") or "")
        if position_strategy != strategy_name and position_strategy != "live_sync":
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
            and _position_age_minutes(position) >= settings.race_sl_min_age_minutes
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

            # Auto-write-off: if the position is past its expiry (or resolved
            # as loser) we can't get a live SELL through — accept the bid as
            # the realized price and close locally so it doesn't linger.
            mtc_now = _minutes_to_close(position)
            expired = mtc_now is not None and mtc_now <= 0
            resolved_loser = candidate.best_bid is not None and candidate.best_bid <= 0.05
            if expired or resolved_loser:
                writeoff_price = max(float(candidate.best_bid or 0.0), 0.0)
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
            rejected.append({"question": candidate.question, "reason": "already_open_market"})
            continue
        if portfolio.has_open_token(candidate.token_id):
            rejected.append({"question": candidate.question, "reason": "already_open_token"})
            continue
        if portfolio.has_pending_token(candidate.token_id):
            rejected.append({"question": candidate.question, "reason": "pending_order"})
            continue
        if portfolio.has_open_event_position(candidate):
            rejected.append({"question": candidate.question, "reason": "already_open_event"})
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
        # Equity-scaled sizing: a fixed % of current equity, capped by
        # the profile's position ceiling. Means a $100 → $200 bankroll
        # automatically doubles the per-trade size. race_stake_usd is
        # kept as the absolute floor so micro-bankrolls still clear the
        # $1 CLOB minimum.
        equity = float(portfolio.summary().get("equity", portfolio.cash))
        target = max(equity * settings.race_stake_pct, 1.0)
        if settings.smart_max_position_ceiling_usd > 0:
            target = min(target, settings.smart_max_position_ceiling_usd)
        stake = min(target, cash_above_floor)
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
