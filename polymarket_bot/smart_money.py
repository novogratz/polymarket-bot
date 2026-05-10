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
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .models import Candidate


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
    fast_market_bonus: float = 0.0

    @property
    def score(self) -> float:
        pnl_bonus = min(self.total_trader_pnl / 1000.0, 15.0)
        value = _value_score(self.avg_copy_price, self.candidate.best_ask, self.copied_usdc)
        return (
            (self.consensus * 10.0)
            + min(self.copied_usdc / 10.0, 25.0)
            + pnl_bonus
            + _short_horizon_bonus(self.candidate.hours_to_close)
            + self.fast_market_bonus
            + value
            + self.category_bonus
            + self.fresh_bonus
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
        selection_reason = (
            f"{self.consensus} profitable wallets bought this same token recently, "
            f"copying ${self.copied_usdc:.2f} total at avg {self.avg_copy_price:.4f}; "
            f"current ask {self.candidate.best_ask} has spread {self.spread:.4f}, "
            f"closes in {_format_hours(self.candidate.hours_to_close)}, "
            f"latest copied buy {_format_minutes(self.latest_trade_age_minutes)} ago{value_text}, "
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
                "fast_market_bonus": round(self.fast_market_bonus, 4),
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

    def positions(self, *, user: str, limit: int = 500) -> list[dict[str, Any]]:
        payload = self._get_json(
            "/positions",
            {
                "user": user,
                "limit": str(limit),
                "sizeThreshold": "0",
            },
        )
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    def _get_json(self, path: str, params: dict[str, str]) -> Any:
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(
            f"{self.base_url}{path}?{query}",
            headers={
                "Accept": "application/json",
                "User-Agent": "polymarket-bot/0.1",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))


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


def fetch_smart_money_data(
    settings: Settings,
    *,
    client: DataApiClient | None = None,
) -> SmartMoneyData:
    client = client or DataApiClient(settings.data_api_base_url)
    try:
        traders = _top_traders(client, settings)
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

    qualified = sorted((t for t in traders if _qualifies(t)), key=lambda trader: trader.pnl, reverse=True)
    if settings.smart_max_traders > 0:
        before_limit = len(qualified)
        qualified = qualified[: settings.smart_max_traders]
        if before_limit > len(qualified) and not settings.quiet:
            print(
                f"      limiting trade fetch to top {len(qualified)}/{before_limit} qualified trader(s) by PnL",
                flush=True,
            )
    if qualified and not settings.quiet:
        print(
            f"      pulling trades for {len(qualified)} qualified trader(s)"
            f" (concurrency={max(1, settings.smart_trade_fetch_concurrency)})...",
            flush=True,
        )
    start = int(time.time()) - (settings.smart_trade_lookback_minutes * 60)
    trades: list[SmartTrade] = []
    traders_used = 0
    concurrency = max(1, settings.smart_trade_fetch_concurrency)

    def _pull(trader: SmartTrader) -> list[SmartTrade]:
        try:
            return client.trades(user=trader.wallet, start=start)
        except Exception:
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
    return SmartMoneyData(
        traders=traders,
        trades=trades,
        pnl_by_wallet=pnl_by_wallet,
        traders_used=traders_used,
    )


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
        max_hours_to_close = _entry_max_hours_to_close(settings)
        if candidate.hours_to_close is None:
            rejected["unknown_expiry"] = rejected.get("unknown_expiry", 0) + 1
            continue
        if candidate.hours_to_close < settings.smart_min_hours_to_close:
            rejected["too_close_to_expiry"] = rejected.get("too_close_to_expiry", 0) + 1
            continue
        if max_hours_to_close > 0 and candidate.hours_to_close > max_hours_to_close:
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

        total_trader_pnl = sum(pnl_by_wallet.get(w.lower(), 0.0) for w in wallets) if pnl_by_wallet else 0.0
        copied_usdc = round(sum(trade.usdc_size for trade in token_trades), 2)
        if copied_usdc < settings.smart_min_copied_usdc:
            rejected["copied_usdc_too_small"] = rejected.get("copied_usdc_too_small", 0) + 1
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
                fast_market_bonus=_fast_market_bonus(candidate.hours_to_close, category, min_consensus, settings),
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


def _entry_max_hours_to_close(settings: Settings) -> float:
    caps = [
        cap
        for cap in (
            float(settings.smart_max_hours_to_close or 0.0),
            168.0,
        )
        if cap > 0
    ]
    return min(caps) if caps else 0.0


def _time_periods(settings: Settings) -> list[str]:
    raw = settings.smart_time_periods.strip()
    if raw:
        periods = [item.strip().upper() for item in raw.split(",") if item.strip()]
        if periods:
            return periods
    return [settings.smart_time_period.strip().upper() or "WEEK"]


def _top_traders(client: DataApiClient, settings: Settings) -> list[SmartTrader]:
    seen: set[str] = set()
    traders: list[SmartTrader] = []
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
        added = 0
        for trader in category_traders:
            if trader.wallet.lower() in seen:
                continue
            seen.add(trader.wallet.lower())
            traders.append(trader)
            added += 1
        if not settings.quiet:
            print(f"         +{added} new (total {len(traders)})", flush=True)
    return traders


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


def _fast_market_bonus(hours_to_close: float | None, category: str, min_consensus: int, settings: Settings) -> float:
    if hours_to_close is None or hours_to_close <= 0:
        return 0.0
    if settings.smart_fast_market_score_bonus <= 0 or hours_to_close > settings.smart_fast_market_max_hours:
        return 0.0
    if category == "SPORTS" and hours_to_close < 0.5:
        return 0.0
    return settings.smart_fast_market_score_bonus if min_consensus >= 2 else 0.0


def _value_pct(avg_copy_price: float, current_ask: float | None) -> float:
    if avg_copy_price <= 0.0 or current_ask is None:
        return 0.0
    return (avg_copy_price - current_ask) / avg_copy_price


def _value_score(avg_copy_price: float, current_ask: float | None, copied_usdc: float) -> float:
    value_pct = _value_pct(avg_copy_price, current_ask)
    if value_pct > 0:
        flow_quality = min(max(copied_usdc, 0.0) / 50.0, 1.0)
        longshot_bonus = 4.0 if current_ask is not None and current_ask <= 0.15 and copied_usdc >= 10.0 else 0.0
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
