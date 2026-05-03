from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value:
        return []
    try:
        parsed = __import__("json").loads(value)
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


@dataclass(frozen=True)
class Candidate:
    market_id: str
    question: str
    slug: str
    end_date: datetime | None
    hours_to_close: float | None
    liquidity: float
    volume: float
    outcome: str
    price: float
    token_id: str | None
    score: float
    url: str
    best_bid: float | None = None
    best_ask: float | None = None
    tick_size: float | None = None
    neg_risk: bool = False
    accepts_orders: bool = False
    event_slug: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "question": self.question,
            "slug": self.slug,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "hours_to_close": self.hours_to_close,
            "liquidity": self.liquidity,
            "volume": self.volume,
            "outcome": self.outcome,
            "price": self.price,
            "token_id": self.token_id,
            "score": self.score,
            "url": self.url,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "tick_size": self.tick_size,
            "neg_risk": self.neg_risk,
            "accepts_orders": self.accepts_orders,
            "event_slug": self.event_slug,
        }
