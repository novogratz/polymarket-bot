"""Edge strategy: multi-lane high-frequency trading on <4h markets.

Four lanes, run in priority order. Each emits :class:`EdgeSignal`
records that are sized via fractional Kelly, gated by daily-drawdown
and per-trade risk caps, then executed through the shared
``execute_live_trade`` path so the journal / dashboard / notifications
work unchanged.

Lanes:

1. **Arbitrage** — binary YES+NO market where ``ask_yes + ask_no`` is
   below ``1 - fee_buffer``. Lock in risk-free profit by buying both
   sides at the same time. Rare in practice; we still scan because the
   one we find pays for the whole tick.
2. **Crypto directional** — short-expiry "X Up or Down" markets where
   our spot+momentum model gives a fair probability that diverges from
   the market mid by ``min_edge_pct`` after fees. Buys the undervalued
   side. Binance is the spot reference.
3. **Near-certainty** — short-expiry favorite riding: when ``best_bid``
   is already ≥ ``min_prob`` and ``best_ask`` is still ≤ ``max_ask``,
   we buy and let it resolve.
4. **Scalp** — tight-spread, high-volume momentum: enter at ask, exit
   at ask+``scalp_tp_ticks``. Cheap, fast, small.

Risk management:

- Per-trade stake is fractional Kelly (default 1/4) capped at
  ``max_position_pct * cash``.
- Daily realized PnL is tracked across the journal; trading halts for
  the day if it drops below ``-daily_drawdown_pct * starting_equity``.
- Per-position stop-loss + adaptive near-expiry tightening.

Only hard rule: ``end_date - now`` ≤ 4h (configurable, but never
relaxed without explicit operator change).
"""

from __future__ import annotations

import datetime as dt
import math
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import notifications
from .config import Settings
from .external_prices import (
    ASSET_TO_SYMBOL,
    SpotQuote,
    fetch_spot_quotes_for_assets,
)
from .gamma import GammaClient
from .models import Candidate, as_float, is_excluded_market, parse_dt, parse_json_list, utc_now
from .news_strategy import _asset_key, _event_slug, _quote_for_outcome
from .portfolio import Portfolio
from .pricing import ensure_open_positions_in_pool
from .trading import build_client, execute_live_sell, execute_live_trade


# Lane identifiers (used as journal tags and in the rationale string).
LANE_ARB = "arb"
LANE_CRYPTO = "crypto_directional"
LANE_NEAR_CERT = "near_certainty"
LANE_SCALP = "scalp"


@dataclass
class EdgeSignal:
    """One actionable trade idea from any of the four lanes."""

    lane: str
    candidate: Candidate
    fair_prob: float  # our estimated true probability of YES on this outcome
    market_price: float  # current ask we'd pay
    edge_pct: float  # fair_prob - market_price, after fees
    confidence: float  # 0..1 used to scale Kelly
    stake_usd: float
    rationale: str
    extra: dict[str, Any] = field(default_factory=dict)


def _step(settings: Settings, msg: str) -> None:
    if not settings.quiet:
        print(msg, flush=True)


def _load_short_expiry_markets(settings: Settings) -> list[dict[str, Any]]:
    client = GammaClient(settings.gamma_base_url)
    now = utc_now()
    horizon = now + timedelta(hours=settings.edge_max_hours)
    batches: list[list[dict[str, Any]]] = []
    for kwargs in (
        {
            "limit": settings.edge_scan_limit,
            "end_date_min": now,
            "end_date_max": horizon,
            "order": "end_date",
            "ascending": True,
        },
        {
            "limit": settings.edge_scan_limit,
            "end_date_min": now,
            "end_date_max": horizon,
            "order": "volume",
            "ascending": False,
        },
    ):
        try:
            batches.append(client.get_markets(**kwargs))
        except Exception as exc:
            print(f"⚠️  edge: gamma batch failed: {type(exc).__name__}: {exc}")
    merged: dict[str, dict[str, Any]] = {}
    for batch in batches:
        for market in batch:
            key = str(market.get("id") or market.get("conditionId") or "")
            if key and key not in merged:
                merged[key] = market
    return list(merged.values())


def _market_passes_basic_filters(market: dict[str, Any], settings: Settings) -> bool:
    if is_excluded_market(market):
        return False
    end_date = parse_dt(market.get("endDate"))
    if end_date is None:
        return False
    now = utc_now()
    if end_date < now + timedelta(minutes=5) or end_date > now + timedelta(hours=settings.edge_max_hours):
        return False
    if not bool(market.get("acceptingOrders")):
        return False
    liquidity = as_float(market.get("liquidity") or market.get("liquidityNum"))
    volume_24h = as_float(market.get("volume24hr") or market.get("volume24hrClob"))
    if liquidity < settings.edge_min_liquidity_usd:
        return False
    if volume_24h < settings.edge_min_volume_24h_usd:
        return False
    return True


def _build_binary_quotes(
    market: dict[str, Any],
) -> tuple[Candidate, Candidate] | None:
    """Construct YES and NO Candidate instances for a binary market.

    Returns ``None`` when the market isn't a clean binary with bid/ask
    on both sides — the edge lanes need both quotes to act.
    """
    outcomes = [str(item) for item in parse_json_list(market.get("outcomes"))]
    prices = [as_float(item, -1.0) for item in parse_json_list(market.get("outcomePrices"))]
    token_ids = [str(item) for item in parse_json_list(market.get("clobTokenIds"))]
    if len(outcomes) != 2 or len(prices) != 2:
        return None

    market_best_bid = as_float(market.get("bestBid"), default=None) if market.get("bestBid") is not None else None
    market_best_ask = as_float(market.get("bestAsk"), default=None) if market.get("bestAsk") is not None else None
    tick_raw = market.get("orderPriceMinTickSize")
    tick_size = as_float(tick_raw, default=None) if tick_raw is not None else None
    if market_best_bid is None or market_best_ask is None or tick_size is None or tick_size <= 0:
        return None

    end_date = parse_dt(market.get("endDate"))
    if end_date is None:
        return None
    now = utc_now()
    hours_to_close = max((end_date - now).total_seconds() / 3600.0, 0.0)
    slug = str(market.get("slug") or market.get("id") or "")
    event_slug = _event_slug(market)
    market_id = str(market.get("id") or "")
    question = str(market.get("question") or "")
    url = (
        f"https://polymarket.com/event/{event_slug or slug}"
        if (event_slug or slug)
        else "https://polymarket.com"
    )
    neg_risk = bool(market.get("negRisk"))
    liquidity = as_float(market.get("liquidity") or market.get("liquidityNum"))
    volume = as_float(market.get("volume") or market.get("volumeNum"))

    candidates: list[Candidate] = []
    for index in range(2):
        price = prices[index]
        if price <= 0.0 or price >= 1.0:
            return None
        bid, ask = _quote_for_outcome(index, 2, market_best_bid, market_best_ask)
        if bid is None or ask is None:
            return None
        candidates.append(
            Candidate(
                market_id=market_id,
                question=question,
                slug=slug,
                end_date=end_date,
                hours_to_close=hours_to_close,
                liquidity=liquidity,
                volume=volume,
                outcome=outcomes[index],
                price=price,
                token_id=token_ids[index] if index < len(token_ids) else None,
                score=0.0,
                url=url,
                best_bid=bid,
                best_ask=ask,
                tick_size=tick_size,
                neg_risk=neg_risk,
                accepts_orders=True,
                event_slug=event_slug,
            )
        )
    return candidates[0], candidates[1]


# ---------------------------------------------------------------------------
# Lane 1: Arbitrage
# ---------------------------------------------------------------------------


def find_arb_opportunities(
    markets: list[dict[str, Any]],
    settings: Settings,
) -> list[tuple[Candidate, Candidate, float]]:
    """Find binary markets where YES+NO ask < (1 - fee_buffer).

    Returns ``(yes_candidate, no_candidate, edge)`` per opportunity.
    """
    out: list[tuple[Candidate, Candidate, float]] = []
    threshold = 1.0 - settings.edge_arb_fee_buffer
    for market in markets:
        if not _market_passes_basic_filters(market, settings):
            continue
        pair = _build_binary_quotes(market)
        if pair is None:
            continue
        yes, no = pair
        ask_sum = (yes.best_ask or 1.0) + (no.best_ask or 1.0)
        if ask_sum >= threshold:
            continue
        edge = threshold - ask_sum  # how much under cost we are
        out.append((yes, no, edge))
    out.sort(key=lambda t: t[2], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Lane 2: Crypto directional
# ---------------------------------------------------------------------------


_PRICE_BAND_RE = re.compile(r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:and|to|-)\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)", re.I)
_PRICE_ABOVE_RE = re.compile(r"(above|over|>=|more than)\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)", re.I)
_PRICE_BELOW_RE = re.compile(r"(below|under|<=|less than)\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)", re.I)


def _parse_price_levels(question: str) -> tuple[float | None, float | None]:
    """Extract (low, high) thresholds from question text.

    Returns:
    - ``(low, high)`` for band markets ("between $80,000 and $82,000")
    - ``(low, None)`` for "above $X"
    - ``(None, high)`` for "below $X"
    - ``(None, None)`` if nothing parseable (treat as Up/Down direction)
    """
    band = _PRICE_BAND_RE.search(question)
    if band:
        try:
            low = float(band.group(1).replace(",", ""))
            high = float(band.group(2).replace(",", ""))
            if low > high:
                low, high = high, low
            return low, high
        except ValueError:
            pass
    above = _PRICE_ABOVE_RE.search(question)
    if above:
        try:
            return float(above.group(2).replace(",", "")), None
        except ValueError:
            pass
    below = _PRICE_BELOW_RE.search(question)
    if below:
        try:
            return None, float(below.group(2).replace(",", ""))
        except ValueError:
            pass
    return None, None


def _normal_cdf(x: float) -> float:
    """Standard normal CDF via math.erf — no scipy dependency."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _crypto_fair_probability(
    question: str,
    outcome: str,
    quote: SpotQuote,
    hours_to_close: float,
    settings: Settings,
) -> tuple[float, str] | None:
    """Compute P(outcome resolves YES) using spot + momentum.

    Two cases:

    1. **Band/threshold markets** ("BTC above $X", "BTC between $X and
       $Y"): use a log-normal Brownian terminal-price model with the
       configured annualised vol.
    2. **Direction markets** ("X Up or Down"): bias 0.50 by the 15-min
       and 5-min momentum. Strong recent move means the trend is likely
       to persist over the next ≤4h.

    Returns ``(prob_yes_for_this_outcome, rationale_snippet)`` or
    ``None`` when we can't model.
    """
    spot = quote.price
    if spot <= 0 or hours_to_close <= 0:
        return None
    low, high = _parse_price_levels(question)
    tau_years = hours_to_close / (24.0 * 365.25)
    sigma = settings.edge_crypto_annual_vol

    is_yes_side = outcome.strip().lower() in {"yes", "up", "above"}

    if low is not None or high is not None:
        # Threshold model. d = ln(S/X) / (sigma * sqrt(tau)).
        if low is not None and high is not None:
            # Between low and high → CDF(d_high) - CDF(d_low).
            d_high = (math.log(spot / high) + 0.5 * sigma * sigma * tau_years) / (sigma * math.sqrt(tau_years))
            d_low = (math.log(spot / low) + 0.5 * sigma * sigma * tau_years) / (sigma * math.sqrt(tau_years))
            p_between = max(0.0, _normal_cdf(d_low) - _normal_cdf(d_high))
            p_yes = p_between if is_yes_side else 1.0 - p_between
            return p_yes, f"band[{low:g},{high:g}] spot={spot:.0f} p_between={p_between:.3f}"
        threshold = low if low is not None else high
        d = (math.log(spot / threshold) + 0.5 * sigma * sigma * tau_years) / (sigma * math.sqrt(tau_years))
        p_above = _normal_cdf(d)
        if low is not None:  # "above $X"
            p_yes_market = p_above
        else:  # "below $X"
            p_yes_market = 1.0 - p_above
        p_yes = p_yes_market if is_yes_side else 1.0 - p_yes_market
        return p_yes, f"threshold[{threshold:g}] spot={spot:.0f} p_above={p_above:.3f}"

    # Direction model (Up/Down): bias 0.50 by momentum.
    # Empirically unprofitable in live trading — the momentum signals are too
    # small to override the market's order-flow information. Disabled by
    # default in the edge profile; can be re-enabled via crypto_direction_enabled.
    if not settings.edge_crypto_direction_enabled:
        return None
    mom_signal = quote.momentum_15m * settings.edge_crypto_momentum_alpha
    p_up = 0.5 + mom_signal
    p_up = max(0.05, min(0.95, p_up))
    p_yes = p_up if is_yes_side else 1.0 - p_up
    return (
        p_yes,
        f"direction mom_15m={quote.momentum_15m:+.4f} mom_5m={quote.momentum_5m:+.4f} p_up={p_up:.3f}",
    )


def find_crypto_edge_opportunities(
    markets: list[dict[str, Any]],
    settings: Settings,
    spot_quotes: dict[str, SpotQuote],
) -> list[EdgeSignal]:
    """Crypto directional lane: spot + momentum → fair prob → edge.

    Within one binary market, we keep at most ONE side — the outcome
    with the higher fair probability AND positive edge. Holding both
    Up and Down is just paying fees twice; we always pick the side our
    model thinks is more likely to win.
    """
    out: list[EdgeSignal] = []
    for market in markets:
        if not _market_passes_basic_filters(market, settings):
            continue
        question = str(market.get("question") or "")
        asset_key = _asset_key(question, _event_slug(market), str(market.get("slug") or ""))
        if not asset_key or not asset_key.startswith("crypto:"):
            continue
        asset = asset_key.split(":", 1)[1]
        if asset not in spot_quotes:
            continue
        pair = _build_binary_quotes(market)
        if pair is None:
            continue
        yes, no = pair
        per_market: list[EdgeSignal] = []
        for candidate in (yes, no):
            if candidate.best_ask is None or candidate.best_bid is None:
                continue
            if candidate.best_ask < settings.edge_min_price or candidate.best_ask > settings.edge_max_price:
                continue
            spread = candidate.best_ask - candidate.best_bid
            if spread < 0 or spread > settings.edge_max_spread:
                continue
            result = _crypto_fair_probability(
                question, candidate.outcome, spot_quotes[asset], candidate.hours_to_close or 0.0, settings
            )
            if result is None:
                continue
            fair_prob, rationale = result
            edge_after_fees = fair_prob - candidate.best_ask - settings.edge_fee_pct
            if edge_after_fees < settings.edge_min_edge_pct:
                continue
            confidence = min(1.0, edge_after_fees / 0.10)
            per_market.append(
                EdgeSignal(
                    lane=LANE_CRYPTO,
                    candidate=candidate,
                    fair_prob=fair_prob,
                    market_price=candidate.best_ask,
                    edge_pct=edge_after_fees,
                    confidence=confidence,
                    stake_usd=0.0,
                    rationale=f"crypto/{asset} {rationale} fair={fair_prob:.3f} ask={candidate.best_ask:.3f} edge={edge_after_fees:+.3f}",
                    extra={
                        "asset": asset,
                        "spot": spot_quotes[asset].price,
                        "momentum_5m": spot_quotes[asset].momentum_5m,
                        "momentum_15m": spot_quotes[asset].momentum_15m,
                    },
                )
            )
        # Of the YES/NO sides on this binary market, keep only the one
        # with the higher fair probability (i.e. the side our model
        # thinks is most likely to win). Tiebreak on edge size.
        if per_market:
            best = max(per_market, key=lambda s: (s.fair_prob, s.edge_pct))
            out.append(best)
    out.sort(key=lambda s: s.edge_pct, reverse=True)
    return out


# ---------------------------------------------------------------------------
# Lane 3: Near-certainty (favorite riding)
# ---------------------------------------------------------------------------


def find_near_certainty_opportunities(
    markets: list[dict[str, Any]],
    settings: Settings,
) -> list[EdgeSignal]:
    """Favorite riding: best_bid already in the high zone, ask still buyable.

    Per market we keep only the side with the higher fair probability
    (the favorite). Buying both sides would just pay the spread twice.
    """
    out: list[EdgeSignal] = []
    for market in markets:
        if not _market_passes_basic_filters(market, settings):
            continue
        pair = _build_binary_quotes(market)
        if pair is None:
            continue
        per_market: list[EdgeSignal] = []
        for candidate in pair:
            if candidate.best_bid is None or candidate.best_ask is None:
                continue
            if candidate.hours_to_close is None:
                continue
            if candidate.hours_to_close > settings.edge_near_cert_max_hours:
                continue
            if candidate.best_bid < settings.edge_near_cert_min_bid:
                continue
            if candidate.best_ask > settings.edge_near_cert_max_ask:
                continue
            # Fair-prob model: assume market consensus (best_bid) underprices
            # near-certain favorites by a small bias factor (typically traders
            # demand a premium for capital lock-up on slow resolutions). The
            # multiplier is a tunable hypothesis — set to 1.0 to disable the
            # lane entirely without removing it from the dispatch.
            fair_prob = min(0.99, candidate.best_bid * settings.edge_near_cert_bias_multiplier)
            edge_after_fees = fair_prob - candidate.best_ask - settings.edge_fee_pct
            if edge_after_fees < settings.edge_min_edge_pct:
                continue
            per_market.append(
                EdgeSignal(
                    lane=LANE_NEAR_CERT,
                    candidate=candidate,
                    fair_prob=fair_prob,
                    market_price=candidate.best_ask,
                    edge_pct=edge_after_fees,
                    confidence=min(1.0, (fair_prob - 0.90) / 0.10) if fair_prob > 0.90 else 0.5,
                    stake_usd=0.0,
                    rationale=(
                        f"near-cert bid={candidate.best_bid:.3f} ask={candidate.best_ask:.3f} "
                        f"fair={fair_prob:.3f} h2c={candidate.hours_to_close:.2f}"
                    ),
                )
            )
        if per_market:
            best = max(per_market, key=lambda s: (s.fair_prob, s.edge_pct))
            out.append(best)
    out.sort(key=lambda s: s.edge_pct, reverse=True)
    return out


# ---------------------------------------------------------------------------
# Lane 4: Scalp
# ---------------------------------------------------------------------------


def find_scalp_opportunities(
    markets: list[dict[str, Any]],
    settings: Settings,
) -> list[EdgeSignal]:
    """Tight-spread, high-flow markets: enter at ask, target 1-tick exit.

    Exit logic is handled in :func:`_execute_edge_exits`. The lane only
    sources entries.
    """
    if not settings.edge_scalp_enabled:
        return []
    out: list[EdgeSignal] = []
    for market in markets:
        if not _market_passes_basic_filters(market, settings):
            continue
        volume_24h = as_float(market.get("volume24hr") or market.get("volume24hrClob"))
        if volume_24h < settings.edge_scalp_min_volume_24h:
            continue
        pair = _build_binary_quotes(market)
        if pair is None:
            continue
        for candidate in pair:
            if candidate.best_bid is None or candidate.best_ask is None or candidate.tick_size is None:
                continue
            if candidate.best_ask < settings.edge_min_price or candidate.best_ask > settings.edge_max_price:
                continue
            spread = candidate.best_ask - candidate.best_bid
            if spread > candidate.tick_size * 1.5:
                continue
            # The "edge" here is the scalp target — assume we can exit one tick higher.
            target = min(0.99, candidate.best_ask + candidate.tick_size)
            edge = (target - candidate.best_ask) - settings.edge_fee_pct
            if edge < settings.edge_min_edge_pct * 0.5:  # scalps tolerate smaller edges
                continue
            out.append(
                EdgeSignal(
                    lane=LANE_SCALP,
                    candidate=candidate,
                    fair_prob=candidate.best_ask + candidate.tick_size,
                    market_price=candidate.best_ask,
                    edge_pct=edge,
                    confidence=0.4,
                    stake_usd=0.0,
                    rationale=f"scalp spread={spread:.3f} vol24h=${volume_24h:.0f}",
                    extra={"scalp_target_price": target},
                )
            )
    out.sort(key=lambda s: s.edge_pct, reverse=True)
    return out


# ---------------------------------------------------------------------------
# Sizing & risk
# ---------------------------------------------------------------------------


def kelly_fraction(p: float, ask: float) -> float:
    """Full Kelly fraction for a binary bet at probability ``p``, paying 1.

    b = (1 - ask) / ask (net odds). f = (p*b - q) / b, clipped to [0, 1].
    """
    if ask <= 0.0 or ask >= 1.0:
        return 0.0
    b = (1.0 - ask) / ask
    q = 1.0 - p
    f = (p * b - q) / b
    return max(0.0, min(1.0, f))


def size_signal(
    signal: EdgeSignal,
    cash: float,
    equity: float,
    settings: Settings,
) -> float:
    """Fractional-Kelly stake, capped by per-trade and per-lane limits."""
    if cash <= 0 or equity <= 0:
        return 0.0
    kelly = kelly_fraction(signal.fair_prob, signal.market_price)
    fraction = kelly * settings.edge_kelly_fraction * signal.confidence
    raw_stake = equity * fraction
    cap_per_trade = equity * settings.edge_max_position_pct
    stake = min(raw_stake, cap_per_trade, cash)
    if signal.lane == LANE_ARB:
        stake = min(stake, equity * settings.edge_arb_max_position_pct)
    elif signal.lane == LANE_SCALP:
        stake = min(stake, equity * settings.edge_scalp_max_position_pct)
    return round(max(0.0, stake), 2)


def _daily_realized_pnl(journal_path: Path) -> float:
    """Sum realized PnL from journal entries closed today (UTC).

    Best-effort; on any parse error, returns 0.0 (don't halt trading
    because the journal is malformed).
    """
    if not journal_path.is_file():
        return 0.0
    today = utc_now().date()
    total = 0.0
    try:
        with journal_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    import json as _json

                    record = _json.loads(line)
                except Exception:
                    continue
                closed_at = record.get("closed_at")
                if not closed_at:
                    continue
                try:
                    closed_dt = dt.datetime.fromisoformat(str(closed_at).replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if closed_dt.date() != today:
                    continue
                try:
                    total += float(record.get("realized_pnl", 0.0) or 0.0)
                except (TypeError, ValueError):
                    continue
    except Exception:
        return 0.0
    return total


# ---------------------------------------------------------------------------
# Exits
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


def _edge_sell_plan(
    position: dict[str, Any],
    current_pnl_pct: float,
    settings: Settings,
) -> dict[str, Any] | None:
    shares = float(position.get("shares", 0.0) or 0.0)
    if shares <= 0:
        return None
    lane = str(position.get("edge_lane") or "")
    age_min = _position_age_minutes(position)
    minutes_left = _minutes_to_close(position)

    # Universal min-hold: no sell of any kind before stop_loss_min_age_minutes.
    if age_min < settings.edge_stop_loss_min_age_minutes:
        return None

    # Scalp: tight TP/SL, exit fast on stagnation.
    if lane == LANE_SCALP:
        if current_pnl_pct >= settings.edge_scalp_tp_pct:
            return {"reason": "edge_scalp_tp", "shares": shares}
        if current_pnl_pct <= -settings.edge_scalp_sl_pct:
            return {"reason": "edge_scalp_sl", "shares": shares}
        if age_min >= settings.edge_scalp_max_age_minutes:
            return {"reason": "edge_scalp_timeout", "shares": shares}

    # Arb: no per-position exit — locked in, wait for resolution.
    if lane == LANE_ARB:
        if minutes_left is not None and minutes_left <= 5:
            # Just before expiry, exit whichever side is winning.
            if current_pnl_pct > 0:
                return {"reason": "edge_arb_expiry_flush", "shares": shares}
        return None

    # Crypto / near-cert: TP at +25%, adaptive SL.
    if current_pnl_pct >= settings.edge_take_profit_pct:
        return {"reason": "edge_take_profit", "shares": shares}
    hours_left = (minutes_left / 60.0) if minutes_left is not None else 4.0
    sl_pct = settings.edge_stop_loss_pct
    if hours_left <= settings.edge_tight_stop_hours:
        sl_pct = settings.edge_tight_stop_pct
    if hours_left <= settings.edge_very_tight_stop_hours:
        sl_pct = settings.edge_very_tight_stop_pct
    if current_pnl_pct <= -sl_pct:
        return {"reason": "edge_stop_loss", "shares": shares}

    # Near-expiry positive flush.
    if (
        minutes_left is not None
        and minutes_left <= settings.edge_near_expiry_minutes
        and current_pnl_pct >= 0
    ):
        return {"reason": "edge_near_expiry", "shares": shares}
    return None


def _execute_edge_exits(
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
        # Only manage edge-tagged positions; leave news/smart-money positions alone.
        if str(position.get("strategy") or "") != "edge":
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
        plan = _edge_sell_plan(position, current_pnl_pct, settings)
        if plan is None and (
            settings.edge_resolved_exit_threshold > 0
            and candidate.best_bid >= settings.edge_resolved_exit_threshold
        ):
            plan = {"reason": "edge_resolved", "shares": float(position.get("shares", 0.0))}
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
                        f"   edge cancelled {len(cancelled)} resting order(s) on "
                        f"'{position.get('question')}'; will retry sell next tick",
                        flush=True,
                    )
                    continue
                # Cancel failed → force-close locally so we stop spamming.
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
                print(
                    f"🗑️  edge force-closed stuck position "
                    f"'{position.get('question')}' (CLOB balance < needed)",
                    flush=True,
                )
                continue
            print(
                f"⚠️  edge sell skipped on {position.get('question')}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            continue
        portfolio.save(settings.state_path)
        if position.get("status") == "closed":
            from .main import _append_trade_journal
            _append_trade_journal(settings, position, str(plan["reason"]))
        exits.append(
            {
                "market_id": position.get("market_id"),
                "question": position.get("question"),
                "action": "sell",
                "reason": plan["reason"],
                "lane": str(position.get("edge_lane") or ""),
                "pnl_pct": round(current_pnl_pct, 4),
                "order": result.order,
                "response": result.response,
            }
        )
    return exits


# ---------------------------------------------------------------------------
# Tick orchestration
# ---------------------------------------------------------------------------


def _open_asset_keys(portfolio: Portfolio) -> set[str]:
    keys: set[str] = set()
    for position in portfolio.positions:
        if position.get("status") != "open":
            continue
        question = str(position.get("question") or "")
        event_slug = str(position.get("event_slug") or "")
        key = _asset_key(question, event_slug)
        if key:
            keys.add(key)
    return keys


def _execute_signal(
    client: Any,
    settings: Settings,
    portfolio: Portfolio,
    signal: EdgeSignal,
) -> dict[str, Any] | None:
    """Place the buy described by ``signal`` and tag the position."""
    candidate = signal.candidate
    if not candidate.token_id:
        return None
    if portfolio.has_open_position(candidate.market_id):
        return {"action": "skip", "reason": "duplicate_open_market"}
    if portfolio.has_open_token(candidate.token_id):
        return {"action": "skip", "reason": "duplicate_open_token"}
    if portfolio.has_pending_token(candidate.token_id):
        return {"action": "skip", "reason": "pending_order_exists"}
    if portfolio.has_open_event_position(candidate):
        return {"action": "skip", "reason": "duplicate_open_event"}
    signal_payload = {
        "question": candidate.question,
        "selection_reason": signal.rationale,
        "selection_metrics": {
            "lane": signal.lane,
            "fair_prob": round(signal.fair_prob, 4),
            "market_price": round(signal.market_price, 4),
            "edge_pct": round(signal.edge_pct, 4),
            "confidence": round(signal.confidence, 3),
            "current_ask": candidate.best_ask,
            "current_bid": candidate.best_bid,
            "hours_to_close": round(candidate.hours_to_close or 0.0, 3),
            **signal.extra,
        },
        "tag": f"edge_{signal.lane}",
    }
    try:
        result = execute_live_trade(
            client,
            settings,
            candidate,
            portfolio,
            min_trade_usd=1.0,
            max_trade_usd=signal.stake_usd,
            strategy="edge",
            signal=signal_payload,
        )
    except ValueError as exc:
        return {"action": "skip", "reason": f"value_error:{exc}"}
    except Exception as exc:
        try:
            notifications.notify_error(
                "edge_order_rejected",
                str(exc)[:500],
                dedupe_key=f"edge_order_rejected:{candidate.token_id}",
            )
        except Exception:
            pass
        return {"action": "skip", "reason": f"{type(exc).__name__}: {exc}"}
    # Tag the just-created position with the lane so exits route correctly.
    for position in portfolio.positions:
        if (
            position.get("status") == "open"
            and position.get("token_id") == candidate.token_id
        ):
            position["edge_lane"] = signal.lane
            position["edge_fair_prob"] = round(signal.fair_prob, 4)
            position["edge_pct"] = round(signal.edge_pct, 4)
            if signal.lane == LANE_SCALP and "scalp_target_price" in signal.extra:
                position["edge_scalp_target_price"] = signal.extra["scalp_target_price"]
            break
    portfolio.save(settings.state_path)
    return {
        "action": "buy",
        "lane": signal.lane,
        "stake_usd": signal.stake_usd,
        "rationale": signal.rationale,
        "order": result.order,
        "response": result.response,
    }


def edge_once(settings: Settings) -> dict[str, Any]:
    """Single tick: scan, exit, rank, size, execute."""
    print("▶  edge tick start", flush=True)
    _step(settings, "   loading short-expiry markets...")
    markets = _load_short_expiry_markets(settings)
    _step(settings, f"   markets: {len(markets)} raw")

    portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)

    # Live-position sync: critical for correctness. Without it, a stale
    # local ledger (from a previous strategy / partial fill / etc.) will
    # try to sell shares that don't exist on-chain and every SELL fails
    # with "balance is not enough". The smart-money loop has done this
    # since v0.1; edge needs the same treatment.
    if settings.dry_run:
        _step(settings, "   [DRY-RUN] skipping live-position sync")
    elif settings.sync_live_positions:
        from .main import _sync_live_positions  # lazy: main imports this module

        _step(settings, "   syncing live positions...")
        sync_actions = _sync_live_positions(settings, portfolio)
        if sync_actions:
            closed = sum(1 for a in sync_actions if a.get("action") == "closed_stale_local_position")
            imported = sum(1 for a in sync_actions if a.get("action") == "imported_live_position")
            updated = sum(1 for a in sync_actions if a.get("action") == "updated_live_position")
            _step(
                settings,
                f"   sync: {closed} stale closed, {imported} imported, {updated} updated",
            )

    pair_candidates: list[Candidate] = []
    for market in markets:
        pair = _build_binary_quotes(market)
        if pair is not None:
            pair_candidates.extend(pair)
    pool = ensure_open_positions_in_pool(settings, portfolio, pair_candidates)
    portfolio.mark_to_market(pool)
    summary = portfolio.summary()
    _step(
        settings,
        f"   open positions: {summary['open_positions']} | cash ${summary['cash']:.2f}",
    )

    client = build_client(settings)
    exits = _execute_edge_exits(client, settings, portfolio, pool)
    sells = sum(1 for e in exits if e.get("action") == "sell")
    if sells:
        _step(settings, f"   edge exits: {sells}")

    if not settings.dry_run:
        try:
            live_cash = client.live_available_balance()
            portfolio.cash = round(live_cash, 2)
        except Exception as exc:
            print(f"   live cash refresh failed: {type(exc).__name__}: {exc}")

    summary = portfolio.summary()
    equity = float(summary.get("equity", 0.0) or 0.0)
    cash = float(summary.get("cash", 0.0) or 0.0)

    # Daily drawdown gate.
    starting_equity = max(settings.paper_balance_usd, settings.assumed_live_balance_usd, 1.0)
    if settings.edge_daily_drawdown_pct > 0:
        realized_today = _daily_realized_pnl(settings.trade_journal_path)
        dd_limit = -starting_equity * settings.edge_daily_drawdown_pct
        if realized_today <= dd_limit:
            print(
                f"🛑 edge: daily drawdown limit hit "
                f"(${realized_today:+.2f} ≤ ${dd_limit:+.2f}) — entries paused",
                flush=True,
            )
            portfolio.save(settings.state_path)
            return {
                "trade": None,
                "strategy": "edge",
                "status": "daily_drawdown_halt",
                "realized_today_usd": realized_today,
                "drawdown_limit_usd": dd_limit,
                "exits": exits,
                "summary": summary,
            }

    cash_floor = equity * settings.edge_cash_floor_pct if equity > 0 else 0.0
    if cash < max(1.0, cash_floor):
        portfolio.save(settings.state_path)
        return {
            "trade": None,
            "strategy": "edge",
            "status": "cash_floor",
            "available_cash": cash,
            "cash_floor_usd": cash_floor,
            "exits": exits,
            "summary": summary,
        }

    # Lane 1: arbitrage. Each arb opens TWO orders — we pre-deduct an
    # estimated stake before scanning the other lanes.
    arb_actions: list[dict[str, Any]] = []
    for yes_c, no_c, edge in find_arb_opportunities(markets, settings):
        # Optimal split: total stake S, allocate proportional to (1 - ask)
        # so YES and NO returns match. Simple equal-split is fine for
        # tight arbs; cap each leg to half of the per-trade budget.
        per_arb = min(equity * settings.edge_arb_max_position_pct, cash) / 2.0
        if per_arb < 1.0:
            break
        yes_signal = EdgeSignal(
            lane=LANE_ARB,
            candidate=yes_c,
            fair_prob=1.0 - no_c.best_ask,
            market_price=yes_c.best_ask,
            edge_pct=edge,
            confidence=1.0,
            stake_usd=round(per_arb, 2),
            rationale=f"arb yes_ask={yes_c.best_ask} no_ask={no_c.best_ask} sum={yes_c.best_ask + no_c.best_ask:.3f} edge={edge:+.3f}",
        )
        no_signal = EdgeSignal(
            lane=LANE_ARB,
            candidate=no_c,
            fair_prob=1.0 - yes_c.best_ask,
            market_price=no_c.best_ask,
            edge_pct=edge,
            confidence=1.0,
            stake_usd=round(per_arb, 2),
            rationale=f"arb leg-2 yes_ask={yes_c.best_ask} no_ask={no_c.best_ask} sum={yes_c.best_ask + no_c.best_ask:.3f}",
        )
        for sig in (yes_signal, no_signal):
            result = _execute_signal(client, settings, portfolio, sig)
            if result is not None:
                arb_actions.append(result)
        # Refresh after each arb so subsequent ones see updated cash.
        portfolio.save(settings.state_path)
        cash = portfolio.cash
        if cash < 2.0:
            break

    # Lane 2: crypto directional.
    crypto_assets_in_scope = set()
    for market in markets:
        key = _asset_key(
            str(market.get("question") or ""),
            _event_slug(market),
            str(market.get("slug") or ""),
        )
        if key and key.startswith("crypto:"):
            crypto_assets_in_scope.add(key.split(":", 1)[1])
    spot_quotes: dict[str, SpotQuote] = {}
    if crypto_assets_in_scope and settings.edge_crypto_enabled:
        try:
            spot_quotes = fetch_spot_quotes_for_assets(sorted(crypto_assets_in_scope))
        except Exception as exc:
            print(f"   edge: spot fetch failed: {type(exc).__name__}: {exc}")
        if spot_quotes:
            _step(settings, f"   spot quotes: {len(spot_quotes)} asset(s)")

    crypto_signals = (
        find_crypto_edge_opportunities(markets, settings, spot_quotes)
        if settings.edge_crypto_enabled
        else []
    )

    # Lane 3: near-certainty.
    near_signals = (
        find_near_certainty_opportunities(markets, settings)
        if settings.edge_near_cert_enabled
        else []
    )

    # Lane 4: scalp.
    scalp_signals = find_scalp_opportunities(markets, settings)

    # Merge non-arb signals, dedupe by token, rank by edge.
    all_signals: list[EdgeSignal] = []
    seen_tokens: set[str] = set()
    for batch in (crypto_signals, near_signals, scalp_signals):
        for sig in batch:
            tok = sig.candidate.token_id or ""
            if tok in seen_tokens:
                continue
            all_signals.append(sig)
            if tok:
                seen_tokens.add(tok)
    all_signals.sort(key=lambda s: s.edge_pct, reverse=True)
    _step(
        settings,
        f"   signals: crypto={len(crypto_signals)} near={len(near_signals)} scalp={len(scalp_signals)}",
    )

    open_assets = _open_asset_keys(portfolio)
    executed: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    cash_above_floor = max(0.0, cash - cash_floor)

    for sig in all_signals:
        if len(executed) >= settings.edge_max_orders_per_tick:
            break
        if cash_above_floor < 1.0:
            break
        asset_key = _asset_key(sig.candidate.question, sig.candidate.event_slug or "", sig.candidate.slug or "")
        if asset_key and asset_key in open_assets:
            rejected.append(
                {
                    "question": sig.candidate.question,
                    "lane": sig.lane,
                    "reason": f"duplicate_asset:{asset_key}",
                }
            )
            continue
        sig.stake_usd = size_signal(sig, cash_above_floor, equity, settings)
        if sig.stake_usd < settings.edge_min_stake_usd:
            rejected.append(
                {
                    "question": sig.candidate.question,
                    "lane": sig.lane,
                    "reason": f"stake_below_min ({sig.stake_usd:.2f})",
                }
            )
            continue
        result = _execute_signal(client, settings, portfolio, sig)
        if result is None:
            continue
        if result.get("action") == "buy":
            executed.append({**result, "rationale": sig.rationale, "edge_pct": sig.edge_pct})
            if asset_key:
                open_assets.add(asset_key)
            cash_above_floor = max(0.0, portfolio.cash - cash_floor)
        else:
            rejected.append(
                {
                    "question": sig.candidate.question,
                    "lane": sig.lane,
                    "reason": result.get("reason", "unknown"),
                }
            )

    portfolio.save(settings.state_path)
    return {
        "trade": (executed or arb_actions)[-1] if (executed or arb_actions) else None,
        "strategy": "edge",
        "trades": arb_actions + executed,
        "arb_actions": arb_actions,
        "orders_placed": len(arb_actions) + len(executed),
        "rejected_signals": rejected,
        "exits": exits,
        "scan_counts": {
            "raw_markets": len(markets),
            "arb_opps": len(arb_actions) // 2,
            "crypto": len(crypto_signals),
            "near_cert": len(near_signals),
            "scalp": len(scalp_signals),
            "spot_quotes": len(spot_quotes),
        },
        "summary": portfolio.summary(),
    }


def edge_loop(settings: Settings) -> None:
    """Run :func:`edge_once` on the standard tick cadence."""
    from .main import strategy_loop

    strategy_loop(settings, "edge", edge_once)
