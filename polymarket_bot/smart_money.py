"""Smart-money signal engine.

Fetches profitable wallets from the Polymarket leaderboards (across one or
more time periods and categories), pulls each wallet's recent BUY trades in
parallel, groups by asset, and produces ranked :class:`SmartMoneySignal`
opportunities that pass spread, price-band, freshness, and consensus
filters. Also exposes a reverse-lookup helper that fetches missing markets
by ``clob_token_ids`` so high-flow tokens not present in the standard Gamma
scan are not silently dropped.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings
from .models import Candidate
from .wallet_persistence import (
    PersistenceSignal,
    WalletHistoryStore,
    filter_cohort_by_persistence,
)


@dataclass(frozen=True)
class SmartTrader:
    wallet: str
    username: str
    pnl: float
    volume: float
    category: str


@dataclass(frozen=True)
class SmartTrade:
    wallet: str
    asset: str
    side: str
    price: float
    size: float
    usdc_size: float
    timestamp: int
    title: str
    outcome: str
    slug: str


@dataclass(frozen=True)
class SmartMoneySignal:
    candidate: Candidate
    consensus: int
    copied_usdc: float
    avg_copy_price: float
    wallets: list[str]
    titles: list[str]
    total_trader_pnl: float = 0.0
    spread: float = 0.0
    min_consensus: int = 2
    is_crypto_micro: bool = False
    category: str = "OTHER"
    category_bonus: float = 0.0
    latest_trade_age_minutes: float | None = None
    fresh_bonus: float = 0.0
    fresh_wallets: int = 0
    largest_wallet_share: float = 0.0
    flow_balance_bonus: float = 0.0
    source: str = "consensus"  # "consensus" (cohort) or "whale" (single big bet)

    @property
    def score(self) -> float:
        pnl_bonus = min(self.total_trader_pnl / 1000.0, 15.0)
        value = _value_score(self.avg_copy_price, self.candidate.best_ask, self.copied_usdc)
        return (
            (self.consensus * 10.0)
            + min(self.copied_usdc / 10.0, 25.0)
            + pnl_bonus
            + _short_horizon_bonus(self.candidate.hours_to_close)
            + value
            + self.category_bonus
            + self.fresh_bonus
            + self.flow_balance_bonus
            - (self.candidate.best_ask or 0.0)
        )

    def to_dict(self) -> dict[str, Any]:
        price_distance = (
            round((self.candidate.best_ask or 0.0) - self.avg_copy_price, 4)
            if self.avg_copy_price > 0.0 and self.candidate.best_ask is not None
            else None
        )
        value_pct = _value_pct(self.avg_copy_price, self.candidate.best_ask)
        value_text = (
            f", value discount {abs(value_pct):.1%} below smart-money avg"
            if value_pct > 0
            else f", chase premium {abs(value_pct):.1%} above smart-money avg"
            if value_pct < 0
            else ""
        )
        if self.source == "whale":
            wallet = self.wallets[0] if self.wallets else "?"
            selection_reason = (
                f"WHALE single-wallet buy: {wallet} bought ${self.copied_usdc:,.0f} of this "
                f"token at avg {self.avg_copy_price:.4f} in the lookback window; "
                f"current ask {self.candidate.best_ask} has spread {self.spread:.4f}, "
                f"closes in {_format_hours(self.candidate.hours_to_close)}, "
                f"latest buy {_format_minutes(self.latest_trade_age_minutes)} ago{value_text}; "
                f"passed price band, spread, liquidity, and exclusion checks."
            )
        else:
            selection_reason = (
                f"{self.consensus} profitable wallets bought this same token recently, "
                f"copying ${self.copied_usdc:.2f} total at avg {self.avg_copy_price:.4f}; "
                f"current ask {self.candidate.best_ask} has spread {self.spread:.4f}, "
                f"closes in {_format_hours(self.candidate.hours_to_close)}, "
                f"latest copied buy {_format_minutes(self.latest_trade_age_minutes)} ago{value_text}, "
                f"fresh wallets {self.fresh_wallets}, largest wallet share {self.largest_wallet_share:.1%}, "
                f"and passed min consensus {self.min_consensus}, price band, spread, and duplicate checks."
            )
        return {
            "market_id": self.candidate.market_id,
            "question": self.candidate.question,
            "outcome": self.candidate.outcome,
            "best_ask": self.candidate.best_ask,
            "best_bid": self.candidate.best_bid,
            "consensus": self.consensus,
            "copied_usdc": self.copied_usdc,
            "avg_copy_price": self.avg_copy_price,
            "wallets": self.wallets,
            "titles": self.titles,
            "total_trader_pnl": self.total_trader_pnl,
            "category": self.category,
            "score": round(self.score, 4),
            "source": self.source,
            "selection_reason": selection_reason,
            "selection_metrics": {
                "profitable_wallet_count": self.consensus,
                "min_consensus": self.min_consensus,
                "copied_usdc": self.copied_usdc,
                "avg_copy_price": self.avg_copy_price,
                "current_ask": self.candidate.best_ask,
                "current_bid": self.candidate.best_bid,
                "spread": round(self.spread, 4),
                "hours_to_close": self.candidate.hours_to_close,
                "ask_minus_avg_copy_price": price_distance,
                "value_discount_pct": round(value_pct, 4),
                "value_score": round(_value_score(self.avg_copy_price, self.candidate.best_ask, self.copied_usdc), 4),
                "total_trader_pnl": round(self.total_trader_pnl, 2),
                "is_crypto_micro": self.is_crypto_micro,
                "category": self.category,
                "category_bonus": round(self.category_bonus, 4),
                "latest_trade_age_minutes": (
                    round(self.latest_trade_age_minutes, 2) if self.latest_trade_age_minutes is not None else None
                ),
                "fresh_bonus": round(self.fresh_bonus, 4),
                "fresh_wallets": self.fresh_wallets,
                "largest_wallet_share": round(self.largest_wallet_share, 4),
                "flow_balance_score": round(self.flow_balance_bonus, 4),
            },
            "url": self.candidate.url,
        }


@dataclass(frozen=True)
class SmartMoneyReport:
    selected: SmartMoneySignal | None
    opportunities: list[SmartMoneySignal]
    traders_checked: int
    traders_used: int
    trades_checked: int
    eligible_trade_count: int
    grouped_tokens: int
    matched_tokens: int
    rejected: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": self.selected.to_dict() if self.selected else None,
            "opportunities": [signal.to_dict() for signal in self.opportunities[:10]],
            "traders_checked": self.traders_checked,
            "traders_used": self.traders_used,
            "trades_checked": self.trades_checked,
            "eligible_trade_count": self.eligible_trade_count,
            "grouped_tokens": self.grouped_tokens,
            "matched_tokens": self.matched_tokens,
            "rejected": self.rejected,
        }


class DataApiClient:
    def __init__(self, base_url: str = "https://data-api.polymarket.com", timeout: int = 15) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def leaderboard(self, *, category: str, time_period: str, limit: int) -> list[SmartTrader]:
        payload = self._get_json(
            "/v1/leaderboard",
            {
                "category": category,
                "timePeriod": time_period,
                "orderBy": "PNL",
                "limit": str(limit),
            },
        )
        traders: list[SmartTrader] = []
        for item in payload if isinstance(payload, list) else []:
            wallet = str(item.get("proxyWallet") or "")
            if not wallet:
                continue
            traders.append(
                SmartTrader(
                    wallet=wallet,
                    username=str(item.get("userName") or item.get("pseudonym") or ""),
                    pnl=_float(item.get("pnl")),
                    volume=_float(item.get("vol")),
                    category=category,
                )
            )
        return traders

    def trades(
        self,
        *,
        user: str,
        start: int,
        limit: int = 100,
        side: str | None = "BUY",
    ) -> list[SmartTrade]:
        params: dict[str, str] = {
            "user": user,
            "start": str(start),
            "limit": str(limit),
            "takerOnly": "true",
        }
        if side:
            params["side"] = side
        payload = self._get_json("/trades", params)
        trades: list[SmartTrade] = []
        for item in payload if isinstance(payload, list) else []:
            asset = str(item.get("asset") or "")
            if not asset:
                continue
            trades.append(
                SmartTrade(
                    wallet=str(item.get("proxyWallet") or user),
                    asset=asset,
                    side=str(item.get("side") or ""),
                    price=_float(item.get("price")),
                    size=_float(item.get("size")),
                    usdc_size=_float(item.get("usdcSize"), _float(item.get("size")) * _float(item.get("price"))),
                    timestamp=int(_float(item.get("timestamp"))),
                    title=str(item.get("title") or ""),
                    outcome=str(item.get("outcome") or ""),
                    slug=str(item.get("slug") or item.get("eventSlug") or ""),
                )
            )
        return trades

    def recent_trades(
        self,
        *,
        start: int,
        limit: int = 500,
        side: str | None = "BUY",
        min_usdc: float = 0.0,
    ) -> list[SmartTrade]:
        """Global recent taker trades (NO user filter), for whale detection.

        Notes from probing the live data-api (2026-06-15):
        - Sending ``start`` on a global (no-user) query makes the endpoint scan
          a huge window and return HTTP 408 — so we do NOT send it; ``start``
          is applied CLIENT-SIDE as a timestamp cutoff instead.
        - The global feed has no ``usdcSize`` field, so the dollar value is
          ``size * price`` (the ``_float`` fallback below).
        - ``filterType``/``filterAmount`` do NOT filter by size here, so the
          ≥ ``min_usdc`` cut is done client-side too.
        """
        params: dict[str, str] = {
            "limit": str(limit),
            "takerOnly": "true",
        }
        if side:
            params["side"] = side
        payload = self._get_json("/trades", params)
        trades: list[SmartTrade] = []
        for item in payload if isinstance(payload, list) else []:
            asset = str(item.get("asset") or "")
            if not asset:
                continue
            ts = int(_float(item.get("timestamp")))
            if start and ts < start:
                continue
            usdc = _float(item.get("usdcSize"), _float(item.get("size")) * _float(item.get("price")))
            if min_usdc > 0 and usdc < min_usdc:
                continue
            trades.append(
                SmartTrade(
                    wallet=str(item.get("proxyWallet") or ""),
                    asset=asset,
                    side=str(item.get("side") or ""),
                    price=_float(item.get("price")),
                    size=_float(item.get("size")),
                    usdc_size=usdc,
                    timestamp=int(_float(item.get("timestamp"))),
                    title=str(item.get("title") or ""),
                    outcome=str(item.get("outcome") or ""),
                    slug=str(item.get("slug") or item.get("eventSlug") or ""),
                )
            )
        return trades

    def positions(self, *, user: str, limit: int = 500) -> list[dict[str, Any]]:
        # use_cache=False: the live ledger sync depends on this being
        # current. If a stale snapshot were cached for 600s, the live
        # bot would miss positions that landed mid-cache-window and the
        # local ledger would stay out of sync with Polymarket for up to
        # 10min — observed in production as $4 local equity vs $46 real.
        payload = self._get_json(
            "/positions",
            {
                "user": user,
                "limit": str(limit),
                "sizeThreshold": "0",
            },
            use_cache=False,
        )
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    def _get_json(
        self,
        path: str,
        params: dict[str, str],
        *,
        use_cache: bool = True,
    ) -> Any:
        query = urllib.parse.urlencode(sorted(params.items()))  # stable cache key
        url = f"{self.base_url}{path}?{query}"

        # Cross-process disk cache. When 50+ bots all want the same
        # leaderboard or wallet trades, only the first one hits the
        # network — the rest read from cache/. TTL default 600s; tunable
        # via POLYMARKET_HTTP_CACHE_TTL_SECONDS. Per-user endpoints (like
        # /positions for the live wallet) must bypass — see positions().
        if use_cache:
            cached = _cache_read(url)
            if cached is not None:
                return cached

        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "polymarket-bot/0.1",
            },
        )
        # Retry on 429 (Too Many Requests) with backoff. The cohort fetch pulls
        # 100+ wallets per tick and bursts trip the data-api rate limiter; a
        # short retry recovers most without losing the wallet's signal. Respects
        # Retry-After when the server sends it, capped so a tick never stalls.
        delay = 0.4
        last_err: Exception | None = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if use_cache:
                    _cache_write(url, payload)
                return payload
            except urllib.error.HTTPError as exc:
                last_err = exc
                if exc.code != 429 or attempt == 3:
                    raise
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    wait = float(retry_after) if retry_after else delay
                except (TypeError, ValueError):
                    wait = delay
                time.sleep(min(wait, 3.0))
                delay *= 2
        if last_err is not None:  # pragma: no cover - loop always returns/raises
            raise last_err


# ─── Shared HTTP cache (cross-process, file-based) ────────────────────
#
# Why: every smart_money bot in the dry race fetches the SAME leaderboard
# and (largely) the same wallet trade histories. With 50+ bots × 50 wallet
# fetches per tick = 2,500 redundant API calls per minute. Caching the
# JSON payload by URL with a short TTL (default 90s) collapses that to
# ~50 total network calls regardless of bot count.
#
# Cross-process: each unique URL gets its own file under data/cache/http/.
# First bot to need it fetches and writes; subsequent bots within the
# TTL window read the file. Atomic via tempfile + rename.

_CACHE_DIR = Path("data/cache/http")
# Cache TTL default 600s (10min): the multi-bot cold-start spike is what
# kills the 429 rate. 50 bots launching together can saturate the API
# before any of them populate the cache. A longer TTL means subsequent
# ticks (at minute +10 for dry bots) still see warm data, and the cache
# pays off over many ticks. Set lower (e.g. 60) for fresher signal at
# the cost of more API calls.
_CACHE_TTL = int(os.environ.get("POLYMARKET_HTTP_CACHE_TTL_SECONDS", "600"))


def _cache_key(url: str) -> Path:
    import hashlib as _h
    h = _h.sha1(url.encode("utf-8")).hexdigest()[:32]
    return _CACHE_DIR / f"{h}.json"


def _cache_read(url: str) -> Any | None:
    if _CACHE_TTL <= 0:
        return None
    try:
        path = _cache_key(url)
        if not path.exists():
            return None
        if time.time() - path.stat().st_mtime > _CACHE_TTL:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _cache_write(url: str, payload: Any) -> None:
    if _CACHE_TTL <= 0:
        return
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _cache_key(url)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.rename(path)
    except Exception:
        pass


def choose_smart_money_trade(
    candidates: list[Candidate],
    settings: Settings,
    *,
    client: DataApiClient | None = None,
) -> SmartMoneySignal | None:
    return analyze_smart_money(candidates, settings, client=client).selected


@dataclass(frozen=True)
class SmartMoneyData:
    traders: list[SmartTrader]
    trades: list[SmartTrade]
    pnl_by_wallet: dict[str, float]
    traders_used: int
    leaderboard_error: str | None = None
    persistence_signals: dict[str, PersistenceSignal] = field(default_factory=dict)
    cohort_before_persistence: int = 0
    cohort_after_persistence: int = 0
    trades_fetch_errors: int = 0


def fetch_smart_money_data(
    settings: Settings,
    *,
    client: DataApiClient | None = None,
) -> SmartMoneyData:
    client = client or DataApiClient(settings.data_api_base_url)
    try:
        traders_by_period = _top_traders(client, settings)
        # Liste plate dédupée pour le pipeline existant (compat ascendante)
        seen_wallets: set[str] = set()
        traders: list[SmartTrader] = []
        for period_traders in traders_by_period.values():
            for t in period_traders:
                key = t.wallet.lower()
                if key in seen_wallets:
                    continue
                seen_wallets.add(key)
                traders.append(t)
    except Exception as exc:
        return SmartMoneyData(
            traders=[],
            trades=[],
            pnl_by_wallet={},
            traders_used=0,
            leaderboard_error=f"leaderboard_api_error_{type(exc).__name__}",
        )
    pnl_by_wallet = {t.wallet.lower(): t.pnl for t in traders}

    def _qualifies(trader: SmartTrader) -> bool:
        if trader.pnl < settings.smart_min_trader_pnl:
            return False
        if settings.smart_min_trader_volume > 0 and trader.volume < settings.smart_min_trader_volume:
            return False
        if settings.smart_min_trader_roi > 0:
            roi = trader.pnl / trader.volume if trader.volume > 0 else 0.0
            if roi < settings.smart_min_trader_roi:
                return False
        return True

    cohort_before_prefilter = len(traders)
    qualified = [t for t in traders if _qualifies(t)]

    # Filtre persistance d'edge — branché entre pré-filtre PnL/Vol/ROI et fetch trades
    cohort_before = len(qualified)
    persistence_signals: dict[str, PersistenceSignal] = {}
    if settings.persistence_enabled:
        leaderboards_sets: dict[str, set[str]] = {
            period: {t.wallet.lower() for t in period_traders}
            for period, period_traders in traders_by_period.items()
        }
        store = WalletHistoryStore(
            settings.persistence_cache_path,
            window_days=settings.persistence_window_days,
        )
        qualified, persistence_signals = filter_cohort_by_persistence(
            qualified,
            leaderboards=leaderboards_sets,
            store=store,
            settings=settings,
        )
        if not settings.quiet:
            n_cache = sum(
                1 for s in persistence_signals.values()
                if s.cache_score >= settings.persistence_cache_threshold
            )
            n_intersect = sum(
                1 for s in persistence_signals.values()
                if s.intersect_score * 3 >= settings.persistence_intersect_min
            )
            n_both = sum(
                1 for s in persistence_signals.values()
                if s.cache_score >= settings.persistence_cache_threshold
                and s.intersect_score * 3 >= settings.persistence_intersect_min
            )
            print(
                f"      cohort: {cohort_before_prefilter} → {cohort_before} "
                f"(PnL/Vol/ROI) → {len(qualified)} "
                f"(persistence: {n_cache} cache, {n_intersect} intersect, {n_both} both)",
                flush=True,
            )
    cohort_after = len(qualified)

    if qualified and not settings.quiet:
        print(
            f"      pulling trades for {len(qualified)} qualified trader(s)"
            f" (concurrency={max(1, settings.smart_trade_fetch_concurrency)})...",
            flush=True,
        )
    start = int(time.time()) - (settings.smart_trade_lookback_minutes * 60)
    trades: list[SmartTrade] = []
    traders_used = 0
    fetch_errors: list[tuple[str, str]] = []
    concurrency = max(1, settings.smart_trade_fetch_concurrency)

    def _pull(trader: SmartTrader) -> list[SmartTrade]:
        try:
            return client.trades(user=trader.wallet, start=start)
        except Exception as exc:
            fetch_errors.append((trader.wallet, f"{type(exc).__name__}: {exc}"))
            return []

    if concurrency > 1 and len(qualified) > 1:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {executor.submit(_pull, trader): trader for trader in qualified}
            for future in as_completed(futures):
                traders_used += 1
                if not settings.quiet and (
                    traders_used == 1 or traders_used % 50 == 0 or traders_used == len(qualified)
                ):
                    print(
                        f"      trades fetched: {traders_used}/{len(qualified)} (running total: {len(trades)})",
                        flush=True,
                    )
                trades.extend(future.result())
    else:
        for trader in qualified:
            traders_used += 1
            if not settings.quiet and (
                traders_used == 1 or traders_used % 50 == 0 or traders_used == len(qualified)
            ):
                print(
                    f"      trades fetched: {traders_used}/{len(qualified)} (running total: {len(trades)})",
                    flush=True,
                )
            trades.extend(_pull(trader))
    if fetch_errors:
        # Sample the first 3 distinct error messages so we don't dump 24 identical 429s.
        sample = list(dict.fromkeys(msg for _, msg in fetch_errors))[:3]
        print(
            f"      ⚠️  {len(fetch_errors)}/{len(qualified)} trade fetches failed "
            f"(sample: {' | '.join(sample)})",
            file=sys.stderr,
            flush=True,
        )
    return SmartMoneyData(
        traders=traders,
        trades=trades,
        pnl_by_wallet=pnl_by_wallet,
        traders_used=traders_used,
        persistence_signals=persistence_signals,
        cohort_before_persistence=cohort_before,
        cohort_after_persistence=cohort_after,
        trades_fetch_errors=len(fetch_errors),
    )


def fetch_whale_signals(
    settings: Settings,
    eligible_candidates: list[Candidate],
    *,
    client: DataApiClient | None = None,
    now_ts: int | None = None,
) -> list[SmartMoneySignal]:
    """Whale-copy pass: copy ANY single wallet's large buy, leaderboard or not.

    Watches the GLOBAL recent-trade feed (no user filter), aggregates each
    wallet's BUY flow per token over the lookback window, and emits a
    consensus-1 ``source="whale"`` signal for every (wallet, token) whose flow
    reaches ``smart_whale_min_usdc``. Restricted to ``eligible_candidates`` —
    the markets that already passed the Gamma scan, exclusions (crypto ban,
    O/U lines, etc.), price band, spread and liquidity floors — so the whale
    pass can never trade something the cohort path would have refused.
    """
    if not settings.smart_whale_copy_enabled or settings.smart_whale_min_usdc <= 0:
        return []

    by_token: dict[str, Candidate] = {
        c.token_id: c for c in eligible_candidates if c.token_id
    }
    if not by_token:
        return []

    client = client or DataApiClient(settings.data_api_base_url)
    now_ts = now_ts if now_ts is not None else int(time.time())
    start = now_ts - max(1, settings.smart_whale_lookback_minutes) * 60
    try:
        raw = client.recent_trades(
            start=start,
            limit=settings.smart_whale_fetch_limit,
            side="BUY",
            min_usdc=settings.smart_whale_min_usdc,
        )
    except Exception as exc:  # fail-open: whale pass is additive, never fatal
        print(f"   whale-copy fetch skipped: {type(exc).__name__}: {exc}", flush=True)
        return []

    # Aggregate each wallet's flow per token (a single $50k fill OR several
    # buys by the same wallet summing past the threshold both qualify).
    agg: dict[tuple[str, str], dict[str, float]] = {}
    for t in raw:
        if t.side != "BUY" or not t.wallet or t.asset not in by_token:
            continue
        key = (t.wallet, t.asset)
        slot = agg.setdefault(key, {"usdc": 0.0, "size": 0.0, "latest": 0.0, "title": ""})
        slot["usdc"] += t.usdc_size
        slot["size"] += t.size
        if t.timestamp > slot["latest"]:
            slot["latest"] = float(t.timestamp)
        if t.title:
            slot["title"] = t.title

    # Keep only (wallet, token) at/above threshold; one signal per token
    # (largest wallet wins if several whales hit the same token).
    best_by_token: dict[str, SmartMoneySignal] = {}
    for (wallet, asset), slot in agg.items():
        usdc = slot["usdc"]
        if usdc < settings.smart_whale_min_usdc:
            continue
        candidate = by_token[asset]
        avg_price = usdc / slot["size"] if slot["size"] > 0 else (candidate.best_ask or 0.0)
        spread = (candidate.best_ask or 0.0) - (candidate.best_bid or 0.0)
        age_min = max(0.0, (now_ts - slot["latest"]) / 60.0) if slot["latest"] else None
        signal = SmartMoneySignal(
            candidate=candidate,
            consensus=1,
            copied_usdc=round(usdc, 2),
            avg_copy_price=round(avg_price, 4),
            wallets=[wallet],
            titles=[slot["title"] or candidate.question],
            spread=round(spread, 4),
            min_consensus=1,
            latest_trade_age_minutes=age_min,
            largest_wallet_share=1.0,
            source="whale",
        )
        prev = best_by_token.get(asset)
        if prev is None or usdc > prev.copied_usdc:
            best_by_token[asset] = signal

    return sorted(best_by_token.values(), key=lambda s: s.copied_usdc, reverse=True)


def analyze_smart_money_with_data(
    candidates: list[Candidate],
    settings: Settings,
    data: SmartMoneyData,
) -> SmartMoneyReport:
    if data.leaderboard_error is not None:
        return SmartMoneyReport(
            selected=None,
            opportunities=[],
            traders_checked=0,
            traders_used=0,
            trades_checked=0,
            eligible_trade_count=0,
            grouped_tokens=0,
            matched_tokens=0,
            rejected={data.leaderboard_error: 1},
        )
    signals, details = smart_money_signals(
        candidates, data.trades, settings, pnl_by_wallet=data.pnl_by_wallet, include_details=True
    )
    opportunities = sorted(signals, key=lambda item: item.score, reverse=True)
    return SmartMoneyReport(
        selected=opportunities[0] if opportunities else None,
        opportunities=opportunities,
        traders_checked=len(data.traders),
        traders_used=data.traders_used,
        trades_checked=len(data.trades),
        eligible_trade_count=int(details["eligible_trade_count"]),
        grouped_tokens=int(details["grouped_tokens"]),
        matched_tokens=int(details["matched_tokens"]),
        rejected=dict(details["rejected"]),
    )


def analyze_smart_money(
    candidates: list[Candidate],
    settings: Settings,
    *,
    client: DataApiClient | None = None,
) -> SmartMoneyReport:
    data = fetch_smart_money_data(settings, client=client)
    return analyze_smart_money_with_data(candidates, settings, data)


def smart_money_signals(
    candidates: list[Candidate],
    trades: list[SmartTrade],
    settings: Settings,
    *,
    pnl_by_wallet: dict[str, float] | None = None,
    include_details: bool = False,
) -> list[SmartMoneySignal] | tuple[list[SmartMoneySignal], dict[str, Any]]:
    by_token = {candidate.token_id: candidate for candidate in candidates if candidate.token_id}
    grouped: dict[str, list[SmartTrade]] = {}
    rejected: dict[str, int] = {}
    eligible_trade_count = 0
    now_ts = int(time.time())
    for trade in trades:
        if trade.side.upper() != "BUY" or trade.usdc_size < settings.smart_min_trade_usd:
            rejected["trade_too_small_or_not_buy"] = rejected.get("trade_too_small_or_not_buy", 0) + 1
            continue
        eligible_trade_count += 1
        grouped.setdefault(trade.asset, []).append(trade)

    signals: list[SmartMoneySignal] = []
    matched_tokens = 0
    for token_id, token_trades in grouped.items():
        candidate = by_token.get(token_id)
        if candidate is None or candidate.best_ask is None or candidate.best_bid is None:
            rejected["no_matching_candidate_or_quote"] = rejected.get("no_matching_candidate_or_quote", 0) + 1
            continue
        matched_tokens += 1
        # When the loser-flush exit is armed, any position opened inside the
        # flush window will be sold on the next tick — so don't open it. The
        # effective floor is max(smart_min_hours_to_close, flush_minutes/60).
        # Bug bled $117 on a $30 bankroll in 7min when the windows overlapped
        # (live baseline_tight 2026-05-22, market 2237157 Elon tweets).
        effective_min_hours = settings.smart_min_hours_to_close
        if settings.smart_near_expiry_exit_losers:
            effective_min_hours = max(
                effective_min_hours,
                settings.smart_near_expiry_loser_minutes / 60.0,
            )
        if candidate.hours_to_close is not None and candidate.hours_to_close < effective_min_hours:
            rejected["too_close_to_expiry"] = rejected.get("too_close_to_expiry", 0) + 1
            continue
        if (
            settings.smart_max_hours_to_close > 0
            and candidate.hours_to_close is not None
            and candidate.hours_to_close > settings.smart_max_hours_to_close
        ):
            rejected["too_far_to_expiry"] = rejected.get("too_far_to_expiry", 0) + 1
            continue
        if not candidate.accepts_orders or candidate.tick_size is None:
            rejected["not_accepting_orders"] = rejected.get("not_accepting_orders", 0) + 1
            continue
        spread = candidate.best_ask - candidate.best_bid
        if spread < 0 or spread > settings.smart_max_spread:
            rejected["spread_too_wide"] = rejected.get("spread_too_wide", 0) + 1
            continue
        if (
            settings.smart_max_relative_spread > 0
            and candidate.best_ask > 0
            and (spread / candidate.best_ask) > settings.smart_max_relative_spread
        ):
            rejected["spread_too_wide_relative"] = rejected.get("spread_too_wide_relative", 0) + 1
            continue
        if candidate.best_ask < settings.smart_min_buy_price or candidate.best_ask > settings.smart_max_buy_price:
            rejected["ask_outside_price_band"] = rejected.get("ask_outside_price_band", 0) + 1
            continue

        wallets = sorted({trade.wallet for trade in token_trades})
        category = market_category(candidate.question, candidate.slug)
        is_crypto = _is_crypto_market(candidate)
        is_crypto_micro = _is_crypto_micro(candidate)
        min_consensus = max(1, settings.smart_min_consensus)
        if is_crypto:
            min_consensus = max(min_consensus, settings.smart_crypto_min_consensus)
        if is_crypto_micro:
            min_consensus = max(min_consensus, settings.smart_crypto_micro_min_consensus)
        if len(wallets) < min_consensus:
            rejected["not_enough_wallet_consensus"] = rejected.get("not_enough_wallet_consensus", 0) + 1
            continue
        latest_trade_ts = max((trade.timestamp for trade in token_trades), default=0)
        latest_trade_age_minutes = max((now_ts - latest_trade_ts) / 60.0, 0.0) if latest_trade_ts else None
        if (
            settings.smart_max_signal_age_minutes > 0
            and latest_trade_age_minutes is not None
            and latest_trade_age_minutes > settings.smart_max_signal_age_minutes
        ):
            rejected["signal_too_stale"] = rejected.get("signal_too_stale", 0) + 1
            continue

        if settings.smart_min_wallet_flow_usdc > 0:
            flow_by_wallet = _wallet_flow(token_trades)
            token_trades = [
                trade for trade in token_trades
                if flow_by_wallet.get(trade.wallet, 0.0) >= settings.smart_min_wallet_flow_usdc
            ]
            wallets = sorted({trade.wallet for trade in token_trades})
            if len(wallets) < min_consensus:
                rejected["not_enough_wallet_flow"] = rejected.get("not_enough_wallet_flow", 0) + 1
                continue

        total_trader_pnl = sum(pnl_by_wallet.get(w.lower(), 0.0) for w in wallets) if pnl_by_wallet else 0.0
        copied_usdc = round(sum(trade.usdc_size for trade in token_trades), 2)
        if copied_usdc < settings.smart_min_copied_usdc:
            rejected["copied_usdc_too_small"] = rejected.get("copied_usdc_too_small", 0) + 1
            continue
        wallet_flow = _wallet_flow(token_trades)
        largest_wallet_share = max(wallet_flow.values(), default=0.0) / copied_usdc if copied_usdc > 0 else 0.0
        if (
            settings.smart_max_wallet_flow_share > 0
            and len(wallets) > 1
            and largest_wallet_share > settings.smart_max_wallet_flow_share
        ):
            rejected["wallet_flow_too_concentrated"] = rejected.get("wallet_flow_too_concentrated", 0) + 1
            continue
        fresh_wallets = _fresh_wallet_count(token_trades, now_ts, settings.smart_fresh_wallet_minutes)
        if settings.smart_min_fresh_wallets > 0 and fresh_wallets < settings.smart_min_fresh_wallets:
            rejected["not_enough_fresh_wallets"] = rejected.get("not_enough_fresh_wallets", 0) + 1
            continue
        total_size = sum(trade.size for trade in token_trades)
        avg_price = round(sum(trade.price * trade.size for trade in token_trades) / total_size, 4) if total_size else 0.0
        if _value_pct(avg_price, candidate.best_ask) < -settings.smart_max_chase_premium:
            rejected["chase_premium_too_high"] = rejected.get("chase_premium_too_high", 0) + 1
            continue
        if is_crypto and not _crypto_signal_allowed(candidate, settings, len(wallets), copied_usdc):
            rejected["crypto_signal_blocked"] = rejected.get("crypto_signal_blocked", 0) + 1
            continue
        signals.append(
            SmartMoneySignal(
                candidate=candidate,
                consensus=len(wallets),
                copied_usdc=copied_usdc,
                avg_copy_price=avg_price,
                wallets=wallets,
                titles=sorted({trade.title for trade in token_trades if trade.title})[:3],
                total_trader_pnl=total_trader_pnl,
                spread=spread,
                min_consensus=min_consensus,
                is_crypto_micro=is_crypto_micro,
                category=category,
                category_bonus=_category_bonus(category, settings),
                latest_trade_age_minutes=latest_trade_age_minutes,
                fresh_bonus=_fresh_signal_bonus(latest_trade_age_minutes, settings),
                fresh_wallets=fresh_wallets,
                largest_wallet_share=largest_wallet_share,
                flow_balance_bonus=_flow_balance_bonus(largest_wallet_share, len(wallets)),
            )
        )
    if include_details:
        return signals, {
            "eligible_trade_count": eligible_trade_count,
            "grouped_tokens": len(grouped),
            "matched_tokens": matched_tokens,
            "rejected": rejected,
        }
    return signals


def _time_periods(settings: Settings) -> list[str]:
    raw = settings.smart_time_periods.strip()
    if raw:
        periods = [item.strip().upper() for item in raw.split(",") if item.strip()]
        if periods:
            return periods
    return [settings.smart_time_period.strip().upper() or "WEEK"]


def _wallet_flow(trades: list[SmartTrade]) -> dict[str, float]:
    flow: dict[str, float] = {}
    for trade in trades:
        flow[trade.wallet] = flow.get(trade.wallet, 0.0) + max(0.0, trade.usdc_size)
    return flow


def _fresh_wallet_count(trades: list[SmartTrade], now_ts: int, window_minutes: int) -> int:
    if window_minutes <= 0:
        return len({trade.wallet for trade in trades})
    cutoff = now_ts - (window_minutes * 60)
    return len({trade.wallet for trade in trades if trade.timestamp >= cutoff})


def _flow_balance_bonus(largest_wallet_share: float, wallet_count: int) -> float:
    if wallet_count < 2 or largest_wallet_share <= 0.0:
        return 0.0
    return max(0.0, 1.0 - largest_wallet_share) * min(wallet_count, 5) * 2.0


def _top_traders(client: DataApiClient, settings: Settings) -> dict[str, list[SmartTrader]]:
    """Retourne un dict {period: traders} (au lieu d'une liste dédupée).

    La déduplication par wallet est désormais responsabilité du consommateur,
    qui peut ainsi calculer les croisements multi-période (filtre persistance).
    Dédup interne par période (un même wallet dans plusieurs catégories
    n'apparaît qu'une fois dans la liste de cette période).
    """
    result: dict[str, list[SmartTrader]] = {}
    categories = _categories(settings)
    periods = _time_periods(settings)
    combos = [(period, category) for period in periods for category in categories]
    for index, (period, category) in enumerate(combos, 1):
        if not settings.quiet:
            print(f"      leaderboard {index}/{len(combos)} {period}/{category}...", flush=True)
        try:
            category_traders = client.leaderboard(
                category=category,
                time_period=period,
                limit=settings.smart_leaderboard_limit,
            )
        except Exception as exc:
            print(
                f"⚠️  Smart-money leaderboard skipped: {period}/{category} {type(exc).__name__}: {exc}",
                flush=True,
            )
            continue
        bucket = result.setdefault(period, [])
        seen = {t.wallet.lower() for t in bucket}
        added = 0
        for trader in category_traders:
            key = trader.wallet.lower()
            if key in seen:
                continue
            seen.add(key)
            bucket.append(trader)
            added += 1
        if not settings.quiet:
            print(f"         +{added} new in {period} (period total {len(bucket)})", flush=True)
    return result


def _categories(settings: Settings) -> list[str]:
    return [item.strip().upper() for item in settings.smart_categories.split(",") if item.strip()]


def market_category(question: str, slug: str = "") -> str:
    text = f"{question} {slug}".lower()
    sports_markers = (
        " fc ",
        " cf ",
        " vs.",
        " vs ",
        "-vs-",
        " nba ",
        " nfl ",
        " nhl ",
        " mlb ",
        " epl ",
        " mls ",
        " ufc ",
        " laliga ",
        " serie ",
        " champions league ",
        " playoffs ",
        " esports ",
        " lol:",
        " o/u ",
    )
    if any(marker in f" {text} " for marker in sports_markers):
        return "SPORTS"
    if any(marker in text for marker in ("rain", "snow", "temperature", "hurricane", "weather", "storm")):
        return "WEATHER"
    if any(marker in text for marker in ("trump", "biden", "election", "senate", "congress", "president", "politic")):
        return "POLITICS"
    if any(marker in text for marker in ("fed", "rate", "inflation", "cpi", "unemployment", "gdp", "recession")):
        return "ECONOMICS"
    if any(marker in text for marker in ("stock", "nasdaq", "s&p", "earnings", "ipo", "bank", "bitcoin", "ethereum")):
        return "FINANCE"
    if any(marker in text for marker in ("movie", "box office", "album", "grammy", "oscar", "streaming")):
        return "CULTURE"
    return "OTHER"


def _category_bonus(category: str, settings: Settings) -> float:
    if category == "SPORTS":
        return -settings.smart_sports_score_penalty
    if category in {"WEATHER", "POLITICS", "ECONOMICS", "FINANCE", "CULTURE"}:
        return settings.smart_priority_category_bonus
    return 0.0


def _fresh_signal_bonus(age_minutes: float | None, settings: Settings) -> float:
    if age_minutes is None or settings.smart_fresh_signal_bonus <= 0:
        return 0.0
    if age_minutes <= 2:
        return settings.smart_fresh_signal_bonus
    if age_minutes <= 10:
        return settings.smart_fresh_signal_bonus * 0.6
    if age_minutes <= 30:
        return settings.smart_fresh_signal_bonus * 0.25
    return 0.0


def _is_crypto_micro(candidate: Candidate) -> bool:
    text = f"{candidate.question} {candidate.slug}".lower()
    return ("bitcoin up or down" in text or "ethereum up or down" in text or "btc-updown" in text or "eth-updown" in text)


def _is_crypto_market(candidate: Candidate) -> bool:
    text = f"{candidate.question} {candidate.slug}".lower()
    markers = (
        "bitcoin",
        "btc",
        "ethereum",
        "ether",
        " eth ",
        "eth-",
        "solana",
        " sol ",
        "xrp",
        "dogecoin",
        "doge",
        "crypto",
    )
    return any(marker in text for marker in markers)


def _crypto_signal_allowed(candidate: Candidate, settings: Settings, consensus: int, copied_usdc: float) -> bool:
    if not settings.smart_allow_crypto:
        return False
    hours = candidate.hours_to_close
    if hours is None:
        return False
    if candidate.best_ask is None or candidate.best_ask < settings.smart_crypto_min_buy_price:
        return False
    return (
        settings.smart_crypto_min_hours_to_close <= hours <= settings.smart_crypto_max_hours_to_close
        and consensus >= settings.smart_crypto_min_consensus
        and copied_usdc >= settings.smart_crypto_min_copied_usdc
    )


def _short_horizon_bonus(hours_to_close: float | None) -> float:
    if hours_to_close is None:
        return 0.0
    if hours_to_close <= 1:
        return 8.0
    if hours_to_close <= 6:
        return 6.0
    if hours_to_close <= 24:
        return 4.0
    if hours_to_close <= 72:
        return 1.0
    return -min((hours_to_close - 72.0) / 24.0, 5.0)


def _value_pct(avg_copy_price: float, current_ask: float | None) -> float:
    if avg_copy_price <= 0.0 or current_ask is None:
        return 0.0
    return (avg_copy_price - current_ask) / avg_copy_price


def _value_score(avg_copy_price: float, current_ask: float | None, copied_usdc: float) -> float:
    value_pct = _value_pct(avg_copy_price, current_ask)
    if value_pct > 0:
        flow_quality = min(max(copied_usdc, 0.0) / 250.0, 1.0)
        longshot_bonus = 4.0 if current_ask is not None and current_ask <= 0.15 and copied_usdc >= 20.0 else 0.0
        return min(value_pct * 30.0, 18.0) * flow_quality + longshot_bonus
    return -min(abs(value_pct) * 18.0, 12.0)


def _format_hours(hours_to_close: float | None) -> str:
    if hours_to_close is None:
        return "unknown"
    if hours_to_close < 1:
        return f"{round(hours_to_close * 60)}m"
    return f"{hours_to_close:.1f}h"


def _format_minutes(minutes: float | None) -> str:
    if minutes is None:
        return "unknown"
    if minutes < 1:
        return "<1m"
    return f"{minutes:.0f}m"


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
