"""External spot-price fetcher for the edge strategy.

Lightweight client for Binance public REST endpoints. Used by the
``edge_strategy`` crypto-directional lane to compute a fair probability
on short-expiry "Up or Down" markets.

No authentication required. We hit two endpoints:

- ``/api/v3/ticker/price`` — current spot, one call per symbol.
- ``/api/v3/klines`` — recent candles for momentum estimation.

Both are best-effort: any failure returns ``None`` (price) or ``0.0``
(momentum) so the strategy falls back to "no signal" rather than
crashing. We do NOT hammer the API — one call per symbol per tick at
most, and the strategy caches results within the tick.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

_BINANCE_BASE = "https://api.binance.com"
_USER_AGENT = "polymarket-bot/0.1"

# Map of canonical asset key → Binance trading pair.
ASSET_TO_SYMBOL: dict[str, str] = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
    "DOGE": "DOGEUSDT",
    "ADA": "ADAUSDT",
    "AVAX": "AVAXUSDT",
    "MATIC": "MATICUSDT",
    "LINK": "LINKUSDT",
    "DOT": "DOTUSDT",
}


@dataclass(frozen=True)
class SpotQuote:
    """Snapshot of an external spot price + recent momentum."""

    symbol: str
    price: float
    momentum_5m: float  # % change over the last 5 minutes
    momentum_15m: float  # % change over the last 15 minutes
    fetched_at: float


def _http_get_json(url: str, timeout: int = 5) -> Any:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": _USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_spot_price(symbol: str, *, timeout: int = 5) -> float | None:
    """One spot price (USDT pair). Returns ``None`` on any error."""
    params = urllib.parse.urlencode({"symbol": symbol})
    try:
        payload = _http_get_json(f"{_BINANCE_BASE}/api/v3/ticker/price?{params}", timeout=timeout)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return float(payload.get("price", 0.0) or 0.0) or None
    except (TypeError, ValueError):
        return None


def fetch_recent_momentum(symbol: str, *, timeout: int = 5) -> tuple[float, float]:
    """Return (5min % change, 15min % change) from Binance 1m klines.

    Both are signed fractions (e.g. ``0.012`` = +1.2%). On any failure
    both values come back as 0.0 so callers can treat absence of
    momentum identically to "no edge".
    """
    params = urllib.parse.urlencode({"symbol": symbol, "interval": "1m", "limit": 16})
    try:
        payload = _http_get_json(f"{_BINANCE_BASE}/api/v3/klines?{params}", timeout=timeout)
    except Exception:
        return 0.0, 0.0
    if not isinstance(payload, list) or len(payload) < 6:
        return 0.0, 0.0
    try:
        # Kline tuple: [open_time, open, high, low, close, ...]
        last_close = float(payload[-1][4])
        close_5m_ago = float(payload[-6][4])
        # 15m: pick the bar furthest back we have (cap at 15 bars ago).
        anchor_idx = max(0, len(payload) - 16)
        close_15m_ago = float(payload[anchor_idx][4])
    except (TypeError, ValueError, IndexError):
        return 0.0, 0.0
    if close_5m_ago <= 0 or close_15m_ago <= 0:
        return 0.0, 0.0
    mom_5 = (last_close - close_5m_ago) / close_5m_ago
    mom_15 = (last_close - close_15m_ago) / close_15m_ago
    return mom_5, mom_15


def fetch_spot_quote(symbol: str) -> SpotQuote | None:
    """Combine spot + momentum into one snapshot.

    Two HTTP calls per symbol. Caller should batch and cache.
    """
    price = fetch_spot_price(symbol)
    if price is None or price <= 0:
        return None
    mom_5, mom_15 = fetch_recent_momentum(symbol)
    return SpotQuote(
        symbol=symbol,
        price=price,
        momentum_5m=mom_5,
        momentum_15m=mom_15,
        fetched_at=time.time(),
    )


def fetch_spot_quotes_for_assets(assets: list[str]) -> dict[str, SpotQuote]:
    """Bulk fetch quotes keyed by canonical asset key (BTC, ETH, ...)."""
    out: dict[str, SpotQuote] = {}
    for asset in assets:
        symbol = ASSET_TO_SYMBOL.get(asset)
        if not symbol:
            continue
        quote = fetch_spot_quote(symbol)
        if quote is not None:
            out[asset] = quote
    return out
