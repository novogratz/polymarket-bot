from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import stdev
from typing import Any

from .config import Settings
from .models import Candidate, utc_now


COINBASE_EXCHANGE_URL = "https://api.exchange.coinbase.com"
COINBASE_FALLBACK_SPOT_URL = "https://api.coinbase.com"


@dataclass(frozen=True)
class BtcModel:
    spot: float
    annual_volatility: float
    fetched_at: datetime


@dataclass(frozen=True)
class BtcSignal:
    candidate: Candidate
    fair_probability: float
    edge: float
    side: str
    strike: float
    spot: float
    annual_volatility: float
    hours_to_expiry: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_id": self.candidate.market_id,
            "question": self.candidate.question,
            "outcome": self.candidate.outcome,
            "side": self.side,
            "strike": self.strike,
            "spot": self.spot,
            "annual_volatility": self.annual_volatility,
            "hours_to_expiry": self.hours_to_expiry,
            "fair_probability": self.fair_probability,
            "best_ask": self.candidate.best_ask,
            "best_bid": self.candidate.best_bid,
            "edge": self.edge,
            "url": self.candidate.url,
        }


class CoinbaseBtcClient:
    def __init__(
        self,
        base_url: str = COINBASE_EXCHANGE_URL,
        fallback_url: str = COINBASE_FALLBACK_SPOT_URL,
        timeout: int = 15,
        max_retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.fallback_url = fallback_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries

    def spot(self) -> float:
        try:
            payload = self._get_json(self.base_url, "/products/BTC-USD/ticker")
            return float(payload["price"])
        except Exception:
            payload = self._get_json(self.fallback_url, "/v2/prices/BTC-USD/spot")
            return float(payload["data"]["amount"])

    def annualized_volatility(self, *, days: int) -> float:
        end = utc_now()
        start = end - timedelta(days=days)
        params = urllib.parse.urlencode(
            {
                "granularity": "3600",
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
        )
        try:
            candles = self._get_json(self.base_url, f"/products/BTC-USD/candles?{params}")
        except Exception:
            return 0.60
        closes = [float(item[4]) for item in sorted(candles, key=lambda row: row[0]) if len(item) >= 5]
        returns = [math.log(closes[index] / closes[index - 1]) for index in range(1, len(closes)) if closes[index - 1] > 0]
        if len(returns) < 24:
            return 0.60
        hourly_vol = stdev(returns)
        return max(0.20, min(hourly_vol * math.sqrt(24 * 365), 2.50))

    def model(self, settings: Settings) -> BtcModel:
        return BtcModel(
            spot=self.spot(),
            annual_volatility=self.annualized_volatility(days=settings.btc_volatility_days),
            fetched_at=utc_now(),
        )

    def _get_json(self, base_url: str, path: str) -> Any:
        last_exc: Exception | None = None
        for attempt in range(max(1, self.max_retries)):
            try:
                request = urllib.request.Request(
                    f"{base_url}{path}",
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "polymarket-bot/0.1",
                    },
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last_exc = exc
                if exc.code in (502, 503, 504, 429) and attempt + 1 < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                raise
            except Exception as exc:
                last_exc = exc
                if attempt + 1 < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("coinbase client exhausted retries without response")


def choose_btc_edge_trade(
    candidates: list[Candidate],
    settings: Settings,
    model: BtcModel,
) -> BtcSignal | None:
    signals = [
        signal
        for candidate in candidates
        if (signal := btc_signal(candidate, settings, model)) is not None
    ]
    return max(signals, key=lambda item: item.edge, default=None)


def btc_signal(candidate: Candidate, settings: Settings, model: BtcModel) -> BtcSignal | None:
    if not candidate.accepts_orders or not candidate.token_id:
        return None
    if candidate.best_ask is None or candidate.best_bid is None or candidate.tick_size is None:
        return None
    if candidate.best_ask <= 0 or candidate.best_ask >= 1:
        return None
    spread = candidate.best_ask - candidate.best_bid
    if spread < 0 or spread > settings.btc_max_spread:
        return None
    if candidate.best_ask < settings.btc_min_buy_price or candidate.best_ask > settings.btc_max_buy_price:
        return None
    if not _is_yes(candidate):
        return None

    threshold = parse_btc_threshold(candidate.question)
    if threshold is None:
        threshold = parse_btc_threshold(candidate.slug.replace("-", " "))
    if threshold is None or candidate.end_date is None:
        return None

    direction, strike = threshold
    hours = max((candidate.end_date - utc_now()).total_seconds() / 3600.0, 0.0)
    if hours <= 0:
        return None

    probability = btc_terminal_probability(
        spot=model.spot,
        strike=strike,
        hours=hours,
        annual_volatility=model.annual_volatility,
        direction=direction,
    )
    edge = probability - candidate.best_ask
    if probability < settings.btc_min_model_probability or edge < settings.btc_min_edge:
        return None

    return BtcSignal(
        candidate=candidate,
        fair_probability=probability,
        edge=edge,
        side="BUY",
        strike=strike,
        spot=model.spot,
        annual_volatility=model.annual_volatility,
        hours_to_expiry=hours,
    )


def parse_btc_threshold(text: str) -> tuple[str, float] | None:
    normalized = text.lower().replace(",", "")
    if "bitcoin" not in normalized and "btc" not in normalized:
        return None
    if any(term in normalized for term in ("between", "range", "touch", "hit", "reach")):
        return None

    match = re.search(
        r"(above|over|greater than|below|under|less than)\s+\$?\s*([0-9]+(?:\.[0-9]+)?)\s*([kKmM]?)",
        normalized,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None

    direction_text = match.group(1).lower()
    multiplier = {"": 1.0, "k": 1_000.0, "K": 1_000.0, "m": 1_000_000.0, "M": 1_000_000.0}[match.group(3)]
    strike = float(match.group(2)) * multiplier
    direction = "above" if direction_text in {"above", "over", "greater than"} else "below"
    return direction, strike


def btc_terminal_probability(
    *,
    spot: float,
    strike: float,
    hours: float,
    annual_volatility: float,
    direction: str,
) -> float:
    if spot <= 0 or strike <= 0 or hours <= 0 or annual_volatility <= 0:
        return 0.0
    years = hours / (24 * 365)
    sigma_t = annual_volatility * math.sqrt(years)
    if sigma_t <= 0:
        probability_above = 1.0 if spot > strike else 0.0
    else:
        z = (math.log(spot / strike) - 0.5 * annual_volatility * annual_volatility * years) / sigma_t
        probability_above = _normal_cdf(z)
    return probability_above if direction == "above" else 1.0 - probability_above


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _is_yes(candidate: Candidate) -> bool:
    return candidate.outcome.strip().lower() in {"yes", "y"}
