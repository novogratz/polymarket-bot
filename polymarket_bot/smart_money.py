from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
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

    @property
    def score(self) -> float:
        # Score includes consensus, size, and now quality (weighted PnL)
        pnl_bonus = min(self.total_trader_pnl / 1000.0, 15.0)  # Up to 15 points for high PnL wallets
        return (self.consensus * 10.0) + min(self.copied_usdc / 10.0, 25.0) + pnl_bonus - (self.candidate.best_ask or 0.0)

    def to_dict(self) -> dict[str, Any]:
        price_distance = (
            round((self.candidate.best_ask or 0.0) - self.avg_copy_price, 4)
            if self.avg_copy_price > 0.0 and self.candidate.best_ask is not None
            else None
        )
        selection_reason = (
            f"{self.consensus} profitable wallets bought this same token recently, "
            f"copying ${self.copied_usdc:.2f} total at avg {self.avg_copy_price:.4f}; "
            f"current ask {self.candidate.best_ask} has spread {self.spread:.4f} "
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
                "ask_minus_avg_copy_price": price_distance,
                "total_trader_pnl": round(self.total_trader_pnl, 2),
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

    def trades(self, *, user: str, start: int, limit: int = 100) -> list[SmartTrade]:
        payload = self._get_json(
            "/trades",
            {
                "user": user,
                "side": "BUY",
                "start": str(start),
                "limit": str(limit),
                "takerOnly": "true",
            },
        )
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


def analyze_smart_money(
    candidates: list[Candidate],
    settings: Settings,
    *,
    client: DataApiClient | None = None,
) -> SmartMoneyReport:
    client = client or DataApiClient(settings.data_api_base_url)
    traders = _top_traders(client, settings)
    pnl_by_wallet = {t.wallet.lower(): t.pnl for t in traders}
    
    start = int(time.time()) - (settings.smart_trade_lookback_minutes * 60)
    trades: list[SmartTrade] = []
    traders_used = 0
    for trader in traders:
        if trader.pnl < settings.smart_min_trader_pnl:
            continue
        traders_used += 1
        trades.extend(client.trades(user=trader.wallet, start=start))
    signals, details = smart_money_signals(candidates, trades, settings, pnl_by_wallet=pnl_by_wallet, include_details=True)
    opportunities = sorted(signals, key=lambda item: item.score, reverse=True)
    return SmartMoneyReport(
        selected=opportunities[0] if opportunities else None,
        opportunities=opportunities,
        traders_checked=len(traders),
        traders_used=traders_used,
        trades_checked=len(trades),
        eligible_trade_count=int(details["eligible_trade_count"]),
        grouped_tokens=int(details["grouped_tokens"]),
        matched_tokens=int(details["matched_tokens"]),
        rejected=dict(details["rejected"]),
    )


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
        if not candidate.accepts_orders or candidate.tick_size is None:
            rejected["not_accepting_orders"] = rejected.get("not_accepting_orders", 0) + 1
            continue
        spread = candidate.best_ask - candidate.best_bid
        if spread < 0 or spread > settings.smart_max_spread:
            rejected["spread_too_wide"] = rejected.get("spread_too_wide", 0) + 1
            continue
        if candidate.best_ask < settings.smart_min_buy_price or candidate.best_ask > settings.smart_max_buy_price:
            rejected["ask_outside_price_band"] = rejected.get("ask_outside_price_band", 0) + 1
            continue

        wallets = sorted({trade.wallet for trade in token_trades})
        min_consensus = max(2, settings.smart_min_consensus)
        if len(wallets) < min_consensus:
            rejected["not_enough_wallet_consensus"] = rejected.get("not_enough_wallet_consensus", 0) + 1
            continue

        total_trader_pnl = sum(pnl_by_wallet.get(w.lower(), 0.0) for w in wallets) if pnl_by_wallet else 0.0
        copied_usdc = round(sum(trade.usdc_size for trade in token_trades), 2)
        total_size = sum(trade.size for trade in token_trades)
        avg_price = round(sum(trade.price * trade.size for trade in token_trades) / total_size, 4) if total_size else 0.0
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


def _top_traders(client: DataApiClient, settings: Settings) -> list[SmartTrader]:
    seen: set[str] = set()
    traders: list[SmartTrader] = []
    for category in _categories(settings):
        for trader in client.leaderboard(
            category=category,
            time_period=settings.smart_time_period,
            limit=settings.smart_leaderboard_limit,
        ):
            if trader.wallet.lower() in seen:
                continue
            seen.add(trader.wallet.lower())
            traders.append(trader)
    return traders


def _categories(settings: Settings) -> list[str]:
    return [item.strip().upper() for item in settings.smart_categories.split(",") if item.strip()]


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
