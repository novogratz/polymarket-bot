"""Shared dataclasses and parsing helpers.

Hosts the :class:`Candidate` market record consumed by the strategy layer,
plus the timezone-aware datetime parsing helpers used throughout the bot
(Polymarket payloads mix ISO-8601 strings with and without trailing ``Z``).
"""

from __future__ import annotations

import re
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
    # ALL crypto markets banned (2026-06-03) — Up/Down, price thresholds, and
    # any coin market. btc_edge lane also disabled in the profiles.
    "up or down",
    "bitcoin",
    "btc",
    "ethereum",
    "solana",
    "dogecoin",
    "xrp",
    "cardano",
    "litecoin",
    "crypto",
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
    # O/U low-line soccer: 1-3 goals flip the bet instantly.
    "o/u 0.5",
    "o/u 1.5",
    "o/u 2.5",
    "o/u 3.5",
    # O/U 5.5+ high-line: rare but catastrophic if 6+ goals scored.
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
_EXCLUDED_SLUG_SUBSTRINGS = (
    "updown",
    "up-or-down",
    "exact-score",
    # Crypto slug markers (all crypto banned 2026-06-03).
    "bitcoin",
    "btc",
    "ethereum",
    "solana",
    "crypto",
)

# ── Esports — LIVE GAMES ONLY (2026-06-12, was a blanket ban) ─────────────
# Blanket-banned 2026-05-31 (thin/volatile pre-game books; the FENNEL LoL
# buy). User re-allowed them 2026-06-12 ON ONE CONDITION: the game must be
# IN PROGRESS (gameStartTime in the past) — an in-play favorite is converging
# to resolution. Pre-game, missing gameStartTime, or a start so old the
# series must be over (> _ESPORTS_LIVE_MAX_HOURS) stays excluded.
_ESPORTS_QUESTION_SUBSTRINGS = (
    "counter-strike",
    "esports",
    "valorant",
    "league of legends",
    # "LoL:" title prefix — Polymarket LoL markets are titled
    # "LoL: <team> vs <team> - Game N Winner", not "League of Legends".
    "lol:",
    "dota",
    "cs2",
    "csgo",
    "rainbow six",
    "rocket league",
    "overwatch",
    "(bo1)",
    "(bo3)",
    "(bo5)",
)
_ESPORTS_SLUG_SUBSTRINGS = (
    "counter-strike",
    "csgo",
    "cs2",
    "valorant",
    "league-of-legends",
    "lol-",
    "dota",
    "esports",
)
_ESPORTS_LIVE_MAX_HOURS = 8.0

# ── Stock market — ONGOING TRADING SESSION ONLY (2026-06-12) ─────────────
# Blanket-banned 2026-06-11 after the SPY buy. User re-allowed 2026-06-12 ON
# ONE CONDITION: only during the ongoing day's regular NYSE session
# (Mon–Fri 09:30–16:00 ET) and only for THAT day's close (market end within
# _STOCK_SAME_DAY_MAX_HOURS) — an in-session same-day close converges to the
# 16:00 print. Overnight, weekends, and multi-day stock bets stay excluded.
_STOCK_QUESTION_SUBSTRINGS = (
    "s&p",
    "dow jones",
    "russell 2000",
    "stock market",
    "stock price",
    "share price",
    "market cap",
    "wall street",
    # Price-threshold close markets ("X closes above $725 on June 10?") —
    # the stock/index/commodity pattern; crypto is banned outright above.
    "closes above $",
    "close above $",
    "closes below $",
    "close below $",
)
_STOCK_SLUG_SUBSTRINGS = (
    "stock-market",
    "sp500",
    "s-and-p",
)
_STOCK_SAME_DAY_MAX_HOURS = 12.0

# Stock tickers and company names need word boundaries — plain substrings
# would false-positive ("spy" in "spying", "meta" in "metal"). Lowercased
# question AND slug are both checked (hyphens count as word breaks).
_STOCK_MARKET_RE = re.compile(
    r"\b(?:"
    r"spy|qqq|voo|djia|nasdaq|nikkei|ftse|dax"
    r"|googl?|aapl|tsla|nvda|msft|amzn|nflx|amd|intc"
    r"|google|alphabet|apple|tesla|nvidia|microsoft|amazon|netflix|meta"
    r")\b"
)


def _parse_market_dt(raw: Any) -> datetime | None:
    s = str(raw or "").strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00").replace(" ", "T")
    if s.endswith("+00"):
        s += ":00"
    try:
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except ValueError:
        return None


def _esports_game_is_live(market: dict[str, Any], now: datetime) -> bool:
    """True only while the game/series is actually IN PROGRESS."""
    start = _parse_market_dt(market.get("gameStartTime"))
    if start is None:
        return False
    hours_running = (now - start).total_seconds() / 3600.0
    return 0.0 <= hours_running <= _ESPORTS_LIVE_MAX_HOURS


def _stock_session_is_ongoing(market: dict[str, Any], now: datetime) -> bool:
    """True only during the regular NYSE session AND for that day's close."""
    try:
        from zoneinfo import ZoneInfo
        et = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return False
    if et.weekday() >= 5:  # Sat/Sun
        return False
    minutes = et.hour * 60 + et.minute
    if not (9 * 60 + 30 <= minutes < 16 * 60):  # 09:30-16:00 ET
        return False
    end = _parse_market_dt(market.get("endDate"))
    if end is None:
        return False
    hours_to_end = (end - now).total_seconds() / 3600.0
    return 0.0 <= hours_to_end <= _STOCK_SAME_DAY_MAX_HOURS


def is_excluded_market(market: dict[str, Any], now: datetime | None = None) -> bool:
    """True for market types excluded from every strategy.

    Blocked categories:
    - Crypto Up/Down binaries: no real book depth, FOK orders bounce.
    - Temperature/weather threshold markets (°C and °F): specific-degree
      weather fails constantly even at 0.94.
    - Exact-score live sports: gaps on goals, SL can't catch them.
    - O/U 0.5 soccer: any-goal binary, same gap risk.

    Conditionally allowed (2026-06-12, user rule — "ongoing only"):
    - Esports: tradeable ONLY while the game is live (gameStartTime in the
      past, within _ESPORTS_LIVE_MAX_HOURS). Pre-game or unknown start ->
      excluded.
    - Stock market / equities: tradeable ONLY during the ongoing regular
      NYSE session (Mon-Fri 09:30-16:00 ET) and only for that day's close.
      Overnight, weekends, multi-day -> excluded.
    """
    q = str(market.get("question") or "").lower()
    if any(pat in q for pat in _EXCLUDED_QUESTION_SUBSTRINGS):
        return True
    slug = str(market.get("slug") or "").lower()
    if any(pat in slug for pat in _EXCLUDED_SLUG_SUBSTRINGS):
        return True
    if now is None:
        now = datetime.now(timezone.utc)
    is_esports = any(pat in q for pat in _ESPORTS_QUESTION_SUBSTRINGS) or any(
        pat in slug for pat in _ESPORTS_SLUG_SUBSTRINGS
    )
    if is_esports:
        return not _esports_game_is_live(market, now)
    is_stock = (
        any(pat in q for pat in _STOCK_QUESTION_SUBSTRINGS)
        or any(pat in slug for pat in _STOCK_SLUG_SUBSTRINGS)
        or bool(_STOCK_MARKET_RE.search(q))
        or bool(_STOCK_MARKET_RE.search(slug))
    )
    if is_stock:
        return not _stock_session_is_ongoing(market, now)
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
