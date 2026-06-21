"""v4 market-category classification + data-driven auto-disable.

User 2026-06-21 ("Polymarket Bot v4"): with ``unban_all_markets`` every
category is allowed at entry selection, and governance becomes **data-driven**
instead of manual bans — after at least ``min_samples`` realized trades in a
category, the category is auto-disabled if its ROI falls below
``roi_threshold`` (default −5%). This module is the deterministic engine for
that: a category classifier and the per-category stats / disabled-set helpers.

The classifier is pure text matching over the market question + slug. It is
intentionally simple and improvable; it never raises. Order matters — the most
specific buckets are tested first so e.g. a crypto or economics market is not
swallowed by the generic sports "vs" rule.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

# The fixed v4 category set (user 2026-06-21).
CATEGORIES = (
    "politics",
    "economics",
    "crypto",
    "ufc",
    "golf",
    "soccer",
    "sports",
    "entertainment",
    "other",
)

# Word-bounded patterns (collision-safe). Checked in this order.
_CRYPTO_RE = re.compile(
    r"\b(?:bitcoin|btc|ethereum|eth|solana|sol|dogecoin|doge|xrp|cardano|ada|"
    r"litecoin|crypto|stablecoin|altcoin)\b|\bup or down\b"
)
_ECON_RE = re.compile(
    r"\b(?:fed|fomc|interest rates?|rate (?:cut|hike|decision)|inflation|cpi|"
    r"ppi|gdp|unemployment|recession|basis points|selic|ecb|jobs report|"
    r"nonfarm|payrolls)\b"
)
_POLITICS_RE = re.compile(
    r"\b(?:election|primary|senate|congress|president(?:ial)?|governor|mayor(?:al)?|"
    r"parliament|referendum|ballot|caucus|nominee|approval rating|trump|biden|"
    r"politic|impeach|cabinet|prime minister)\b"
)
_UFC_RE = re.compile(r"\b(?:ufc|mma|octagon|by submission|by tko|by ko)\b")
_GOLF_RE = re.compile(
    r"\b(?:golf|pga|liv golf|ryder cup|the masters|masters tournament|"
    r"the open championship|us open golf)\b"
)
_SOCCER_RE = re.compile(
    r"\b(?:soccer|world cup|premier league|la ?liga|serie a|bundesliga|"
    r"ligue 1|champions league|europa league|uefa|fifa|mls|epl|"
    r"copa|eredivisie|primeira)\b|\bfc\b|\bcf\b|\bsc\b"
)
_ENTERTAINMENT_RE = re.compile(
    r"\b(?:movie|film|box office|album|grammy|oscar|academy award|emmy|"
    r"golden globe|palme|netflix|spotify|tiktok|billboard|rotten tomatoes|"
    r"celebrity|streaming|youtube|mrbeast|tweet)\b|\bviews\b"
)
# Generic sports — checked AFTER soccer/ufc/golf so those keep their own bucket.
_SPORTS_RE = re.compile(
    r"\b(?:nba|nfl|nhl|mlb|tennis|atp|wta|nascar|f1|formula 1|boxing|cricket|"
    r"rugby|playoffs?|esports|lol|valorant|dota|counter-?strike)\b|"
    r"\bo/u\b|\bvs\.?\b|-vs-|\bwin on\b"
)


def classify_category(question: str, slug: str = "") -> str:
    """Return the v4 category for a market (always one of CATEGORIES)."""
    text = f" {str(question or '').lower()} {str(slug or '').lower().replace('-', ' ')} "
    if _CRYPTO_RE.search(text):
        return "crypto"
    if _ECON_RE.search(text):
        return "economics"
    if _POLITICS_RE.search(text):
        return "politics"
    if _UFC_RE.search(text):
        return "ufc"
    if _GOLF_RE.search(text):
        return "golf"
    if _SOCCER_RE.search(text):
        return "soccer"
    if _ENTERTAINMENT_RE.search(text):
        return "entertainment"
    if _SPORTS_RE.search(text):
        return "sports"
    return "other"


def classify_market(market: dict[str, Any]) -> str:
    """Category for a raw Gamma market dict."""
    return classify_category(
        str(market.get("question") or ""),
        str(market.get("slug") or market.get("id") or ""),
    )


def _record_pnl(record: dict[str, Any]) -> float:
    for key in ("realized_pnl", "realized_pnl_usd"):
        val = record.get(key)
        if val is not None:
            try:
                return float(val or 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _record_cost(record: dict[str, Any]) -> float:
    """Cost basis (entry $) for a realized trade — the ROI denominator."""
    for key in ("stake", "cost", "stake_usd", "entry_cost"):
        val = record.get(key)
        if val is not None:
            try:
                cost = float(val or 0.0)
            except (TypeError, ValueError):
                cost = 0.0
            if cost > 0:
                return cost
    # Fall back to entry_price × shares when an explicit stake isn't stored.
    try:
        entry = float(record.get("entry_price") or 0.0)
        shares = float(record.get("shares") or record.get("initial_shares") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return entry * shares if entry > 0 and shares > 0 else 0.0


def category_stats(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-category realized stats: trades, wins, losses, total_pnl, total_cost,
    roi (= total_pnl / total_cost), avg_pnl. ROI is 0.0 when no cost is known."""
    stats: dict[str, dict[str, Any]] = {}
    for record in records:
        cat = classify_category(
            str(record.get("question") or ""),
            str(record.get("slug") or ""),
        )
        pnl = _record_pnl(record)
        cost = _record_cost(record)
        s = stats.setdefault(
            cat,
            {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0, "total_cost": 0.0},
        )
        s["trades"] += 1
        s["total_pnl"] += pnl
        s["total_cost"] += cost
        if pnl > 0:
            s["wins"] += 1
        elif pnl < 0:
            s["losses"] += 1
    for s in stats.values():
        s["roi"] = (s["total_pnl"] / s["total_cost"]) if s["total_cost"] > 0 else 0.0
        s["avg_pnl"] = (s["total_pnl"] / s["trades"]) if s["trades"] else 0.0
        s["win_rate"] = (s["wins"] / s["trades"]) if s["trades"] else 0.0
        s["total_pnl"] = round(s["total_pnl"], 2)
        s["total_cost"] = round(s["total_cost"], 2)
        s["roi"] = round(s["roi"], 4)
        s["avg_pnl"] = round(s["avg_pnl"], 4)
        s["win_rate"] = round(s["win_rate"], 3)
    return stats


def disabled_categories(
    records: Iterable[dict[str, Any]],
    *,
    min_samples: int = 100,
    roi_threshold: float = -0.05,
) -> set[str]:
    """Categories to auto-disable: ≥ ``min_samples`` realized trades AND
    ROI < ``roi_threshold`` (default −5%). Below the sample size a category is
    never disabled — the rule is forward-looking, so a fresh bot disables
    nothing. ``other`` is never auto-disabled (it is a catch-all, not a lane)."""
    if min_samples <= 0:
        return set()
    out: set[str] = set()
    for cat, s in category_stats(records).items():
        if cat == "other":
            continue
        if s["trades"] >= min_samples and s["roi"] < roi_threshold:
            out.add(cat)
    return out
