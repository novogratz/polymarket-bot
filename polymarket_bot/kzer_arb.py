"""kzerlepgm_ultimatestrategy — structural arb + Kelly directional.

Two lanes operating on ≤4h binary markets:

  Lane A — Paired arbitrage. When ask_YES + ask_NO < 1.0, buy both legs
  for guaranteed (1 - pair_ask) at resolution. Rare on Polymarket binary
  markets but still scanned every tick.

  Lane B — Kelly-sized directional on near-arbs. When pair_ask is tight
  (1.00 ≤ sum ≤ kzer_near_arb_ceiling) AND one side is a clear favorite
  (price ≥ kzer_favorite_min), assume the favorite is mildly underpriced
  (bias_multiplier on the bid) and bet ¼-Kelly on it.

DRY-RUN: positions opened via Portfolio.open_paper_position.
LIVE: not yet wired — raises NotImplementedError.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from .config import Settings
from .models import Candidate, as_float, parse_dt, parse_json_list, utc_now
from .portfolio import Portfolio


KZER_PAIR_ARB_CEILING = 1.00          # Lane A: true arb requires sum < this.
KZER_MIN_ARB_EDGE = 0.005             # 0.5% minimum edge after rounding.
KZER_NEAR_ARB_CEILING = 1.03          # Lane B: pair_ask must be ≤ this.
KZER_FAVORITE_MIN_PRICE = 0.65        # Lane B: favorite leg must be ≥ this.
KZER_BIAS_MULTIPLIER = 1.05           # Assume favorites are 5% underpriced.
KZER_KELLY_FRACTION = 0.25            # ¼ Kelly.
KZER_PRICE_BATCH = 100


def _mark_open_positions_to_market(settings: Settings, portfolio: Portfolio) -> None:
    """Refresh current_price + unrealized_pnl on all open kzer positions.

    Without this, positions stay frozen at entry_price forever and the
    leaderboard equity never moves.
    """
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import BookParams

    open_positions = [
        p for p in portfolio.positions
        if p.get("status") == "open"
        and str(p.get("strategy") or "") == "kzerlepgm_ultimatestrategy"
        and p.get("token_id")
    ]
    if not open_positions:
        return
    token_ids = list({str(p["token_id"]) for p in open_positions})
    client = ClobClient(settings.clob_base_url)

    mids: dict[str, float] = {}
    for i in range(0, len(token_ids), KZER_PRICE_BATCH):
        chunk = token_ids[i : i + KZER_PRICE_BATCH]
        try:
            mids_raw = client.get_midpoints(
                params=[BookParams(token_id=t) for t in chunk]
            )
        except Exception as exc:
            print(
                f"   [kzer] midpoints chunk {i // KZER_PRICE_BATCH + 1} failed: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            continue
        for tok, mid in (mids_raw or {}).items():
            v = _safe_float(mid)
            if v is not None:
                mids[str(tok)] = v

    candidates: list[Candidate] = []
    for p in open_positions:
        tok = str(p["token_id"])
        if tok not in mids:
            continue
        candidates.append(
            Candidate(
                market_id=str(p.get("market_id") or ""),
                question=str(p.get("question") or ""),
                slug=str(p.get("slug") or ""),
                end_date=parse_dt(str(p.get("end_date") or "")) if p.get("end_date") else None,
                hours_to_close=None,
                liquidity=0.0,
                volume=0.0,
                outcome=str(p.get("outcome") or ""),
                price=mids[tok],
                token_id=tok,
                score=0.0,
                url=str(p.get("url") or "https://polymarket.com"),
                best_bid=None,
                best_ask=None,
                tick_size=float(p.get("tick_size") or 0.01),
                neg_risk=bool(p.get("neg_risk", False)),
                accepts_orders=True,
                event_slug=str(p.get("event_slug") or ""),
            )
        )
    portfolio.mark_to_market(candidates)


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _fetch_paired_quotes(
    settings: Settings, token_pairs: list[tuple[str, str]]
) -> dict[tuple[str, str], dict[str, float | None]]:
    """Chunked CLOB fetch: BUY-side (= ask) and SELL-side (= bid) per token.

    Returns ``{(yes, no): {"ask_yes":, "ask_no":, "bid_yes":, "bid_no":}}``
    for pairs where all four prices came back.
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
    quotes: dict[str, tuple[float | None, float | None]] = {}
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
            # CLOB convention: side="BUY" query returns the best BID (price
            # someone is offering to buy at), side="SELL" returns the best
            # ASK (price someone is offering to sell at). Same mapping as
            # pricing._fetch_clob_quotes.
            bid = _safe_float(sides.get("BUY"))
            ask = _safe_float(sides.get("SELL"))
            quotes[str(tok)] = (bid, ask)

    out: dict[tuple[str, str], dict[str, float | None]] = {}
    for yes, no in token_pairs:
        y = quotes.get(yes)
        n = quotes.get(no)
        if not y or not n:
            continue
        bid_y, ask_y = y
        bid_n, ask_n = n
        if ask_y is None or ask_n is None or ask_y <= 0 or ask_n <= 0:
            continue
        out[(yes, no)] = {
            "ask_yes": ask_y,
            "ask_no": ask_n,
            "bid_yes": bid_y,
            "bid_no": bid_n,
        }
    return out


def _discover_pairs(settings: Settings) -> list[dict[str, Any]]:
    """Scan ≤4h binary markets and return their token-pair metadata."""
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


def _make_leg_candidate(meta: dict[str, Any], side: str, ask: float, bid: float | None) -> Candidate:
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
        best_bid=bid,
        best_ask=ask,
        tick_size=meta.get("tick_size") or 0.01,
        neg_risk=meta.get("neg_risk", False),
        accepts_orders=True,
        event_slug=meta["slug"],
    )


def _arb_stake(equity: float, edge: float, settings: Settings) -> float:
    if edge <= 0 or equity <= 0:
        return 0.0
    fraction = KZER_KELLY_FRACTION * edge
    return min(equity * fraction, settings.race_stake_usd)


def _kelly_directional_stake(equity: float, ask: float, fair_prob: float, settings: Settings) -> float:
    """Fractional Kelly for a directional bet at ``ask`` with assumed prob ``fair_prob``."""
    if equity <= 0 or ask <= 0 or ask >= 1.0 or fair_prob <= ask:
        return 0.0
    b = (1.0 - ask) / ask                  # net odds
    p = fair_prob
    q = 1.0 - p
    full_kelly = (b * p - q) / b           # standard Kelly
    if full_kelly <= 0:
        return 0.0
    raw = equity * full_kelly * KZER_KELLY_FRACTION
    return min(raw, settings.race_stake_usd)


def kzer_once(settings: Settings) -> dict[str, Any]:
    if not settings.dry_run:
        raise NotImplementedError(
            "kzerlepgm_ultimatestrategy live execution is not wired yet. "
            "Run in --dry-run mode for validation first."
        )

    portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)
    _mark_open_positions_to_market(settings, portfolio)
    pairs = _discover_pairs(settings)
    if not pairs:
        portfolio.save(settings.state_path)
        return {
            "strategy": "kzerlepgm_ultimatestrategy",
            "scanned_pairs": 0,
            "arb_opps": 0,
            "directional_opps": 0,
            "trades": 0,
            "summary": portfolio.summary(),
        }

    token_pairs = [(p["yes_token"], p["no_token"]) for p in pairs]
    quotes = _fetch_paired_quotes(settings, token_pairs)

    summary = portfolio.summary()
    equity = float(summary.get("equity", 0.0) or 0.0)
    cash = float(summary.get("cash", 0.0) or 0.0)
    cash_floor = equity * settings.race_cash_floor_pct if equity > 0 else 0.0
    opened_markets = {
        str(p.get("market_id")) for p in portfolio.positions if p.get("status") == "open"
    }

    arb_opps: list[dict[str, Any]] = []
    dir_opps: list[dict[str, Any]] = []
    for meta in pairs:
        q = quotes.get((meta["yes_token"], meta["no_token"]))
        if not q:
            continue
        ask_y, ask_n = q["ask_yes"], q["ask_no"]
        pair_ask = ask_y + ask_n

        if pair_ask < KZER_PAIR_ARB_CEILING and (1.0 - pair_ask) >= KZER_MIN_ARB_EDGE:
            arb_opps.append(
                {"meta": meta, "ask_yes": ask_y, "ask_no": ask_n,
                 "bid_yes": q["bid_yes"], "bid_no": q["bid_no"],
                 "pair_ask": pair_ask, "edge": 1.0 - pair_ask}
            )
            continue

        if pair_ask <= KZER_NEAR_ARB_CEILING:
            # Pick the favorite side (higher ask = market thinks it's more likely).
            if ask_y >= ask_n:
                fav_side, fav_ask, fav_bid = "yes", ask_y, q["bid_yes"]
            else:
                fav_side, fav_ask, fav_bid = "no", ask_n, q["bid_no"]
            if fav_ask < KZER_FAVORITE_MIN_PRICE:
                continue
            fair_prob = min(0.99, fav_ask * KZER_BIAS_MULTIPLIER)
            dir_opps.append(
                {"meta": meta, "side": fav_side, "ask": fav_ask, "bid": fav_bid,
                 "fair_prob": fair_prob, "pair_ask": pair_ask}
            )

    arb_opps.sort(key=lambda o: o["edge"], reverse=True)
    dir_opps.sort(key=lambda o: o["fair_prob"] - o["ask"], reverse=True)

    trades: list[dict[str, Any]] = []
    max_orders = settings.race_max_orders_per_tick

    # Lane A — paired arb.
    for opp in arb_opps:
        if len(trades) >= max_orders:
            break
        meta = opp["meta"]
        if str(meta["market_id"]) in opened_markets:
            continue
        total_stake = _arb_stake(equity, opp["edge"], settings)
        per_leg = total_stake / 2.0
        if per_leg < 1.0 or cash - total_stake < cash_floor:
            continue
        yes_c = _make_leg_candidate(meta, "yes", opp["ask_yes"], opp["bid_yes"])
        no_c = _make_leg_candidate(meta, "no", opp["ask_no"], opp["bid_no"])
        yp = portfolio.open_paper_position(yes_c, per_leg, entry_price=opp["ask_yes"])
        np_ = portfolio.open_paper_position(no_c, per_leg, entry_price=opp["ask_no"])
        if yp is None or np_ is None:
            continue
        for p in (yp, np_):
            p["strategy"] = "kzerlepgm_ultimatestrategy"
        cash = float(portfolio.cash)
        opened_markets.add(str(meta["market_id"]))
        trades.append({"lane": "arb", "market_id": meta["market_id"],
                       "edge": round(opp["edge"], 4), "stake": round(total_stake, 2)})
        print(
            f"🎯 [kzer arb] {meta['question'][:50]} "
            f"pair_ask={opp['pair_ask']:.3f} edge={opp['edge']:+.3f} stake=2×${per_leg:.2f}",
            flush=True,
        )

    # Lane B — Kelly directional.
    for opp in dir_opps:
        if len(trades) >= max_orders:
            break
        meta = opp["meta"]
        if str(meta["market_id"]) in opened_markets:
            continue
        stake = _kelly_directional_stake(equity, opp["ask"], opp["fair_prob"], settings)
        if stake < 1.0 or cash - stake < cash_floor:
            continue
        cand = _make_leg_candidate(meta, opp["side"], opp["ask"], opp["bid"])
        pos = portfolio.open_paper_position(cand, stake, entry_price=opp["ask"])
        if pos is None:
            continue
        pos["strategy"] = "kzerlepgm_ultimatestrategy"
        cash = float(portfolio.cash)
        opened_markets.add(str(meta["market_id"]))
        trades.append({"lane": "directional", "market_id": meta["market_id"],
                       "side": opp["side"], "ask": round(opp["ask"], 3),
                       "stake": round(stake, 2)})
        print(
            f"🎯 [kzer dir] {meta['question'][:50]} {opp['side'].upper()} "
            f"ask={opp['ask']:.3f} fair≈{opp['fair_prob']:.3f} stake=${stake:.2f}",
            flush=True,
        )

    portfolio.save(settings.state_path)
    print(
        f"[kzer] scanned {len(pairs)} pairs · arb_opps={len(arb_opps)} · "
        f"dir_opps={len(dir_opps)} · trades={len(trades)}",
        flush=True,
    )

    return {
        "strategy": "kzerlepgm_ultimatestrategy",
        "scanned_pairs": len(pairs),
        "arb_opps": len(arb_opps),
        "directional_opps": len(dir_opps),
        "trades": len(trades),
        "trade_details": trades,
        "summary": portfolio.summary(),
        "ts": utc_now().isoformat(),
    }


def kzer_loop(settings: Settings) -> None:
    from .main import strategy_loop

    strategy_loop(settings, "kzerlepgm_ultimatestrategy", kzer_once)
