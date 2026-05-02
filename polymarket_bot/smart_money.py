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

    @property
    def score(self) -> float:
        return (self.consensus * 10.0) + min(self.copied_usdc / 10.0, 25.0) - (self.candidate.best_ask or 0.0)

    def to_dict(self) -> dict[str, Any]:
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
            "url": self.candidate.url,
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
    client = client or DataApiClient(settings.data_api_base_url)
    traders = _top_traders(client, settings)
    start = int(time.time()) - (settings.smart_trade_lookback_minutes * 60)
    trades: list[SmartTrade] = []
    for trader in traders:
        if trader.pnl < settings.smart_min_trader_pnl:
            continue
        trades.extend(client.trades(user=trader.wallet, start=start))
    signals = smart_money_signals(candidates, trades, settings)
    return max(signals, key=lambda item: item.score, default=None)


def smart_money_signals(
    candidates: list[Candidate],
    trades: list[SmartTrade],
    settings: Settings,
) -> list[SmartMoneySignal]:
    by_token = {candidate.token_id: candidate for candidate in candidates if candidate.token_id}
    grouped: dict[str, list[SmartTrade]] = {}
    for trade in trades:
        if trade.side.upper() != "BUY" or trade.usdc_size < settings.smart_min_trade_usd:
            continue
        grouped.setdefault(trade.asset, []).append(trade)

    signals: list[SmartMoneySignal] = []
    for token_id, token_trades in grouped.items():
        candidate = by_token.get(token_id)
        if candidate is None or candidate.best_ask is None or candidate.best_bid is None:
            continue
        if not candidate.accepts_orders or candidate.tick_size is None:
            continue
        spread = candidate.best_ask - candidate.best_bid
        if spread < 0 or spread > settings.smart_max_spread:
            continue
        if candidate.best_ask < settings.smart_min_buy_price or candidate.best_ask > settings.smart_max_buy_price:
            continue

        wallets = sorted({trade.wallet for trade in token_trades})
        if len(wallets) < settings.smart_min_consensus:
            continue

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
            )
        )
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
