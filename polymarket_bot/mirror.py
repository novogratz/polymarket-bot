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
import time
from pathlib import Path
from typing import Any

from ._atomic_io import atomic_write_text
from .config import Settings
from .gamma import GammaClient
from .models import Candidate
from .portfolio import Portfolio
from .smart_money import DataApiClient, SmartTrade
from .strategy import build_pricing_candidates
from .trading import build_client, execute_live_sell, execute_live_trade
from .wallet_resolver import resolve_all


_MAX_SEEN = 2000


def _load_state(path: Path) -> dict[str, Any]:
    empty: dict[str, Any] = {"seen": [], "last_ts_by_target": {}, "legacy_last_ts": 0}
    if not path.is_file():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return empty
    if not isinstance(data, dict):
        return empty
    seen = data.get("seen", [])
    last_ts_raw = data.get("last_ts_by_target", {})
    last_ts_by_target: dict[str, int] = {}
    if isinstance(last_ts_raw, dict):
        for key, value in last_ts_raw.items():
            try:
                last_ts_by_target[str(key).lower()] = int(value)
            except (TypeError, ValueError):
                continue
    return {
        "seen": [str(k) for k in seen][-_MAX_SEEN:] if isinstance(seen, list) else [],
        "last_ts_by_target": last_ts_by_target,
        # Legacy single-target state: pre-multi-target ledgers stored a global
        # ``last_ts``. Used as a per-target fallback so the first multi-target
        # tick doesn't replay every historical trade.
        "legacy_last_ts": int(data.get("last_ts") or 0),
    }


def _save_state(path: Path, state: dict[str, Any]) -> None:
    payload = {
        "seen": list(state.get("seen", []))[-_MAX_SEEN:],
        "last_ts_by_target": {
            str(k).lower(): int(v)
            for k, v in (state.get("last_ts_by_target") or {}).items()
        },
    }
    atomic_write_text(path, json.dumps(payload, indent=2))


def _last_ts_for(state: dict[str, Any], target: str) -> int:
    return int(
        state.get("last_ts_by_target", {}).get(target, state.get("legacy_last_ts", 0))
        or 0
    )


def _parse_targets(raw: str) -> list[str]:
    """Parse a CSV (or single address) into a normalized, lowercased, deduped list."""
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for chunk in raw.split(","):
        addr = chunk.strip().lower()
        if not addr or addr in seen:
            continue
        seen.add(addr)
        out.append(addr)
    return out


def _resolver_cache_path(settings: Settings) -> Path:
    """Per-ledger cache for username/URL → address resolution."""
    return settings.mirror_state_path.parent / "wallet_resolutions.json"


def _resolved_targets(settings: Settings, *, verbose: bool = True) -> list[str]:
    """Parse + resolve ``settings.mirror_target`` into proxy-wallet addresses.

    Accepts a CSV mixing ``0x`` addresses, ``polymarket.com/profile/0x…``
    URLs, and usernames. Username resolutions are cached on disk so this
    is cheap on subsequent calls.
    """
    resolved, unresolved = resolve_all(
        settings.mirror_target,
        cache_path=_resolver_cache_path(settings),
        verbose=verbose,
    )
    if unresolved and verbose:
        print(
            f"[mirror] {len(unresolved)} unresolved target(s): "
            f"{', '.join(unresolved[:5])}",
            file=sys.stderr,
            flush=True,
        )
    return resolved


def _trade_key(target: str, trade: SmartTrade) -> str:
    return f"{target}:{trade.asset}:{trade.side.upper()}:{trade.timestamp}"


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


def _humanize_age(seconds: float) -> str:
    """Compact relative time format (``2m``, ``1.5h``, ``3d``)."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def format_recent_trades(
    trades: list[SmartTrade],
    *,
    now_ts: int,
    limit: int = 20,
    title_width: int = 50,
) -> list[str]:
    """Format the most recent target-wallet trades as aligned rows.

    Returned as a list of strings so the caller can print or test them.
    Most recent first.
    """
    if not trades:
        return ["(no recent trades found for this wallet)"]
    recent = sorted(trades, key=lambda t: t.timestamp, reverse=True)[:limit]
    rows = [
        f"  {'AGE':>6s}  {'SIDE':4s}  {'STAKE':>10s}  {'PRICE':>6s}  {'OUTCOME':10s}  TITLE"
    ]
    for trade in recent:
        age = _humanize_age(max(0, now_ts - int(trade.timestamp)))
        side = (trade.side or "?").upper()
        stake = f"${trade.usdc_size:,.0f}"
        price = f"{trade.price:.3f}"
        outcome = (trade.outcome or "")[:10]
        title = (trade.title or "")[:title_width]
        rows.append(
            f"  {age:>6s}  {side:4s}  {stake:>10s}  {price:>6s}  {outcome:10s}  {title}"
        )
    return rows


def print_recent_trades_snapshot(
    target: str,
    api: DataApiClient | None = None,
    *,
    limit: int = 20,
) -> None:
    """Fetch + pretty-print the most recent trades for ``target`` (best effort)."""
    if not target:
        return
    api = api or DataApiClient()
    trades = _fetch_target_trades(api, target)
    short = _short_target(target)
    print(f"\n📜 Recent trades for {short}  (limit={limit})", flush=True)
    now_ts = int(time.time())
    for line in format_recent_trades(trades, now_ts=now_ts, limit=limit):
        print(line, flush=True)
    print("", flush=True)


def _passes_buy_filters(trade: SmartTrade, settings: Settings) -> bool:
    return settings.mirror_min_buy_price <= trade.price <= settings.mirror_max_buy_price


def _select_eligible(
    trades: list[SmartTrade],
    *,
    target: str,
    last_ts: int,
    seen: set[str],
    settings: Settings,
    now_ts: int | None = None,
) -> list[SmartTrade]:
    eligible: list[SmartTrade] = []
    max_age = max(0, int(settings.mirror_max_trade_age_seconds))
    cutoff_ts = (now_ts if now_ts is not None else int(time.time())) - max_age if max_age else 0
    for trade in trades:
        if trade.timestamp <= last_ts:
            continue
        if max_age and trade.timestamp < cutoff_ts:
            continue
        if _trade_key(target, trade) in seen:
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
    if portfolio.has_open_position(candidate.market_id, candidate.outcome):
        return {
            "action": "skip",
            "reason": "duplicate_open_position",
            "token_id": trade.asset,
        }
    if portfolio.has_open_event_position(candidate):
        return {
            "action": "skip",
            "reason": "duplicate_open_event",
            "token_id": trade.asset,
        }
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
    """One tick of the mirror loop. Returns the standard tick payload.

    Polls every configured target wallet (CSV in ``settings.mirror_target``),
    merges their new trades into a single chronological queue, and mirrors
    each one in order.
    """
    targets = _resolved_targets(settings, verbose=False)
    if not targets:
        print("[mirror] POLYMARKET_MIRROR_TARGET is empty — nothing to mirror", flush=True)
        return {
            "summary": {"equity": 0.0, "cash": 0.0, "open_positions": 0},
            "actions": [],
            "scan_counts": {"polled": 0, "eligible": 0, "mirrored": 0, "skipped": 0},
            "targets": [],
        }

    portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)
    state = _load_state(settings.mirror_state_path)
    seen: set[str] = set(state["seen"])
    last_ts_by_target: dict[str, int] = dict(state["last_ts_by_target"])

    print(
        f"▶  mirror tick — {len(targets)} target(s): "
        f"{', '.join(_short_target(t) for t in targets)}",
        flush=True,
    )

    api = DataApiClient(settings.data_api_base_url)
    now_ts = int(time.time())
    all_eligible: list[tuple[str, SmartTrade]] = []
    polled = 0
    for target in targets:
        target_trades = _fetch_target_trades(api, target)
        polled += len(target_trades)
        last_ts = _last_ts_for(state, target)
        for trade in _select_eligible(
            target_trades, target=target, last_ts=last_ts, seen=seen, settings=settings, now_ts=now_ts
        ):
            all_eligible.append((target, trade))

    # Mirror chronologically: oldest eligible trade first, so SELL after BUY
    # for the same token gets the right order.
    all_eligible.sort(key=lambda pair: pair[1].timestamp)
    print(f"   polled={polled} eligible={len(all_eligible)}", flush=True)

    client = build_client(settings)
    gamma = GammaClient(settings.gamma_base_url)

    actions: list[dict[str, Any]] = []
    mirrored = 0
    skipped = 0

    for target, trade in all_eligible:
        last_ts_by_target[target] = max(
            last_ts_by_target.get(target, 0), trade.timestamp
        )
        seen.add(_trade_key(target, trade))
        target_short = _short_target(target)

        candidate = _candidate_for_token(trade.asset, gamma)
        if candidate is None:
            actions.append(
                {
                    "action": "skip",
                    "reason": "no_market",
                    "token_id": trade.asset,
                    "side": trade.side,
                    "target": target_short,
                }
            )
            skipped += 1
            continue

        side = trade.side.upper()
        if side == "BUY":
            result = _mirror_buy(client, settings, portfolio, candidate, trade, target_short)
        elif side == "SELL" and settings.mirror_mirror_sells:
            result = _mirror_sell(client, settings, portfolio, candidate, trade)
            result["target"] = target_short
        else:
            result = {
                "action": "skip",
                "reason": "sells_disabled",
                "token_id": trade.asset,
                "target": target_short,
            }

        result.setdefault("target", target_short)
        actions.append(result)
        if result["action"] in ("buy", "sell"):
            mirrored += 1
        else:
            skipped += 1

    state["last_ts_by_target"] = last_ts_by_target
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
            "eligible": len(all_eligible),
            "mirrored": mirrored,
            "skipped": skipped,
        },
        "targets": [_short_target(t) for t in targets],
    }


def mirror_loop(settings: Settings) -> None:
    """Run ``mirror_once`` on the standard ``strategy_loop`` cadence.

    Prints a snapshot of the target wallet's most recent trades before
    entering the loop, so the operator sees what kind of activity is
    about to be mirrored.
    """
    from .main import strategy_loop  # lazy import: main imports mirror

    targets = _resolved_targets(settings, verbose=True)
    if targets:
        try:
            api = DataApiClient(settings.data_api_base_url)
            for target in targets:
                try:
                    print_recent_trades_snapshot(target, api)
                except Exception as exc:
                    print(
                        f"[mirror] snapshot failed for {_short_target(target)}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
        except Exception as exc:
            print(f"[mirror] snapshot init failed: {exc}", file=sys.stderr, flush=True)

    strategy_loop(settings, "mirror", mirror_once)
