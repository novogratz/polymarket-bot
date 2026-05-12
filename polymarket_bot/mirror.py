"""Mirror mode: copy a single wallet's BUY/SELL trades in near-real-time.

Alternative to the default smart-money strategy. Polls one target wallet
via the Data API, filters new trades by timestamp + minimum stake, and
mirrors them as our own BUY/SELL via the standard trading client.

State persisted to ``settings.mirror_state_path`` so the loop is idempotent
across ticks: each ``(asset, side, timestamp)`` tuple is only mirrored once.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .config import Settings
from .gamma import GammaClient
from .models import Candidate
from .portfolio import Portfolio
from .smart_money import DataApiClient, SmartTrade
from .strategy import build_pricing_candidates
from .trading import build_client, execute_live_sell, execute_live_trade


_MAX_SEEN = 2000


def _load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"seen": [], "last_ts": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"seen": [], "last_ts": 0}
    if not isinstance(data, dict):
        return {"seen": [], "last_ts": 0}
    seen = data.get("seen", [])
    return {
        "seen": [str(k) for k in seen][-_MAX_SEEN:] if isinstance(seen, list) else [],
        "last_ts": int(data.get("last_ts") or 0),
    }


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "seen": list(state.get("seen", []))[-_MAX_SEEN:],
        "last_ts": int(state.get("last_ts") or 0),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _trade_key(trade: SmartTrade) -> str:
    return f"{trade.asset}:{trade.side.upper()}:{trade.timestamp}"


def _candidate_for_token(token_id: str, gamma: GammaClient) -> Candidate | None:
    """Reverse-lookup a market by CLOB token id and return the matching candidate."""
    try:
        markets = gamma.get_markets_by_clob_token_ids([token_id])
    except Exception as exc:
        print(f"[mirror] gamma lookup failed for {token_id[:8]}…: {exc}", file=sys.stderr, flush=True)
        return None
    if not markets:
        return None
    for candidate in build_pricing_candidates(markets):
        if candidate.token_id == token_id:
            return candidate
    return None


def _fetch_target_trades(api: DataApiClient, target: str) -> list[SmartTrade]:
    """Pull the latest trades for the target wallet (both BUY and SELL)."""
    try:
        trades = api.trades(user=target, start=0, limit=100, side=None)
    except Exception as exc:
        print(f"[mirror] trades fetch failed: {exc}", file=sys.stderr, flush=True)
        return []
    return sorted(trades, key=lambda t: t.timestamp)


def _passes_buy_filters(trade: SmartTrade, settings: Settings) -> bool:
    return settings.mirror_min_buy_price <= trade.price <= settings.mirror_max_buy_price


def _select_eligible(
    trades: list[SmartTrade],
    *,
    last_ts: int,
    seen: set[str],
    settings: Settings,
) -> list[SmartTrade]:
    eligible: list[SmartTrade] = []
    for trade in trades:
        if trade.timestamp <= last_ts:
            continue
        if _trade_key(trade) in seen:
            continue
        if trade.usdc_size < settings.mirror_min_target_stake_usd:
            continue
        side = trade.side.upper()
        if side == "BUY" and not _passes_buy_filters(trade, settings):
            continue
        if side not in ("BUY", "SELL"):
            continue
        eligible.append(trade)
    return eligible


def _mirror_buy(
    client: Any,
    settings: Settings,
    portfolio: Portfolio,
    candidate: Candidate,
    trade: SmartTrade,
    target_short: str,
) -> dict[str, Any]:
    if candidate.best_ask and trade.price > 0:
        premium = (candidate.best_ask - trade.price) / trade.price
        if premium > settings.mirror_max_chase_premium:
            return {
                "action": "skip",
                "reason": "chase_premium",
                "token_id": trade.asset,
                "premium": round(premium, 4),
            }
    try:
        execute_live_trade(
            client,
            settings,
            candidate,
            portfolio,
            min_trade_usd=1.0,
            max_trade_usd=settings.mirror_size_usd,
            strategy="mirror",
            signal={"tag": "mirror", "target": target_short},
        )
    except Exception as exc:
        return {
            "action": "skip",
            "reason": f"buy_failed:{type(exc).__name__}",
            "token_id": trade.asset,
            "message": str(exc)[:200],
        }
    return {
        "action": "buy",
        "token_id": trade.asset,
        "size_usd": settings.mirror_size_usd,
        "price": candidate.best_ask,
    }


def _mirror_sell(
    client: Any,
    settings: Settings,
    portfolio: Portfolio,
    candidate: Candidate,
    trade: SmartTrade,
) -> dict[str, Any]:
    position = next(
        (
            p
            for p in portfolio.positions
            if p.get("status") == "open" and str(p.get("token_id")) == trade.asset
        ),
        None,
    )
    if position is None:
        return {"action": "skip", "reason": "no_open_position", "token_id": trade.asset}
    shares = float(position.get("shares", 0) or 0)
    if shares <= 0:
        return {"action": "skip", "reason": "no_shares", "token_id": trade.asset}
    try:
        execute_live_sell(
            client,
            settings,
            candidate,
            portfolio,
            position,
            shares=shares,
            reason="mirror_sell",
        )
    except Exception as exc:
        return {
            "action": "skip",
            "reason": f"sell_failed:{type(exc).__name__}",
            "token_id": trade.asset,
            "message": str(exc)[:200],
        }
    return {
        "action": "sell",
        "token_id": trade.asset,
        "shares": shares,
        "price": candidate.best_bid,
    }


def _short_target(target: str) -> str:
    if len(target) < 10:
        return target
    return f"{target[:6]}…{target[-4:]}"


def mirror_once(settings: Settings) -> dict[str, Any]:
    """One tick of the mirror loop. Returns the standard tick payload."""
    target = settings.mirror_target.strip().lower()
    if not target:
        print("[mirror] POLYMARKET_MIRROR_TARGET is empty — nothing to mirror", flush=True)
        return {
            "summary": {"equity": 0.0, "cash": 0.0, "open_positions": 0},
            "actions": [],
            "scan_counts": {"polled": 0, "eligible": 0, "mirrored": 0, "skipped": 0},
        }

    portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)
    state = _load_state(settings.mirror_state_path)
    seen: set[str] = set(state["seen"])

    print(f"▶  mirror tick — target={_short_target(target)}", flush=True)

    api = DataApiClient(settings.data_api_base_url)
    trades = _fetch_target_trades(api, target)
    polled = len(trades)

    eligible = _select_eligible(
        trades, last_ts=state["last_ts"], seen=seen, settings=settings
    )
    print(f"   polled={polled} eligible={len(eligible)}", flush=True)

    client = build_client(settings)
    gamma = GammaClient(settings.gamma_base_url)
    target_short = _short_target(target)

    actions: list[dict[str, Any]] = []
    mirrored = 0
    skipped = 0
    max_ts = int(state["last_ts"])

    for trade in eligible:
        max_ts = max(max_ts, trade.timestamp)
        seen.add(_trade_key(trade))

        candidate = _candidate_for_token(trade.asset, gamma)
        if candidate is None:
            actions.append(
                {
                    "action": "skip",
                    "reason": "no_market",
                    "token_id": trade.asset,
                    "side": trade.side,
                }
            )
            skipped += 1
            continue

        side = trade.side.upper()
        if side == "BUY":
            result = _mirror_buy(client, settings, portfolio, candidate, trade, target_short)
        elif side == "SELL" and settings.mirror_mirror_sells:
            result = _mirror_sell(client, settings, portfolio, candidate, trade)
        else:
            result = {"action": "skip", "reason": "sells_disabled", "token_id": trade.asset}

        actions.append(result)
        if result["action"] in ("buy", "sell"):
            mirrored += 1
        else:
            skipped += 1

    state["last_ts"] = max_ts
    state["seen"] = list(seen)
    _save_state(settings.mirror_state_path, state)

    try:
        portfolio.save(settings.state_path)
    except Exception as exc:
        print(f"[mirror] portfolio save failed: {exc}", file=sys.stderr, flush=True)

    summary = portfolio.summary()
    return {
        "summary": {
            "equity": float(summary.get("equity", 0.0) or 0.0),
            "cash": float(summary.get("cash", 0.0) or 0.0),
            "open_positions": int(summary.get("open_positions", 0) or 0),
        },
        "actions": actions,
        "scan_counts": {
            "polled": polled,
            "eligible": len(eligible),
            "mirrored": mirrored,
            "skipped": skipped,
        },
    }


def mirror_loop(settings: Settings) -> None:
    """Run ``mirror_once`` on the standard ``strategy_loop`` cadence."""
    from .main import strategy_loop  # lazy import: main imports mirror

    strategy_loop(settings, "mirror", mirror_once)
