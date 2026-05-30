"""Shared dataclasses and parsing helpers.

Hosts the :class:`Candidate` market record consumed by the strategy layer,
plus the timezone-aware datetime parsing helpers used throughout the bot
(Polymarket payloads mix ISO-8601 strings with and without trailing ``Z``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Always return tz-aware (UTC). Without this, downstream subtraction
    # against utc_now() throws "can't subtract offset-naive and
    # offset-aware datetimes" when the source string has no timezone.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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


_EXCLUDED_QUESTION_SUBSTRINGS = (
    "up or down",
    # Temperature/weather threshold markets: specific-degree weather fails
    # constantly even at 0.94 — 0% win rate in grinder band. Both °C and °F.
    "temperature",
    "°c",
    "°f",
    # Exact-score live sports: "No" at 0.94 gaps to 0.44 in one tick when a
    # goal is scored — the -15% SL cannot catch the gap. -$8.73 in one trade.
    "exact score",
    # Halftime leading/score markets: resolves in an instant when a goal is
    # scored → same gap risk. "Andorra leading at halftime" cost -$61.86.
    "leading at halftime",
    "score at halftime",
    "halftime score",
    "half-time score",
    "leading at half",
    # O/U low-line soccer: 1-3 goals flip the bet. Any single goal is catastrophic.
    # Only O/U 4.5 passes — needs 5+ goals to lose, rare enough to be near-certain.
    "o/u 0.5",
    "o/u 1.5",
    "o/u 2.5",
    "o/u 3.5",
    # O/U 5.5+ high-line soccer: Under-5.5 "resolved" as loser if 6+ goals
    # scored — rare but catastrophic when it happens (-$29.96 in dry run).
    "o/u 5.5",
    "o/u 6.5",
    "o/u 7.5",
    # Spread/handicap markets: gap risk identical to exact-score.
    # A single goal swings AH spreads by 0.40+ in one tick, SL can't catch it.
    # "Spread:" prefix covers all Asian handicap markets on Polymarket.
    "spread:",
    # Draw markets: binary coin-flip at 0.15–0.30 that spikes to 0.90+ when
    # score is 0-0 late, then gaps to 0 on any goal. Same gap profile.
    "end in a draw",
    "win or draw",
)
_EXCLUDED_SLUG_SUBSTRINGS = ("updown", "up-or-down", "exact-score")


def is_excluded_market(market: dict[str, Any]) -> bool:
    """True for market types blanket-excluded from every strategy.

    Blocked categories:
    - Crypto Up/Down binaries: no real book depth, FOK orders bounce.
    - Temperature/weather threshold markets (°C and °F): specific-degree
      weather fails constantly even at 0.94.
    - Exact-score live sports: gaps on goals, SL can't catch them.
    - O/U 0.5 soccer: any-goal binary, same gap risk.
    """
    q = str(market.get("question") or "").lower()
    if any(pat in q for pat in _EXCLUDED_QUESTION_SUBSTRINGS):
        return True
    slug = str(market.get("slug") or "").lower()
    if any(pat in slug for pat in _EXCLUDED_SLUG_SUBSTRINGS):
        return True
    return False


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
