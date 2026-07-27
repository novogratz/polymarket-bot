"""Tweet-count probability model — the tweet-only lane's edge gate.

Prices Polymarket "Will <person> post N1-N2 tweets ..." bracket markets from
the SAME data source that resolves them: the public xtracker feed
(xtracker.polymarket.com), which exposes every tracked account's post-level
timestamps and the exact start/end of every open counting window.

Model (deterministic, stdlib-only, no LLM):
  * nonhomogeneous Poisson intensity with an hour-of-week seasonality profile
    fitted on the trailing 56 days of post timestamps (sleep cycle is real);
  * a short-term activity multiplier from the trailing 72 h actual-vs-expected
    rate (posting sprees are self-exciting, quiet spells persist);
  * an optional REGIME multiplier read from ``data/tweet_regime.json`` —
    written OFFLINE by scripts/tweet_regime_sidecar.py (local Ollama only,
    never in the trade loop); stale or missing file → 1.0;
  * negative-binomial predictive distribution for the remaining count
    (overdispersion fitted from 90 days of daily counts), closed-form pmf —
    no Monte Carlo, no numpy.

Public surface (mirrors weather_forecast.py):
  parse_tweet_question(question) -> dict | None      # pure, offline, regex
  tweet_outcome_probability(parsed, outcome) -> float | None  # network, cached
                                                     # fail-open with None

Calibration (2026-07-27, 9 months of elonmusk history, 192 rolling 7-day
windows): in the model-P(No) 0.90-0.95 band the realized No-rate was 0.915-
0.938 at every decision point; the 0.95+ band realized 0.985-0.996. The model
is honest about its tails — the gate only needs it to not be optimistic.
"""

from __future__ import annotations

import bisect
import json
import math
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Optional

XTRACKER_BASE = "https://xtracker.polymarket.com/api"
_FETCH_TIMEOUT_S = 15
# Posts/users re-fetch cadence: one call per account per 5 minutes.
_CACHE_BUCKET_S = 300
# Minimum history span before we trust a fitted profile.
_MIN_HISTORY_DAYS = 28.0
_PROFILE_TRAIN_DAYS = 56
_DISPERSION_TRAIN_DAYS = 90
_ACTIVITY_LOOKBACK_H = 72.0
# Regime file (written by the offline Ollama sidecar; the live loop only READS
# this file — no LLM call ever happens in the trade path).
REGIME_FILE = os.environ.get("POLYMARKET_TWEET_REGIME_FILE", "data/tweet_regime.json")
_REGIME_MAX_AGE_S = 2 * 3600
_REGIME_CLAMP = (0.5, 2.0)

_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_MONTH_NUM = {name: i + 1 for i, name in enumerate(_MONTHS)}

_PERSON_RE = re.compile(r"^\s*will\s+(?P<person>.+?)\s+(?:post|make|publish|send)\b", re.IGNORECASE)
_RANGE_RE = re.compile(r"\b(?P<lo>\d+)\s*[-–]\s*(?P<hi>\d+)\s+(?:tweets|posts|times)\b", re.IGNORECASE)
_PLUS_RE = re.compile(r"\b(?P<lo>\d+)\s*\+\s*(?:tweets|posts|times)\b", re.IGNORECASE)
_LESS_RE = re.compile(r"(?:<|\bfewer than\b|\bless than\b|\bunder\b)\s*(?P<hi>\d+)\s+(?:tweets|posts|times)\b", re.IGNORECASE)
_MONTH_DAY_RE = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})\b", re.IGNORECASE)
_MONTH_ONLY_RE = re.compile(r"\bin\s+(" + "|".join(_MONTHS) + r")\s+(\d{4})\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(20\d{2})\b")


def parse_tweet_question(question: str) -> Optional[dict]:
    """Parse a tweet/post-count bracket question. Pure and offline.

    "Will Elon Musk post 240-259 tweets from July 21 to July 28, 2026?"
      -> {person: "elon musk", lo: 240, hi: 259, dates: [(7,21),(7,28)], ...}
    "Will Elon Musk post 500+ tweets from ...?"   -> lo=500, hi=None
    "Will Elon Musk post <40 tweets from ...?"    -> lo=0,   hi=39
    "Will Elon Musk post 0-19 tweets in August 2026?" -> month_only=8

    Returns None for anything that is not a count-bracket question.
    """
    if not question:
        return None
    q = str(question)
    person_m = _PERSON_RE.search(q)
    if not person_m:
        return None
    lo: Optional[int]
    hi: Optional[int]
    range_m = _RANGE_RE.search(q)
    plus_m = _PLUS_RE.search(q)
    less_m = _LESS_RE.search(q)
    if range_m:
        lo, hi = int(range_m.group("lo")), int(range_m.group("hi"))
        if hi < lo:
            return None
    elif plus_m:
        lo, hi = int(plus_m.group("lo")), None
    elif less_m:
        lo, hi = 0, int(less_m.group("hi")) - 1
    else:
        return None
    dates = [(_MONTH_NUM[m.lower()], int(d)) for m, d in _MONTH_DAY_RE.findall(q)]
    month_only_m = _MONTH_ONLY_RE.search(q)
    year_m = _YEAR_RE.search(q)
    return {
        "person": person_m.group("person").strip().lower(),
        "lo": lo,
        "hi": hi,
        "dates": dates,
        "month_only": _MONTH_NUM[month_only_m.group(1).lower()] if month_only_m else None,
        "year": int(year_m.group(1)) if year_m else None,
        "question": q,
    }


# ── xtracker fetch layer (cached per 5-minute bucket) ────────────────────────


def _fetch_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-bot/tweet-model"})
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _bucket(now_ts: Optional[float] = None) -> int:
    return int((now_ts if now_ts is not None else time.time()) // _CACHE_BUCKET_S)


@lru_cache(maxsize=64)
def _users_cached(bucket: int) -> tuple:
    """All tracked accounts + their counting windows. One call per 5 min."""
    payload = _fetch_json(f"{XTRACKER_BASE}/users")
    return tuple(payload.get("data") or ())


@lru_cache(maxsize=64)
def _posts_cached(handle: str, bucket: int) -> tuple:
    """Sorted POSIX timestamps of every post by ``handle``. One call per 5 min.

    The endpoint returns the FULL history in one response (~9 months for
    elonmusk) — no pagination needed.
    """
    payload = _fetch_json(f"{XTRACKER_BASE}/users/{handle}/posts")
    stamps = []
    for post in payload.get("data") or ():
        ts = _parse_iso(post.get("createdAt"))
        if ts is not None:
            stamps.append(ts.timestamp())
    stamps.sort()
    return tuple(stamps)


def _parse_iso(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _match_tracking(parsed: dict, now: datetime) -> Optional[tuple[str, datetime, datetime]]:
    """Resolve the question to (handle, window_start, window_end) via xtracker.

    The tracker's trackings carry the EXACT cutoff times the market resolves
    on (e.g. 16:00 UTC weekly boundaries, 04:00 UTC monthly) — never guess
    them from the question text. No match → None (the market is unpriceable
    and must be skipped, not guessed)."""
    person = parsed["person"]
    for user in _users_cached(_bucket(now.timestamp())):
        name = str(user.get("name") or "").lower()
        handle = str(user.get("handle") or "").lower()
        if person not in name and person not in handle and name not in person:
            continue
        for tracking in user.get("trackings") or ():
            start = _parse_iso(tracking.get("startDate"))
            end = _parse_iso(tracking.get("endDate"))
            if start is None or end is None:
                continue
            if parsed["year"] is not None and start.year != parsed["year"] and end.year != parsed["year"]:
                continue
            if parsed["month_only"] is not None:
                # Monthly window: match on start month + a ≥ 25-day span.
                if start.month == parsed["month_only"] and (end - start) >= timedelta(days=25):
                    return str(user.get("handle")), start, end
                continue
            dates = parsed["dates"]
            if not dates:
                continue
            if (start.month, start.day) == dates[0] and (end.month, end.day) == dates[-1]:
                return str(user.get("handle")), start, end
    return None


# ── count model ──────────────────────────────────────────────────────────────


def _count_between(stamps: tuple, a: datetime, b: datetime) -> int:
    if b <= a:
        return 0
    return bisect.bisect_left(stamps, b.timestamp()) - bisect.bisect_left(stamps, a.timestamp())


@lru_cache(maxsize=64)
def _fitted_model(handle: str, bucket: int) -> Optional[tuple]:
    """(hour_of_week_profile[168], r_daily) fitted on trailing history."""
    stamps = _posts_cached(handle, bucket)
    if len(stamps) < 50:
        return None
    now = datetime.fromtimestamp(bucket * _CACHE_BUCKET_S, tz=timezone.utc)
    first = datetime.fromtimestamp(stamps[0], tz=timezone.utc)
    if (now - first) < timedelta(days=_MIN_HISTORY_DAYS):
        return None
    # Hour-of-week rate profile with shrinkage toward the global mean rate
    # (8 observations per cell over 56 days is noisy).
    train_start = now - timedelta(days=_PROFILE_TRAIN_DAYS)
    counts = [0.0] * 168
    exposure = [0.0] * 168
    cur = train_start.replace(minute=0, second=0, microsecond=0)
    while cur < now:
        cell = cur.weekday() * 24 + cur.hour
        counts[cell] += _count_between(stamps, cur, cur + timedelta(hours=1))
        exposure[cell] += 1.0
        cur += timedelta(hours=1)
    total_exposure = sum(exposure)
    if total_exposure <= 0:
        return None
    mean_rate = sum(counts) / total_exposure
    profile = tuple(
        (c + 4.0 * mean_rate) / (e + 4.0) if e else mean_rate for c, e in zip(counts, exposure)
    )
    # Negative-binomial dispersion from daily counts: var = m + m^2/r.
    daily = []
    cur = now - timedelta(days=_DISPERSION_TRAIN_DAYS)
    while cur + timedelta(days=1) <= now:
        daily.append(_count_between(stamps, cur, cur + timedelta(days=1)))
        cur += timedelta(days=1)
    m = sum(daily) / len(daily)
    var = sum((x - m) ** 2 for x in daily) / max(len(daily) - 1, 1)
    r_daily = (m * m / (var - m)) if var > m else 1e9
    return profile, max(r_daily, 0.5)


def _expected_between(profile: tuple, a: datetime, b: datetime) -> float:
    """Integral of the hour-of-week intensity over [a, b)."""
    lam = 0.0
    cur = a
    while cur < b:
        step_end = min(b, (cur + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0))
        if step_end <= cur:
            step_end = min(b, cur + timedelta(hours=1))
        lam += profile[cur.weekday() * 24 + cur.hour] * ((step_end - cur).total_seconds() / 3600.0)
        cur = step_end
    return lam


def _activity_multiplier(stamps: tuple, profile: tuple, now: datetime) -> float:
    """Trailing-72h actual/expected ratio, shrunk toward 1 and clamped."""
    start = now - timedelta(hours=_ACTIVITY_LOOKBACK_H)
    expected = _expected_between(profile, start, now)
    if expected <= 0:
        return 1.0
    actual = float(_count_between(stamps, start, now))
    mult = (actual + 0.5 * expected) / (1.5 * expected)
    return max(0.4, min(2.5, mult))


def _regime_multiplier(handle: str, now: datetime) -> float:
    """Offline-Ollama regime prior from REGIME_FILE. Missing/stale/bad → 1.0."""
    try:
        with open(REGIME_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        entry = data.get(handle.lower())
        if not isinstance(entry, dict):
            return 1.0
        updated = _parse_iso(entry.get("updated"))
        if updated is None or (now - updated).total_seconds() > _REGIME_MAX_AGE_S:
            return 1.0
        mult = float(entry.get("multiplier", 1.0))
        return max(_REGIME_CLAMP[0], min(_REGIME_CLAMP[1], mult))
    except Exception:
        return 1.0


def _nb_pmf(mean: float, r: float, nmax: int) -> list[float]:
    """Negative-binomial pmf[0..nmax] with the given mean and size r
    (variance = mean + mean²/r; r → ∞ recovers plain Poisson)."""
    if mean <= 0:
        out = [0.0] * (nmax + 1)
        out[0] = 1.0
        return out
    p = r / (r + mean)
    out = [0.0] * (nmax + 1)
    lp = r * math.log(p)
    out[0] = math.exp(lp)
    log1mp = math.log(1.0 - p)
    for k in range(1, nmax + 1):
        lp += math.log((k + r - 1.0) / k) + log1mp
        out[k] = math.exp(lp)
    return out


def _bracket_prob(current: int, mean_rem: float, r: float, lo: int, hi: Optional[int]) -> float:
    """P(final count lands in [lo, hi]) given ``current`` so far."""
    sd = math.sqrt(mean_rem + mean_rem * mean_rem / r) if mean_rem > 0 else 0.0
    nmax = int(mean_rem + 12.0 * sd + 50)
    pmf = _nb_pmf(mean_rem, r, nmax)
    lo_r = max(0, lo - current)
    hi_r = nmax if hi is None else hi - current
    if hi_r < 0:
        return 0.0
    return min(1.0, sum(pmf[lo_r : min(hi_r, nmax) + 1]))


def tweet_outcome_probability(parsed: dict, outcome: str) -> Optional[float]:
    """Model probability that ``outcome`` ("Yes"/"No") wins the parsed bracket.

    Network path (xtracker, cached 5 min). Returns None — meaning SKIP, not
    "assume fine" — when the window can't be matched to a tracker window, the
    history is too short, or any fetch fails. The caller treats None as a
    skip, mirroring the weather gate's margin-guard semantics.
    """
    try:
        side = str(outcome or "").strip().lower()
        if side not in ("yes", "no"):
            return None
        now = datetime.now(timezone.utc)
        match = _match_tracking(parsed, now)
        if match is None:
            return None
        handle, start, end = match
        stamps = _posts_cached(handle.lower(), _bucket(now.timestamp()))
        model = _fitted_model(handle.lower(), _bucket(now.timestamp()))
        if model is None:
            return None
        profile, r_daily = model
        lo = int(parsed["lo"] or 0)
        hi = parsed["hi"]
        current = _count_between(stamps, start, min(now, end))
        if now >= end:
            in_bracket = current >= lo and (hi is None or current <= hi)
            p_in = 1.0 if in_bracket else 0.0
        else:
            window_start = max(start, now)
            mult = _activity_multiplier(stamps, profile, now) * _regime_multiplier(handle, now)
            mean_rem = _expected_between(profile, window_start, end) * mult
            days_left = max((end - now).total_seconds() / 86400.0, 0.05)
            r_eff = max(1.0, r_daily * days_left)
            p_in = _bracket_prob(current, mean_rem, r_eff, lo, hi)
        p_yes = min(0.999, max(0.001, p_in))
        return p_yes if side == "yes" else 1.0 - p_yes
    except Exception:
        return None
