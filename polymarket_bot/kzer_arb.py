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

DRY-RUN ONLY. Live paired execution requires:
  - atomic buy of both legs (or partial-fill rollback)
  - per-pair cost basis tracking
  - resolution-time auto-redeem
None of those are wired up yet. The live path intentionally raises so
the strategy can be validated in dry-run before any real capital is
committed (see the user's own spec: "Start Small: Paper trade / small
capital validation → scale only after proven statistics").
"""

from __future__ import annotations

from typing import Any

from .config import Settings
from .models import Candidate, parse_json_list, utc_now


KZER_PAIR_ASK_CEILING = 0.97
KZER_KELLY_FRACTION = 0.25
KZER_MIN_EDGE = 0.01


def _fetch_paired_asks(
    settings: Settings, token_pairs: list[tuple[str, str]]
) -> dict[tuple[str, str], tuple[float, float]]:
    """Batch-fetch BUY-side prices (asks) for every (YES, NO) token pair.

    Returns ``{(yes_tok, no_tok): (ask_yes, ask_no)}`` for pairs where
    both legs returned a price.
    """
    from .pricing import _fetch_clob_quotes

    token_ids: list[str] = []
    for yes, no in token_pairs:
        if yes:
            token_ids.append(yes)
        if no:
            token_ids.append(no)
    if not token_ids:
        return {}
    _midpoints, bid_ask = _fetch_clob_quotes(settings, token_ids)
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


def _discover_pairs(settings: Settings) -> list[dict[str, Any]]:
    """Scan ≤4h binary markets and return their token pair metadata."""
    from .race_strategies import _load_short_expiry_markets

    markets = _load_short_expiry_markets(settings)
    pairs: list[dict[str, Any]] = []
    for market in markets:
        if not bool(market.get("acceptingOrders")):
            continue
        token_ids = [str(item) for item in parse_json_list(market.get("clobTokenIds"))]
        outcomes = [str(item) for item in parse_json_list(market.get("outcomes"))]
        if len(token_ids) != 2 or len(outcomes) != 2:
            continue
        pairs.append(
            {
                "market_id": str(market.get("id") or ""),
                "question": str(market.get("question") or ""),
                "slug": str(market.get("slug") or ""),
                "yes_token": token_ids[0],
                "no_token": token_ids[1],
                "yes_outcome": outcomes[0],
                "no_outcome": outcomes[1],
                "end_date": market.get("endDate"),
                "neg_risk": bool(market.get("negRisk")),
            }
        )
    return pairs


def _kelly_stake(equity: float, edge: float, settings: Settings) -> float:
    """Fractional Kelly bounded by per-tick stake cap and cash-floor."""
    if edge <= 0 or equity <= 0:
        return 0.0
    fraction = KZER_KELLY_FRACTION * edge
    raw = equity * fraction
    return min(raw, settings.race_stake_usd)


def kzer_once(settings: Settings) -> dict[str, Any]:
    """One scan/trade tick of the kzer arbitrage strategy.

    Returns a JSON-friendly summary for the journal/dashboard.
    """
    if not settings.dry_run:
        raise NotImplementedError(
            "kzerlepgm_ultimatestrategy live execution is not wired yet. "
            "Run in --dry-run mode for validation first."
        )

    pairs = _discover_pairs(settings)
    if not pairs:
        return {"strategy": "kzerlepgm_ultimatestrategy", "opportunities": 0, "trades": 0}

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
                "market_id": meta["market_id"],
                "question": meta["question"],
                "ask_yes": round(ask_yes, 4),
                "ask_no": round(ask_no, 4),
                "pair_ask": round(pair_ask, 4),
                "edge": round(edge, 4),
            }
        )

    opportunities.sort(key=lambda o: o["edge"], reverse=True)
    capped = opportunities[: settings.race_max_orders_per_tick]

    print(
        f"[kzer] scanned {len(pairs)} pairs, {len(opportunities)} arb opportunities "
        f"(top edge: {capped[0]['edge'] if capped else 0:.4f})",
        flush=True,
    )
    for opp in capped:
        print(
            f"   pair: {opp['question'][:60]} | ask_yes={opp['ask_yes']} ask_no={opp['ask_no']} "
            f"edge={opp['edge']:+.4f}",
            flush=True,
        )

    return {
        "strategy": "kzerlepgm_ultimatestrategy",
        "scanned_pairs": len(pairs),
        "opportunities": len(opportunities),
        "trades": len(capped),
        "top_opportunities": capped,
        "ts": utc_now().isoformat(),
    }


def kzer_loop(settings: Settings) -> None:
    from .main import strategy_loop

    strategy_loop(settings, "kzerlepgm_ultimatestrategy", kzer_once)
