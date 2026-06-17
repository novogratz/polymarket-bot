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
    "eth ",
    " eth?",
    "solana",
    " sol ",
    "xrp",
    "ripple",
    "dogecoin",
    "doge",
    "cardano",
    "litecoin",
    "polygon",
    "matic",
    "avalanche",
    "avax",
    "chainlink",
    "binance",
    " bnb",
    "crypto",
    "altcoin",
    "defi",
    " nft",
    "blockchain",
    "stablecoin",
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
    # ALL O/U goal-total lines banned (o/u 4.5 added 2026-06-14, data-driven):
    # the loss audit showed O/U 4.5 Unders were 80% of all realized losses
    # ($765 of $960) and the three worst trades ever (Derry -$277, US-Paraguay
    # -$266, FC Lahti -$194 — each bigger than total profit). An Under sits at
    # 0.94 looking safe, then craters to $0 on the goal that crosses the line:
    # textbook gap risk the strategy is meant to avoid. 4.5 was the only O/U
    # line still allowed — now closed.
    "o/u 0.5",
    "o/u 1.5",
    "o/u 2.5",
    "o/u 3.5",
    "o/u 4.5",
    "over/under 4.5",
    "o/u 5.5",
    "o/u 6.5",
    "o/u 7.5",
    # Spread/handicap markets: gap risk identical to exact-score.
    # A single goal swings AH spreads by 0.40+ in one tick, SL can't catch it.
    # "Spread:" prefix covers all Asian handicap markets on Polymarket;
    # "Game Handicap:" is the esports variant that slipped through on
    # 2026-06-12 ("Game Handicap: HLE (-2.5) vs T1 (+2.5)", bought pre-game
    # at 0.889 — banned outright, even for LoL).
    "spread:",
    "game handicap",
    "map handicap",
    # Draw markets: binary coin-flip at 0.15–0.30 that spikes to 0.90+ when
    # score is 0-0 late, then gaps to 0 on any goal. Same gap profile.
    "end in a draw",
    "win or draw",
    # Tweet-count markets (2026-06-12, user rule): "Will Elon Musk post
    # 240-259 tweets from June 5 to June 12?" — a week-long count with no
    # convergence signal; one posting spree flips the bracket. Banned.
    "tweet",
    # YouTube view/subscriber-count markets (2026-06-14, user rule —
    # lost a MrBeast view-count bet).
    "youtube",
    "mrbeast",
    "mr beast",
    # Entertainment / pop-culture ("Divertissement", 2026-06-14, user rule):
    # awards, box office, charts, streaming, and social-metric markets — no
    # convergence edge, they jump on hype. Name-collision-safe terms only
    # (e.g. "academy award"/"best picture" instead of bare "oscar").
    "box office",
    "rotten tomatoes",
    "academy award",
    "best picture",
    "grammy",
    "emmy",
    "golden globe",
    "palme d'or",
    "tony award",
    "billboard",
    "spotify",
    "tiktok",
    "subscribers",
    "followers",
    "streams",
    "netflix",
    "movie",
    "film",
    "album",
    "celebrity",
    # Fed / FOMC interest-rate-decision markets (2026-06-17, user rule — "too
    # far away"). "Fed rate cut by September 2026 meeting?" resolves months out
    # but shows a near-term Gamma endDate, so it slips through the 4h window and
    # the bot kept re-buying it. No short-horizon convergence — banned. Phrases
    # are monetary-policy-specific to avoid colliding with "approval rate" etc.
    "fed rate",
    "rate cut",
    "rate hike",
    "cut rates",
    "hike rates",
    "raise rates",
    "interest rate",
    "fomc",
    "fed funds",
    "rate decision",
    "basis points",
)
_EXCLUDED_SLUG_SUBSTRINGS = (
    "updown",
    "up-or-down",
    "exact-score",
    # Tweet-count slug markers (2026-06-12).
    "-tweets",
    "of-tweets",
    # Handicap slug markers (2026-06-12).
    "game-handicap",
    "map-handicap",
    # O/U 4.5 goal-total slug markers (2026-06-14, data-driven ban).
    "-total-4pt5",
    "-ou-45",
    "ou-4pt5",
    # YouTube view-count slug markers (2026-06-14).
    "youtube",
    "mrbeast",
    "-views-",
    # League of Ireland / Premier Division (Ireland) — banned 2026-06-12
    # (user rule). Every market of that championship carries the "irl1-"
    # slug prefix (e.g. irl1-der-boh-2026-06-12-total-4pt5); the question
    # has no league marker, so the slug is the identifier.
    "irl1-",
    # Crypto slug markers (all crypto banned 2026-06-03).
    "bitcoin",
    "btc",
    "ethereum",
    "solana",
    "dogecoin",
    "ripple",
    "cardano",
    "polygon",
    "avalanche",
    "chainlink",
    "binance",
    "crypto",
)

# ── Esports — BANNED OUTRIGHT (re-banned 2026-06-12, final) ──────────────
# Blanket-banned 2026-05-31 (thin/volatile pre-game books; the FENNEL LoL
# buy); briefly re-allowed live-only then LoL-only on 2026-06-12; banned
# completely again the same day (user: "remove esports completely … block
# counter strike etc"): counter-strike, LoL, dota, valorant, mobile
# legends — never tradeable, in progress or not.

# Esports lane entry floor: dead-letter while the lane is banned outright,
# kept because race_strategies imports it and a held legacy position still
# routes through the fast-lane exit logic.
ESPORTS_MIN_ASK = 0.92

# Per-lane entry floor for soccer/sport "Will <X> win on <date>?" moneylines
# (user 2026-06-17). These gap catastrophically on a single goal: every
# moneyline loss in the realized history entered at ≤ 0.90 (0.85, 0.86, 0.867,
# 0.895, 0.90); the 0.90+ band has ZERO losses across 29 trades. The stop-loss
# cannot protect them — a goal moves the price faster than any tick-based exit
# and the gap often mean-reverts (Difaâ "No" 0.89 → 0.02 → resolved 1.0). The
# real control is at entry: only buy a moneyline favorite already near-settled.
SOCCER_MONEYLINE_MIN_ASK = 0.92
_ESPORTS_QUESTION_SUBSTRINGS = (
    "counter-strike",
    "esports",
    "dota 2",
    "valorant",
    "mobile legends",
    "mlbb",
    "league of legends",
    # "LoL:" title prefix — Polymarket LoL markets are titled
    # "LoL: <team> vs <team> - Game N Winner", not "League of Legends".
    "lol:",
    "dota",
    "league of legends",
    "valorant",
    "overwatch",
    "starcraft",
    "rocket league",
    "rainbow six",
    "cs2",
    "csgo",
    "(bo1)",
    "(bo3)",
    "(bo5)",
    "- bo3",
    "- bo5",
)
_ESPORTS_SLUG_SUBSTRINGS = (
    "counter-strike",
    "csgo",
    "cs2",
    "valorant",
    "league-of-legends",
    "lol-",
    "mobile-legends",
    "mlbb",
    "dota",
    "esports",
)

# ── Stock market — BANNED OUTRIGHT (re-banned 2026-06-12) ────────────────
# Blanket-banned 2026-06-11 after the SPY buy, conditionally re-allowed
# in-session 2026-06-12, then re-banned entirely the same day (user:
# "lets also remove stock market bro everywhere"). All equities, indices,
# ETFs, and price-threshold markets are excluded, always.
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

# Stock tickers and company names need word boundaries — plain substrings
# would false-positive ("spy" in "spying", "meta" in "metal"). Lowercased
# question AND slug are both checked (hyphens count as word breaks).
_STOCK_MARKET_RE = re.compile(
    r"\b(?:"
    r"spy|qqq|voo|djia|nasdaq|nikkei|ftse|dax"
    r"|googl?|aapl|tsla|nvda|msft|amzn|nflx|amd|intc"
    r"|google|alphabet|apple|tesla|nvidia|microsoft|amazon|netflix|meta"
    r"|airbnb|abnb|uber|coinbase|palantir|pltr|robinhood|hood"
    r")\b"
)

# Generic stock-title rule: a parenthesized 2-5 letter UPPERCASE ticker plus
# a dollar amount — "Will Airbnb, Inc. (ABNB) hit (LOW) $124 Week of June 8?"
# slipped past the enumerated tickers (2026-06-12). The $ requirement keeps
# "(GOP)"-style politics and sports titles out.
_PAREN_TICKER_RE = re.compile(r"\([A-Z]{2,5}\)")

# YouTube/social view-count markets (2026-06-14): a standalone "views" word
# (\bviews\b) is the view-count tell — "reviews"/"interviews" do NOT match.
_VIEW_COUNT_RE = re.compile(r"\bviews\b")



def is_esports_text(question: str, slug: str = "") -> bool:
    """True for any esports market (allowed title or not)."""
    q = str(question or "").lower()
    s = str(slug or "").lower()
    return any(pat in q for pat in _ESPORTS_QUESTION_SUBSTRINGS) or any(
        pat in s for pat in _ESPORTS_SLUG_SUBSTRINGS
    )


def is_stock_text(question: str, slug: str = "") -> bool:
    """True for any stock/index/equity market."""
    q = str(question or "").lower()
    s = str(slug or "").lower()
    raw_q = str(question or "")
    return (
        any(pat in q for pat in _STOCK_QUESTION_SUBSTRINGS)
        or any(pat in s for pat in _STOCK_SLUG_SUBSTRINGS)
        or bool(_STOCK_MARKET_RE.search(q))
        or bool(_STOCK_MARKET_RE.search(s))
        or (bool(_PAREN_TICKER_RE.search(raw_q)) and "$" in raw_q)
    )


def is_fast_lane_text(question: str, slug: str = "") -> bool:
    """True for esports and stock/index markets — the 'fast lanes'.

    These in-play / in-session markets resolve on a clock measured in
    minutes-to-hours and their books rarely print a 0.99 bid before the
    market closes; the winner exit for them is 0.98 (user 2026-06-12)
    instead of the standard 0.99.
    """
    return is_esports_text(question, slug) or is_stock_text(question, slug)


def is_excluded_market(market: dict[str, Any], now: datetime | None = None) -> bool:
    """True for market types excluded from every strategy.

    Blocked categories:
    - Crypto Up/Down binaries: no real book depth, FOK orders bounce.
    - Temperature/weather threshold markets (°C and °F): specific-degree
      weather fails constantly even at 0.94.
    - Exact-score live sports: gaps on goals, SL can't catch them.
    - O/U 0.5 soccer: any-goal binary, same gap risk.

    Esports (counter-strike, LoL, dota, valorant, mobile legends, …):
    banned OUTRIGHT (re-banned 2026-06-12, final) — live or not.

    Stock market / equities: banned OUTRIGHT (re-banned 2026-06-12 after a
    one-day in-session experiment) — indices, ETFs, tickers, company
    stocks, price-threshold closes, weekly ranges, touch markets.
    """
    q = str(market.get("question") or "").lower()
    if any(pat in q for pat in _EXCLUDED_QUESTION_SUBSTRINGS):
        return True
    slug = str(market.get("slug") or "").lower()
    if any(pat in slug for pat in _EXCLUDED_SLUG_SUBSTRINGS):
        return True
    if _VIEW_COUNT_RE.search(q):
        return True
    raw_q = str(market.get("question") or "")
    return is_esports_text(raw_q, slug) or is_stock_text(raw_q, slug)


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
