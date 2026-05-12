"""Pricing-refresh helper.

Shared utility that augments a scan-derived candidate list with up-to-date
pricing for every open position so that ``mark_to_market`` and exit
detection cover them — even when the token has drifted out of the Gamma
scan AND when the Gamma snapshot is stale.

Architecture:

1. CLOB midpoint + L1 bid/ask are the authority (no cache, live). We hit
   ``/midpoints`` and ``/prices`` in two batch calls covering every open
   position. The returned ``Candidate`` instances override the Gamma
   prices in ``candidates`` because ``mark_to_market``'s ``by_token``
   lookup keeps the last entry for a given token.
2. Fallback Gamma — for tokens the CLOB couldn't price (rare, e.g.
   illiquid markets with no orderbook) we still try the Gamma scan via
   ``get_markets_by_clob_token_ids``. This preserves the previous
   behaviour for edge cases.
3. The extra candidates are built with score 0.0 and no horizon /
   liquidity filter — they exist purely to keep prices fresh, never to
   seed new entries. Callers must still filter their entry pool from
   the original ``candidates`` list, not from this superset.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BookParams

from .config import Settings
from .gamma import GammaClient
from .models import Candidate, parse_dt
from .portfolio import Portfolio
from .strategy import build_pricing_candidates


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _position_tick_size(position: dict[str, Any], scan: Candidate | None) -> float:
    if scan and scan.tick_size and scan.tick_size > 0:
        return scan.tick_size
    stored = _safe_float(position.get("tick_size"))
    if stored and stored > 0:
        return stored
    return 0.01


def _position_neg_risk(position: dict[str, Any], scan: Candidate | None) -> bool:
    if scan and scan.neg_risk:
        return True
    return bool(position.get("neg_risk"))


def _fetch_clob_quotes(
    settings: Settings, token_ids: list[str]
) -> tuple[dict[str, float], dict[str, tuple[float | None, float | None]]]:
    """Return ``(midpoints, bid_ask)`` keyed by token_id.

    Two batch POSTs to the public CLOB endpoints: ``/midpoints`` and
    ``/prices`` (the latter with ``BUY``+``SELL`` per token to cover both
    sides of the book). No authentication required.

    On failure the partial result is still returned (best-effort).
    """
    if not token_ids:
        return {}, {}
    client = ClobClient(settings.clob_base_url)
    midpoints: dict[str, float] = {}
    bid_ask: dict[str, tuple[float | None, float | None]] = {}
    try:
        mids_raw = client.get_midpoints(params=[BookParams(token_id=tok) for tok in token_ids])
        for tok, mid in (mids_raw or {}).items():
            f = _safe_float(mid)
            if f is not None:
                midpoints[str(tok)] = f
    except Exception as exc:
        print(f"   pricing-refresh: CLOB midpoints failed: {type(exc).__name__}: {exc}")
    try:
        prices_raw = client.get_prices(
            params=[
                p
                for tok in token_ids
                for p in (BookParams(token_id=tok, side="BUY"), BookParams(token_id=tok, side="SELL"))
            ]
        )
        for tok, sides in (prices_raw or {}).items():
            if not isinstance(sides, dict):
                continue
            bid = _safe_float(sides.get("BUY"))
            ask = _safe_float(sides.get("SELL"))
            bid_ask[str(tok)] = (bid, ask)
    except Exception as exc:
        print(f"   pricing-refresh: CLOB prices failed: {type(exc).__name__}: {exc}")
    return midpoints, bid_ask


def _build_clob_candidates(
    portfolio: Portfolio,
    midpoints: dict[str, float],
    bid_ask: dict[str, tuple[float | None, float | None]],
    scan_by_token: dict[str, Candidate] | None = None,
) -> list[Candidate]:
    """Build pricing-only ``Candidate``s from CLOB midpoint + bid/ask.

    Outcome / market_id / slug / end_date are pulled from the open
    position itself — the CLOB endpoints don't return that metadata.
    Only positions for which we have a midpoint are emitted.

    ``scan_by_token`` provides per-token metadata that the CLOB
    endpoints don't expose (``tick_size``, ``neg_risk``). When a
    matching scan candidate exists we inherit those fields so
    downstream sells aren't blocked by a missing tick_size.
    """
    out: list[Candidate] = []
    scan_by_token = scan_by_token or {}
    for position in portfolio.positions:
        if position.get("status") != "open":
            continue
        token = position.get("token_id")
        if not token:
            continue
        tok_str = str(token)
        if tok_str not in midpoints:
            continue
        price = midpoints[tok_str]
        if price <= 0.0 or price >= 1.0:
            continue
        bid, ask = bid_ask.get(tok_str, (None, None))
        end_date: datetime | None = parse_dt(str(position.get("end_date") or "")) if position.get("end_date") else None
        scan = scan_by_token.get(tok_str)
        tick_size = _position_tick_size(position, scan)
        neg_risk = _position_neg_risk(position, scan)
        out.append(
            Candidate(
                market_id=str(position.get("market_id") or ""),
                question=str(position.get("question") or ""),
                slug=str(position.get("slug") or ""),
                end_date=end_date,
                hours_to_close=0.0,
                liquidity=0.0,
                volume=0.0,
                outcome=str(position.get("outcome") or ""),
                price=price,
                token_id=tok_str,
                score=0.0,
                url=str(position.get("url") or "https://polymarket.com"),
                best_bid=bid,
                best_ask=ask,
                tick_size=tick_size,
                neg_risk=neg_risk,
                accepts_orders=False,
                event_slug=str(position.get("event_slug") or ""),
            )
        )
    return out


def ensure_open_positions_in_pool(
    settings: Settings,
    portfolio: Portfolio,
    candidates: list,
) -> list:
    """Return a superset of ``candidates`` covering every open position.

    Primary source : CLOB midpoint + prices (live, no cache).
    Fallback     : Gamma reverse-lookup for any token the CLOB couldn't
    price (rare).

    Callers must still build their entry-eligibility pool from the
    original ``candidates`` list, NOT from this superset — the pricing
    candidates appended here have ``score=0.0``, ``accepts_orders=False``
    and missing liquidity/volume, so they shouldn't be ranked.
    """
    open_tokens = sorted({
        str(position["token_id"])
        for position in portfolio.positions
        if position.get("status") == "open" and position.get("token_id")
    })
    if not open_tokens:
        return list(candidates)

    scan_by_token = {c.token_id: c for c in candidates if c.token_id}
    midpoints, bid_ask = _fetch_clob_quotes(settings, open_tokens)
    pricing = _build_clob_candidates(portfolio, midpoints, bid_ask, scan_by_token=scan_by_token)
    priced_tokens = {c.token_id for c in pricing if c.token_id}

    # Fallback Gamma for tokens the CLOB couldn't price.
    missing = sorted(set(open_tokens) - priced_tokens)
    scan_tokens = {c.token_id for c in candidates if c.token_id}
    missing = [t for t in missing if t not in scan_tokens]
    if missing:
        try:
            extra_markets = GammaClient(settings.gamma_base_url).get_markets_by_clob_token_ids(missing)
            pricing.extend(build_pricing_candidates(extra_markets))
        except Exception as exc:
            print(f"   pricing-refresh fallback failed: {type(exc).__name__}: {exc}")

    # Append AFTER the original candidates so by_token lookup wins on us
    # for held positions (CLOB live > Gamma scan cache).
    return list(candidates) + pricing
