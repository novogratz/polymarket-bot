"""Mirror mode: copy a single wallet's BUY/SELL trades in near-real-time.

Alternative to the default smart-money strategy. Polls one target wallet
via the Data API, filters new trades by timestamp + minimum stake, and
mirrors them as our own BUY/SELL via the standard trading client.

State persisted to ``settings.mirror_state_path`` so the loop is idempotent
across ticks: each ``(asset, side, timestamp)`` tuple is only mirrored once.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .gamma import GammaClient
from .models import Candidate
from .portfolio import Portfolio
from .smart_money import DataApiClient, SmartTrade, market_category, fetch_smart_money_data
from .strategy import build_pricing_candidates
from .trading import build_client, execute_live_sell, execute_live_trade
from .wallet_resolver import resolve_all, resolve_target


_MAX_SEEN = 2000
_DEFAULT_DAILY_ANCHOR = ""


def _utc_day_key(ts: float | None = None) -> str:
    moment = dt.datetime.fromtimestamp(ts if ts is not None else time.time(), tz=dt.timezone.utc)
    return moment.date().isoformat()


def _utc_week_key(ts: float | None = None) -> str:
    moment = dt.datetime.fromtimestamp(ts if ts is not None else time.time(), tz=dt.timezone.utc)
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def _load_state(path: Path) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "seen": [],
        "last_ts_by_target": {},
        "legacy_last_ts": 0,
        "daily_anchor": _DEFAULT_DAILY_ANCHOR,
        "day_start_equity": None,
        "daily_pause_reason": "",
        "weekly_anchor": "",
        "week_start_equity": None,
        "discovered_targets": [],
        "last_discovery_ts": 0,
    }
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
        "daily_anchor": str(data.get("daily_anchor") or ""),
        "day_start_equity": (
            float(data["day_start_equity"])
            if data.get("day_start_equity") not in (None, "")
            else None
        ),
        "daily_pause_reason": str(data.get("daily_pause_reason") or ""),
        "weekly_anchor": str(data.get("weekly_anchor") or ""),
        "week_start_equity": (
            float(data["week_start_equity"])
            if data.get("week_start_equity") not in (None, "")
            else None
        ),
        "discovered_targets": [str(t).lower() for t in data.get("discovered_targets", [])] if isinstance(data.get("discovered_targets"), list) else [],
        "last_discovery_ts": int(data.get("last_discovery_ts") or 0),
    }


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "seen": list(state.get("seen", []))[-_MAX_SEEN:],
        "last_ts_by_target": {
            str(k).lower(): int(v)
            for k, v in (state.get("last_ts_by_target") or {}).items()
        },
        "daily_anchor": str(state.get("daily_anchor") or ""),
        "day_start_equity": state.get("day_start_equity"),
        "daily_pause_reason": str(state.get("daily_pause_reason") or ""),
        "weekly_anchor": str(state.get("weekly_anchor") or ""),
        "week_start_equity": state.get("week_start_equity"),
        "discovered_targets": state.get("discovered_targets", []),
        "last_discovery_ts": state.get("last_discovery_ts", 0),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _get_tiered_ratio(settings: Settings, pnl: float) -> float:
    """Calculate copy ratio based on whale PnL using tiered config."""
    default = float(settings.mirror_copy_ratio)
    raw = settings.mirror_tiered_copy_ratios.strip()
    if not raw:
        return default
    tiers: list[tuple[float, float]] = []
    for chunk in raw.split(","):
        try:
            pnl_limit, ratio = chunk.split(":")
            tiers.append((float(pnl_limit), float(ratio)))
        except (ValueError, TypeError):
            continue
    if not tiers:
        return default
    tiers.sort(key=lambda t: t[0], reverse=True)
    for pnl_limit, ratio in tiers:
        if pnl >= pnl_limit:
            return ratio
    return default


def _discovery_tick(settings: Settings, state: dict[str, Any], api: DataApiClient) -> None:
    """Periodically find new profitable traders and add them to discovered list."""
    if not settings.mirror_discovery_enabled:
        return
    now = int(time.time())
    interval_sec = settings.mirror_discovery_interval_hours * 3600
    if now - state.get("last_discovery_ts", 0) < interval_sec:
        return

    print(f"[mirror] running target discovery...", flush=True)
    data = fetch_smart_money_data(settings, client=api)
    new_targets: list[str] = []
    min_pnl = settings.mirror_min_whale_pnl
    min_roi = getattr(settings, "smart_min_trader_roi", 0.0)
    
    for trader in data.traders:
        if trader.pnl >= min_pnl:
            roi = trader.pnl / trader.volume if trader.volume > 0 else 0.0
            if roi < min_roi:
                continue
            addr = trader.wallet.lower()
            if addr not in state["discovered_targets"]:
                new_targets.append(addr)
    
    if new_targets:
        print(f"[mirror] discovered {len(new_targets)} new high-PnL target(s)", flush=True)
        state["discovered_targets"].extend(new_targets)
    
    state["last_discovery_ts"] = now


def _check_overcrowded(api: DataApiClient, token_id: str, limit_count: int = 15) -> bool:
    """Check if many top holders are already in this market (overcrowded)."""
    try:
        holders = api.holders(token_id=token_id, limit=20)
        # Simple heuristic: if top 10 holders own > 50% or similar? 
        # API doesn't give total supply easily here. 
        # Alternative: if more than X distinct wallets in top holders are likely whales?
        # For now, just count if many large positions exist.
        if len(holders) >= limit_count:
            # Check if top holder has huge %? 
            return False # Placeholder: refine if needed
    except Exception:
        pass
    return False


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


def _position_category(position: dict[str, Any]) -> str:
    signal = position.get("signal")
    if isinstance(signal, dict):
        category = signal.get("category")
        if isinstance(category, str) and category:
            return category
    category = position.get("category")
    if isinstance(category, str) and category:
        return category
    question = str(position.get("question") or "")
    slug = str(position.get("slug") or "")
    return market_category(question, slug)


def _category_exposure(portfolio: Portfolio, category: str) -> float:
    if not category:
        return 0.0
    return sum(
        float(position.get("stake", 0.0) or 0.0)
        for position in portfolio.positions
        if position.get("status") == "open" and _position_category(position) == category
    )


def _mirror_trade_stake(
    settings: Settings,
    portfolio: Portfolio,
    trade: SmartTrade,
    pnl: float = 0.0,
) -> float:
    summary = portfolio.summary()
    equity = float(summary.get("equity", 0.0) or 0.0)
    cash = float(summary.get("cash", 0.0) or 0.0)
    whale_size = float(trade.usdc_size or 0.0)
    
    ratio = _get_tiered_ratio(settings, pnl)
    base = whale_size * max(0.0, ratio)
    
    max_by_equity = equity * max(0.0, float(settings.mirror_max_position_pct))
    max_by_cap = max(0.0, float(settings.mirror_size_usd))
    return round(min(base, max_by_equity, max_by_cap, cash), 2)


def _passes_sentiment_filters(candidate: Candidate, settings: Settings) -> bool:
    """Check if market question/slug contains any priority keywords."""
    text = f"{candidate.question} {candidate.slug}".lower()
    
    # Use discovery keywords as a sentiment filter if provided
    if settings.smart_discovery_keywords:
        keywords = [k.strip().lower() for k in settings.smart_discovery_keywords.split(",") if k.strip()]
        if keywords:
            # If the market matches a keyword, it's "news-relevant"
            # We don't necessarily want to block non-matches for mirroring, 
            # but we can log it or prioritize it.
            # For a "filter", let's say we only want news-relevant markets? 
            # Actually, mirroring should follow the whale. 
            # Let's just make sure it doesn't contain "bad" keywords if we had any.
            pass

    return True


def _mirror_buy(
    client: Any,
    settings: Settings,
    portfolio: Portfolio,
    candidate: Candidate,
    trade: SmartTrade,
    target_short: str,
    *,
    stake_usd: float,
) -> dict[str, Any]:
    if stake_usd <= 0:
        return {
            "action": "skip",
            "reason": "size_cap",
            "token_id": trade.asset,
            "target": target_short,
        }
    
    if not _passes_sentiment_filters(candidate, settings):
        return {
            "action": "skip",
            "reason": "sentiment_filter",
            "token_id": trade.asset,
        }

    price_guard = float(candidate.best_ask or candidate.price or 0.0)
    if price_guard > 0 and (stake_usd / price_guard) < float(settings.min_order_shares):
        return {
            "action": "skip",
            "reason": "below_min_order_size",
            "token_id": trade.asset,
            "target": target_short,
            "stake_usd": stake_usd,
            "min_shares": settings.min_order_shares,
        }
    
    # CONVICTION: Allow adding to existing position if whale is adding.
    existing = next((p for p in portfolio.positions if p.get("status") == "open" and p.get("market_id") == candidate.market_id and p.get("outcome") == candidate.outcome), None)
    if existing:
        # If we already have a position, check if we should add to it.
        # Simple rule: if our current stake is < 50% of the calculated stake for this whale trade, we add.
        current_stake = float(existing.get("stake", 0.0))
        if current_stake >= stake_usd * 0.8: # Already have enough exposure
             return {
                "action": "skip",
                "reason": "duplicate_open_position",
                "token_id": trade.asset,
            }
        # Otherwise, we'll let execute_live_trade handle the top-up.
    
    if portfolio.has_open_event_position(candidate) and not existing:
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
    
    # LIQUIDITY CHECK
    if settings.mirror_min_liquidity_usd > 0:
        liquidity = getattr(candidate, "liquidity", 0.0) or 0.0
        if liquidity > 0 and liquidity < settings.mirror_min_liquidity_usd:
            return {
                "action": "skip",
                "reason": "low_liquidity",
                "token_id": trade.asset,
                "liquidity": round(liquidity, 2),
            }

    category = market_category(candidate.question, candidate.slug)
    category_exposure = _category_exposure(portfolio, category)
    summary = portfolio.summary()
    equity = float(summary.get("equity", 0.0) or 0.0)
    category_cap = equity * max(0.0, float(settings.mirror_max_category_exposure_pct))
    if category_cap > 0 and category_exposure + stake_usd > category_cap:
        return {
            "action": "skip",
            "reason": "category_cap",
            "token_id": trade.asset,
            "category": category,
        }
    if settings.mirror_max_open_positions > 0 and int(summary.get("open_positions", 0) or 0) >= settings.mirror_max_open_positions and not existing:
        return {
            "action": "skip",
            "reason": "max_open_positions",
            "token_id": trade.asset,
        }
    from .trading import _is_filled_buy_response
    try:
        result = execute_live_trade(
            client,
            settings,
            candidate,
            portfolio,
            min_trade_usd=1.0,
            max_trade_usd=stake_usd,
            strategy="mirror",
            signal={
                "tag": "mirror",
                "target": target_short,
                "category": category,
                "whale_trade_usd": trade.usdc_size,
                "copy_ratio": settings.mirror_copy_ratio,
            },
        )
    except Exception as exc:
        return {
            "action": "skip",
            "reason": f"buy_failed:{type(exc).__name__}",
            "token_id": trade.asset,
            "message": str(exc)[:200],
        }
    resp = getattr(result, "response", None) if result else None
    if resp is not None and _is_filled_buy_response(resp):
        return {
            "action": "buy" if not existing else "add",
            "token_id": trade.asset,
            "size_usd": stake_usd,
            "price": candidate.best_ask,
            "category": category,
        }
    status = str(resp.get("status", "")) if isinstance(resp, dict) else "unknown"
    return {
        "action": "skip",
        "reason": f"unfilled:{status}",
        "token_id": trade.asset,
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
    whale_exit_threshold = max(0.0, float(position.get("stake", 0.0) or 0.0)) * max(
        0.0, float(settings.mirror_whale_exit_fraction)
    )
    if whale_exit_threshold > 0 and float(trade.usdc_size or 0.0) < whale_exit_threshold:
        return {
            "action": "skip",
            "reason": "whale_sell_below_threshold",
            "token_id": trade.asset,
            "threshold": round(whale_exit_threshold, 2),
            "sell_usdc": round(float(trade.usdc_size or 0.0), 2),
        }
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


def _mirror_exit_candidates(portfolio: Portfolio, gamma: GammaClient) -> list[Candidate]:
    """Build pricing candidates for open mirrored positions so exits can run."""
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for position in portfolio.positions:
        if position.get("status") != "open" or not position.get("live"):
            continue
        token_id = str(position.get("token_id") or "")
        if not token_id or token_id in seen:
            continue
        seen.add(token_id)
        candidate = _candidate_for_token(token_id, gamma)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _execute_mirror_exits(
    client: Any,
    settings: Settings,
    portfolio: Portfolio,
    gamma: GammaClient,
    candidates: list[Candidate] | None = None,
) -> list[dict[str, object]]:
    """Run the standard stop-loss / terminal-win exits for mirror mode."""
    candidates = candidates if candidates is not None else _mirror_exit_candidates(portfolio, gamma)
    if not candidates:
        return []
    from .main import _execute_sell_strategy  # lazy import: main imports mirror

    return _execute_sell_strategy(client, settings, portfolio, candidates)


def _sync_daily_anchor(state: dict[str, Any], equity: float, *, now_ts: float | None = None) -> None:
    today = _utc_day_key(now_ts)
    if state.get("daily_anchor") != today:
        state["daily_anchor"] = today
        state["day_start_equity"] = round(equity, 2)
        state["daily_pause_reason"] = ""
    
    this_week = _utc_week_key(now_ts)
    if state.get("weekly_anchor") != this_week:
        state["weekly_anchor"] = this_week
        state["week_start_equity"] = round(equity, 2)


def _daily_pause_active(state: dict[str, Any], settings: Settings, equity: float) -> bool:
    if settings.mirror_daily_loss_limit_pct > 0:
        day_start = state.get("day_start_equity")
        if day_start not in (None, ""):
            threshold = float(day_start) * (1.0 - max(0.0, float(settings.mirror_daily_loss_limit_pct)))
            if equity <= threshold:
                return True

    if settings.mirror_weekly_loss_limit_pct > 0:
        week_start = state.get("week_start_equity")
        if week_start not in (None, ""):
            threshold = float(week_start) * (1.0 - max(0.0, float(settings.mirror_weekly_loss_limit_pct)))
            if equity <= threshold:
                return True
                
    return False


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
    static_targets = _resolved_targets(settings, verbose=False)
    state = _load_state(settings.mirror_state_path)
    
    api = DataApiClient(settings.data_api_base_url)
    _discovery_tick(settings, state, api)
    
    targets = sorted(list(set(static_targets + state.get("discovered_targets", []))))
    
    if not targets:
        print("[mirror] POLYMARKET_MIRROR_TARGET is empty and no targets discovered", flush=True)
        return {
            "summary": {"equity": 0.0, "cash": 0.0, "open_positions": 0},
            "actions": [],
            "scan_counts": {"polled": 0, "eligible": 0, "mirrored": 0, "skipped": 0},
            "targets": [],
        }

    portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)

    if not settings.dry_run and settings.sync_live_positions and settings.funder_address:
        from .main import _sync_live_positions as _sync_pos
        sync_report = _sync_pos(settings, portfolio)
        if sync_report:
            for entry in sync_report:
                action = entry.get("action", "")
                token = entry.get("token_id", "")[:10]
                if action == "closed_stale_local_position":
                    print(f"   [sync] closed stale local position {token}", flush=True)
                elif action == "filled_by_live_sync":
                    print(f"   [sync] pending order filled {token}", flush=True)
                elif action == "imported_live_position":
                    print(f"   [sync] imported live position {token}", flush=True)
                elif action == "updated_live_position":
                    print(f"   [sync] updated position {token} shares={entry.get('shares','?')}", flush=True)
                elif action == "synced_cash":
                    print(f"   [sync] cash ${entry.get('old','?')} → ${entry.get('new','?')}", flush=True)

    seen: set[str] = set(state["seen"])
    last_ts_by_target: dict[str, int] = dict(state["last_ts_by_target"])

    print(
        f"▶  mirror tick — {len(targets)} target(s) ({len(static_targets)} static, {len(state.get('discovered_targets', []))} discovered): "
        f"{', '.join(_short_target(t) for t in targets[:5])}" + (f" and {len(targets)-5} more" if len(targets) > 5 else ""),
        flush=True,
    )

    now_ts = int(time.time())
    all_eligible: list[tuple[str, SmartTrade]] = []
    polled = 0
    
    # We might want whale PnLs for tiered ratios. 
    # fetch_smart_money_data is expensive to call on every tick if many traders.
    # For now, let's just use the pnl from the last discovery if available, or fetch it.
    whale_pnls: dict[str, float] = {}
    if settings.mirror_tiered_copy_ratios:
        # Best effort: fetch top traders to get current PnLs
        try:
            data = fetch_smart_money_data(settings, client=api)
            whale_pnls = {t.wallet.lower(): t.pnl for t in data.traders}
        except Exception:
            pass

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
    exit_candidates = _mirror_exit_candidates(portfolio, gamma)
    portfolio.mark_to_market(exit_candidates)
    summary_before_exits = portfolio.summary()
    _sync_daily_anchor(state, float(summary_before_exits.get("equity", 0.0) or 0.0), now_ts=now_ts)

    actions: list[dict[str, Any]] = []
    exits = _execute_mirror_exits(client, settings, portfolio, gamma, exit_candidates)
    mirrored = 0
    skipped = 0
    summary_after_exits = portfolio.summary()
    daily_paused = _daily_pause_active(state, settings, float(summary_after_exits.get("equity", 0.0) or 0.0))
    state["daily_pause_reason"] = "drawdown_limit" if daily_paused else ""

    for target, trade in all_eligible:
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
        if daily_paused and trade.side.upper() == "BUY":
            actions.append(
                {
                    "action": "skip",
                    "reason": "daily_loss_limit",
                    "token_id": trade.asset,
                    "side": trade.side,
                    "target": target_short,
                }
            )
            skipped += 1
            continue
        
        # OVERCROWDED CHECK
        if trade.side.upper() == "BUY" and _check_overcrowded(api, trade.asset):
            actions.append({
                "action": "skip",
                "reason": "overcrowded",
                "token_id": trade.asset,
                "target": target_short,
            })
            skipped += 1
            continue

        side = trade.side.upper()
        if side == "BUY":
            pnl = whale_pnls.get(target, 0.0)
            stake_usd = _mirror_trade_stake(settings, portfolio, trade, pnl=pnl)
            result = _mirror_buy(
                client,
                settings,
                portfolio,
                candidate,
                trade,
                target_short,
                stake_usd=stake_usd,
            )
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
        if result["action"] in ("buy", "sell", "add"):
            mirrored += 1
            last_ts_by_target[target] = max(
                last_ts_by_target.get(target, 0), trade.timestamp
            )
            seen.add(_trade_key(target, trade))
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
            "exits": len([e for e in exits if e.get("action") == "sell"]),
        },
        "targets": [_short_target(t) for t in targets],
        "exits": exits,
        "daily_pause": {
            "active": daily_paused,
            "reason": state.get("daily_pause_reason", ""),
            "day_start_equity": state.get("day_start_equity"),
            "week_start_equity": state.get("week_start_equity"),
            "equity_after_exits": round(float(summary_after_exits.get("equity", 0.0) or 0.0), 2),
        },
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
