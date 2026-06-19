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
    # YouTube view/subscriber-count markets (2026-06-14, user rule — lost a
    # MrBeast view-count bet): "Will <video> reach X views by <date>?" —
    # a view total has no convergence signal and jumps unpredictably.
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
    "crypto",
)

# ── Esports — LEAGUE OF LEGENDS ONLY, LIVE GAMES ONLY ────────────────────
# Blanket-banned 2026-05-31; re-allowed live-only 2026-06-12; narrowed twice
# the same day (user): ONLY League of Legends qualifies — Mobile Legends,
# Counter-Strike, and every other title are banned outright. LoL still
# requires the game to be IN PROGRESS (gameStartTime in the past, within
# _ESPORTS_LIVE_MAX_HOURS) and an ask ≥ ESPORTS_MIN_ASK (0.92).
_ESPORTS_ALLOWED_QUESTION_SUBSTRINGS = (
    "league of legends",
    "lol:",
)
_ESPORTS_ALLOWED_SLUG_SUBSTRINGS = (
    "league-of-legends",
    "lol-",
)

# Per-lane entry floor (user 2026-06-12): the esports lane needs MORE
# certainty than the global price band, not less — never below 0.92.
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
    "valorant",
    "mobile legends",
    "mlbb",
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
    "mobile-legends",
    "mlbb",
    "dota",
    "esports",
)
_ESPORTS_LIVE_MAX_HOURS = 8.0

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
# (\bviews\b) is the view-count tell — "reviews"/"interviews" do NOT match
# (no word boundary before the 'v'), so only true view-count titles are hit.
_VIEW_COUNT_RE = re.compile(r"\bviews\b")

# Macro / central-bank interest-rate decision markets — banned outright
# (user 2026-06-16: "why do we have a bet on the Fed rate by September? too
# far away — we only want stuff expiring in 4-6h max"). These resolve weeks
# to months out (Fed/FOMC, ECB, BoE, Bank of Brazil Selic, etc.) and can
# never satisfy the ≤4h grinder window; one slipped into the wallet via
# live-position sync. Word-bounded so it can't collide with "accurate",
# "winrate", "generate", etc.
_MACRO_RATE_RE = re.compile(
    r"\b(?:rate cuts?|rate hikes?|rate decisions?|interest rates?|fed rates?|"
    r"(?:raise|cut|hold|lower|hike|increase|decrease)s? rates?|"
    r"fomc|selic|central bank|bank of (?:england|japan|canada|brazil)|\becb\b|"
    r"basis points|rate (?:by|after|before))\b"
)

# "What will be said" markets — banned outright (user 2026-06-18: a bot bought
# 'Will the announcers say "Golden Boot" during the Canada vs Qatar match?'
# "never bet about what something will say"). Bets on whether a person /
# commentator / announcer will SAY or MENTION a given word or phrase are pure
# linguistic coin-flips with no convergence edge. Word-bounded so it can't
# collide inside other words ("essay", "Macy's", "summation", etc.); the
# leading \b on "say" stops a match inside "essay"/"naysayer".
_SPEECH_MARKET_RE = re.compile(
    r"\b(?:says?|said|saying|mentions?|mentioned|utters?|uttered)\b"
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


_CRYPTO_QUESTION_SUBSTRINGS = (
    "up or down", "bitcoin", "btc", "ethereum", "solana",
    "dogecoin", "xrp", "cardano", "litecoin", "crypto",
)
_CRYPTO_SLUG_SUBSTRINGS = ("updown", "up-or-down")


def is_excluded_market_light(market: dict[str, Any], now: datetime | None = None) -> bool:
    """Lighter exclusion for the COPY lane (bot 2): ban ONLY crypto + stocks
    (the user's standing outright bans). Everything the grinder excludes for
    its own ride-to-resolution thesis — draws, exact scores, halftime, O/U
    lines, weather, handicaps, esports — is ALLOWED here, because the copy
    lane's edge is the wallet's signal, not our opinion on the market type.
    The grinder still uses the full is_excluded_market (this is opt-in via
    settings.smart_copy_light_exclusions)."""
    q = str(market.get("question") or "").lower()
    slug = str(market.get("slug") or "").lower()
    if any(p in q for p in _CRYPTO_QUESTION_SUBSTRINGS) or any(
        p in slug for p in _CRYPTO_SLUG_SUBSTRINGS
    ):
        return True
    raw_q = str(market.get("question") or "")
    is_stock = (
        any(pat in q for pat in _STOCK_QUESTION_SUBSTRINGS)
        or any(pat in slug for pat in _STOCK_SLUG_SUBSTRINGS)
        or bool(_STOCK_MARKET_RE.search(q))
        or bool(_STOCK_MARKET_RE.search(slug))
        or (bool(_PAREN_TICKER_RE.search(raw_q)) and "$" in raw_q)
    )
    return is_stock


def is_excluded_market(market: dict[str, Any], now: datetime | None = None) -> bool:
    """True for market types excluded from every strategy.

    Blocked categories:
    - Crypto Up/Down binaries: no real book depth, FOK orders bounce.
    - Temperature/weather threshold markets (°C and °F): specific-degree
      weather fails constantly even at 0.94.
    - Exact-score live sports: gaps on goals, SL can't catch them.
    - O/U 0.5 soccer: any-goal binary, same gap risk.

    Conditionally allowed (2026-06-12, user rule):
    - Esports: ONLY League of Legends, and ONLY while the game is live
      (gameStartTime in the past, within _ESPORTS_LIVE_MAX_HOURS). Other
      titles (Mobile Legends, Counter-Strike, Valorant, Dota, …),
      pre-game, or unknown start -> excluded.

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
    if _MACRO_RATE_RE.search(q) or _MACRO_RATE_RE.search(slug.replace("-", " ")):
        return True
    if _SPEECH_MARKET_RE.search(q) or _SPEECH_MARKET_RE.search(slug.replace("-", " ")):
        return True
    if now is None:
        now = datetime.now(timezone.utc)
    is_esports = any(pat in q for pat in _ESPORTS_QUESTION_SUBSTRINGS) or any(
        pat in slug for pat in _ESPORTS_SLUG_SUBSTRINGS
    )
    if is_esports:
        allowed_game = any(
            pat in q for pat in _ESPORTS_ALLOWED_QUESTION_SUBSTRINGS
        ) or any(pat in slug for pat in _ESPORTS_ALLOWED_SLUG_SUBSTRINGS)
        if not allowed_game:
            # Only LoL and Mobile Legends qualify (user 2026-06-12) —
            # Counter-Strike, Valorant, Dota, … are banned outright.
            return True
        return not _esports_game_is_live(market, now)
    raw_q = str(market.get("question") or "")
    is_stock = (
        any(pat in q for pat in _STOCK_QUESTION_SUBSTRINGS)
        or any(pat in slug for pat in _STOCK_SLUG_SUBSTRINGS)
        or bool(_STOCK_MARKET_RE.search(q))
        or bool(_STOCK_MARKET_RE.search(slug))
        or (bool(_PAREN_TICKER_RE.search(raw_q)) and "$" in raw_q)
    )
    if is_stock:
        # Re-banned outright 2026-06-12 (user) — no session window, ever.
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
    # Recent price moves for THIS outcome (NO side already sign-flipped). Used
    # by the favorite-dip lane to spot a favorite that just dropped.
    one_day_change: float = 0.0
    one_hour_change: float = 0.0

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
