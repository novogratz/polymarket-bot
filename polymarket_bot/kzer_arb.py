"""kzerlepgm_ultimatestrategy — structural YES/NO complement arbitrage.

Scans binary prediction markets expiring within the 4h race window and
identifies cases where the paired ask sum (best ask of YES token + best
ask of NO token, fetched independently from the CLOB) falls below 1.0.
Buying one share of each side at those asks locks in
``1 - (ask_YES + ask_NO)`` profit at resolution, minus fees.

Position sizing uses fractional Kelly. For a paired-arb leg the win
probability is effectively 1.0 (both legs combined always pay $1 on
resolution), so the Kelly fraction is bounded by the cash floor and the
per-strategy stake cap rather than by edge uncertainty.

DRY-RUN: paired positions are opened locally via Portfolio.open_paper_position.
LIVE: not yet wired — paired execution requires atomic buy-both-legs (or
partial-fill rollback), which the trading layer doesn't expose. The live
path raises so the strategy can be validated in dry-run first.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from .config import Settings
from .models import Candidate, as_float, parse_dt, parse_json_list, utc_now
from .portfolio import Portfolio


KZER_PAIR_ASK_CEILING = 0.97
KZER_KELLY_FRACTION = 0.25
KZER_MIN_EDGE = 0.01
KZER_PRICE_BATCH = 100


def _fetch_paired_asks(
    settings: Settings, token_pairs: list[tuple[str, str]]
) -> dict[tuple[str, str], tuple[float, float]]:
    """Batch-fetch BUY-side prices (asks) for every (YES, NO) token pair.

    Chunks the CLOB request to stay under Polymarket's payload limit
    (~150 tokens per batch). Returns ``{(yes, no): (ask_yes, ask_no)}``
    for pairs where both legs returned a price.
    """
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import BookParams

    token_ids: list[str] = []
    for yes, no in token_pairs:
        if yes:
            token_ids.append(yes)
        if no:
            token_ids.append(no)
    if not token_ids:
        return {}

    client = ClobClient(settings.clob_base_url)
    bid_ask: dict[str, tuple[float | None, float | None]] = {}
    for i in range(0, len(token_ids), KZER_PRICE_BATCH):
        chunk = token_ids[i : i + KZER_PRICE_BATCH]
        try:
            prices_raw = client.get_prices(
                params=[
                    p
                    for tok in chunk
                    for p in (
                        BookParams(token_id=tok, side="BUY"),
                        BookParams(token_id=tok, side="SELL"),
                    )
                ]
            )
        except Exception as exc:
            print(
                f"   [kzer] CLOB prices chunk {i // KZER_PRICE_BATCH + 1} failed: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            continue
        for tok, sides in (prices_raw or {}).items():
            if not isinstance(sides, dict):
                continue
            bid = _safe_float(sides.get("BUY"))
            ask = _safe_float(sides.get("SELL"))
            bid_ask[str(tok)] = (bid, ask)

    out: dict[tuple[str, str], tuple[float, float]] = {}
    for yes, no in token_pairs:
        yes_q = bid_ask.get(yes)
        no_q = bid_ask.get(no)
        if not yes_q or not no_q:
            continue
        ask_yes = yes_q[1]
        ask_no = no_q[1]
        if ask_yes is None or ask_no is None:
            continue
        if ask_yes <= 0 or ask_no <= 0:
            continue
        out[(yes, no)] = (ask_yes, ask_no)
    return out


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _discover_pairs(settings: Settings) -> list[dict[str, Any]]:
    """Scan ≤4h binary markets and return their token pair metadata."""
    from .race_strategies import _load_short_expiry_markets

    markets = _load_short_expiry_markets(settings)
    now = utc_now()
    earliest = now + timedelta(minutes=5)
    horizon = now + timedelta(hours=settings.race_max_hours)

    pairs: list[dict[str, Any]] = []
    for market in markets:
        if not bool(market.get("acceptingOrders")):
            continue
        end_date = parse_dt(market.get("endDate"))
        if end_date is None or end_date < earliest or end_date > horizon:
            continue
        token_ids = [str(item) for item in parse_json_list(market.get("clobTokenIds"))]
        outcomes = [str(item) for item in parse_json_list(market.get("outcomes"))]
        if len(token_ids) != 2 or len(outcomes) != 2:
            continue
        tick_size = as_float(market.get("orderPriceMinTickSize"), default=None)
        hours_to_close = max((end_date - now).total_seconds() / 3600.0, 0.0)
        slug = str(market.get("slug") or market.get("id") or "")
        market_id = str(market.get("id") or "")
        question = str(market.get("question") or "")
        url = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"
        pairs.append(
            {
                "market_id": market_id,
                "question": question,
                "slug": slug,
                "yes_token": token_ids[0],
                "no_token": token_ids[1],
                "yes_outcome": outcomes[0],
                "no_outcome": outcomes[1],
                "end_date": end_date,
                "hours_to_close": hours_to_close,
                "tick_size": tick_size,
                "url": url,
                "neg_risk": bool(market.get("negRisk")),
                "liquidity": as_float(market.get("liquidity") or market.get("liquidityNum")),
            }
        )
    return pairs


def _make_leg_candidate(meta: dict[str, Any], side: str, ask: float) -> Candidate:
    """Build a Candidate for one leg so Portfolio.open_paper_position works."""
    yes_side = side == "yes"
    return Candidate(
        market_id=meta["market_id"],
        question=meta["question"],
        slug=meta["slug"],
        end_date=meta["end_date"],
        hours_to_close=meta["hours_to_close"],
        liquidity=float(meta.get("liquidity") or 0.0),
        volume=0.0,
        outcome=meta["yes_outcome"] if yes_side else meta["no_outcome"],
        price=ask,
        token_id=meta["yes_token"] if yes_side else meta["no_token"],
        score=0.0,
        url=meta["url"],
        best_bid=None,
        best_ask=ask,
        tick_size=meta.get("tick_size") or 0.01,
        neg_risk=meta.get("neg_risk", False),
        accepts_orders=True,
        event_slug=meta["slug"],
    )


def _stake_for_pair(equity: float, edge: float, settings: Settings) -> float:
    """Total stake for one paired-arb opportunity (split equally across legs)."""
    if edge <= 0 or equity <= 0:
        return 0.0
    fraction = KZER_KELLY_FRACTION * edge
    raw = equity * fraction
    return min(raw, settings.race_stake_usd)


def kzer_once(settings: Settings) -> dict[str, Any]:
    """One scan/trade tick of the kzer arbitrage strategy."""
    if not settings.dry_run:
        raise NotImplementedError(
            "kzerlepgm_ultimatestrategy live execution is not wired yet. "
            "Run in --dry-run mode for validation first."
        )

    portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)
    pairs = _discover_pairs(settings)
    if not pairs:
        portfolio.save(settings.state_path)
        return {
            "strategy": "kzerlepgm_ultimatestrategy",
            "opportunities": 0,
            "trades": 0,
            "summary": portfolio.summary(),
        }

    token_pairs = [(p["yes_token"], p["no_token"]) for p in pairs]
    quotes = _fetch_paired_asks(settings, token_pairs)

    opportunities: list[dict[str, Any]] = []
    for meta in pairs:
        key = (meta["yes_token"], meta["no_token"])
        q = quotes.get(key)
        if not q:
            continue
        ask_yes, ask_no = q
        pair_ask = ask_yes + ask_no
        edge = 1.0 - pair_ask
        if pair_ask >= KZER_PAIR_ASK_CEILING:
            continue
        if edge < KZER_MIN_EDGE:
            continue
        opportunities.append(
            {
                "meta": meta,
                "ask_yes": ask_yes,
                "ask_no": ask_no,
                "pair_ask": pair_ask,
                "edge": edge,
            }
        )

    opportunities.sort(key=lambda o: o["edge"], reverse=True)
    capped = opportunities[: settings.race_max_orders_per_tick]

    summary = portfolio.summary()
    equity = float(summary.get("equity", 0.0) or 0.0)
    cash = float(summary.get("cash", 0.0) or 0.0)
    cash_floor = equity * settings.race_cash_floor_pct if equity > 0 else 0.0

    opened_markets: set[str] = {
        str(p.get("market_id"))
        for p in portfolio.positions
        if p.get("status") == "open"
    }

    trades: list[dict[str, Any]] = []
    for opp in capped:
        meta = opp["meta"]
        if str(meta["market_id"]) in opened_markets:
            continue
        total_stake = _stake_for_pair(equity, opp["edge"], settings)
        per_leg = total_stake / 2.0
        if per_leg < 1.0:
            continue
        if cash - total_stake < cash_floor:
            break

        yes_cand = _make_leg_candidate(meta, "yes", opp["ask_yes"])
        no_cand = _make_leg_candidate(meta, "no", opp["ask_no"])

        yes_pos = portfolio.open_paper_position(yes_cand, per_leg, entry_price=opp["ask_yes"])
        no_pos = portfolio.open_paper_position(no_cand, per_leg, entry_price=opp["ask_no"])
        if yes_pos is None or no_pos is None:
            continue
        for p in (yes_pos, no_pos):
            p["strategy"] = "kzerlepgm_ultimatestrategy"
        cash = float(portfolio.cash)
        trades.append(
            {
                "market_id": meta["market_id"],
                "question": meta["question"],
                "ask_yes": round(opp["ask_yes"], 4),
                "ask_no": round(opp["ask_no"], 4),
                "pair_ask": round(opp["pair_ask"], 4),
                "edge": round(opp["edge"], 4),
                "stake_each_leg": round(per_leg, 2),
            }
        )
        print(
            f"🎯 [kzer] paired arb: {meta['question'][:50]} "
            f"yes={opp['ask_yes']:.3f} no={opp['ask_no']:.3f} "
            f"edge={opp['edge']:+.3f} stake=2×${per_leg:.2f}",
            flush=True,
        )

    portfolio.save(settings.state_path)

    print(
        f"[kzer] scanned {len(pairs)} pairs, {len(opportunities)} arb opportunities "
        f"({len(trades)} paired trades opened)",
        flush=True,
    )

    return {
        "strategy": "kzerlepgm_ultimatestrategy",
        "scanned_pairs": len(pairs),
        "opportunities": len(opportunities),
        "trades": len(trades),
        "paired_trades": trades,
        "summary": portfolio.summary(),
        "ts": utc_now().isoformat(),
    }


def kzer_loop(settings: Settings) -> None:
    from .main import strategy_loop

    strategy_loop(settings, "kzerlepgm_ultimatestrategy", kzer_once)
