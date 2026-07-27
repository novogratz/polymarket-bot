"""Box-office bracket model — the box-office lane's edge gate.

Prices Polymarket ``Will "<film>" <Nth> Weekend Box Office be …?`` bracket
markets from the SAME source that resolves them: The Numbers
(the-numbers.com — named as the primary resolution source in every market's
rules; Box Office Mojo is only the tie-breaker). The site publishes per-film
daily and weekend grosses, flagged ``estimate`` until the actuals land — the
exact figures the market settles on.

Model (deterministic, stdlib-only, no LLM) — HOLDOVER weekends only:
  * target weekend gross already published (estimate or final): normal
    around the published figure, σ = 0.3% (final) / 2% (estimate) — the
    residual estimate→actual revision risk;
  * Friday of the target weekend published, weekend incomplete: weekend ≈
    Friday × 3.35 (stable 3.3–3.4× for holdover family/tentpole films),
    σ = 6%;
  * before the weekend: previous weekend × 0.70 (holdover drops cluster
    ≈ −30%), σ = 15%.
  * OPENING weekends: no model (pre-release tracking is a different problem)
    → None, the market is skipped, never guessed.

Public surface (mirrors tweet_model / weather_forecast):
  parse_boxoffice_question(question) -> dict | None    # pure, offline
  boxoffice_outcome_probability(parsed, outcome) -> float | None  # network,
                                                       # cached, fail-closed

Bracket semantics per the market rules: an exact boundary resolves to the
HIGHER bracket, so "between X and Y" ≡ [X, Y), "less than X" ≡ [0, X),
"at least/greater than X" ≡ [X, ∞).
"""

from __future__ import annotations

import html as _html
import math
import re
import time
import urllib.request
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Optional

TN_BASE = "https://www.the-numbers.com"
_FETCH_TIMEOUT_S = 20
# One fetch per film per 15 minutes — the data changes a few times a day.
_CACHE_BUCKET_S = 900

_QUESTION_RE = re.compile(
    r'^Will\s+"(?P<title>[^"]+)"\s+(?P<week>Opening|\d+(?:st|nd|rd|th))\s+'
    r"Weekend\s+Box\s+Office\s+be\s+"
    r"(?:less\s+than\s+(?P<lt>\d+(?:\.\d+)?)m"
    r"|between\s+(?P<lo>\d+(?:\.\d+)?)m\s+and\s+(?P<hi>\d+(?:\.\d+)?)m"
    r"|(?:at\s+least|greater\s+than)\s+(?P<ge>\d+(?:\.\d+)?)m)\?$",
    re.IGNORECASE,
)

# Weekend/daily performance rows on a The Numbers movie page. The gross cell
# carries class="data estimate" while it is a studio estimate and a plain
# class="data" once final — that class flip IS the market's resolution
# trigger, so it is captured.
_TN_ROW_RE = re.compile(
    r'href="/box-office-chart/(?P<kind>weekend|daily)/(?P<y>\d{4})/(?P<m>\d{2})/(?P<d>\d{2})"'
    r'.*?<td\s+class="data(?P<est>\s+(?:estimate|chart_estimate))?"\s*>\$(?P<gross>[\d,]+)</td>',
    re.IGNORECASE | re.DOTALL,
)


def parse_boxoffice_question(question: str) -> Optional[dict]:
    """Parse a weekend box-office bracket question. Pure and offline.

    Returns {title, week (1 = opening), lo, hi} with lo/hi in DOLLARS
    ([lo, hi), hi None = open top) — or None for anything else.
    """
    m = _QUESTION_RE.match(str(question or "").strip())
    if not m:
        return None
    week_raw = m.group("week").lower()
    week = 1 if week_raw == "opening" else int(re.sub(r"\D", "", week_raw))
    if m.group("lt") is not None:
        lo, hi = 0.0, float(m.group("lt")) * 1e6
    elif m.group("lo") is not None:
        lo, hi = float(m.group("lo")) * 1e6, float(m.group("hi")) * 1e6
        if hi <= lo:
            return None
    else:
        lo, hi = float(m.group("ge")) * 1e6, None
    return {"title": m.group("title").strip(), "week": week, "lo": lo, "hi": hi}


def is_boxoffice_question(question: str) -> bool:
    return "weekend box office" in str(question or "").lower()


# ── The Numbers fetch layer ──────────────────────────────────────────────────


def _bucket(now_ts: Optional[float] = None) -> int:
    return int((now_ts if now_ts is not None else time.time()) // _CACHE_BUCKET_S)


def _fetch(url: str) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) polymarket-bot/boxoffice"}
    )
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_S) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _norm_title(title: str) -> str:
    # Chart link text arrives HTML-escaped ("Minions &amp; Monsters") —
    # unescape before normalizing or the "amp" survives and never matches.
    return re.sub(r"[^a-z0-9]+", " ", _html.unescape(str(title)).lower()).strip()


@lru_cache(maxsize=32)
def _tn_slug_for_title(title_norm: str, bucket: int) -> Optional[str]:
    """Find the film's The Numbers slug via the recent daily charts.

    Question titles can be shorter than the official ones ("Moana" vs
    "Moana (2026)"), so match normalized prefix both ways.
    """
    now = datetime.now(timezone.utc)
    for back in range(1, 6):
        day = datetime.fromtimestamp(now.timestamp() - back * 86400, tz=timezone.utc)
        try:
            html = _fetch(f"{TN_BASE}/box-office-chart/daily/{day:%Y/%m/%d}")
        except Exception:
            continue
        for slug, shown in re.findall(r'href="/movie/([^"#?]+)"[^>]*>([^<]+)</a>', html):
            shown_norm = _norm_title(shown)
            if shown_norm.startswith(title_norm) or title_norm.startswith(shown_norm):
                return slug
    return None


@lru_cache(maxsize=32)
def _tn_movie_rows(slug: str, bucket: int) -> tuple:
    """((kind, date, gross, is_estimate), …) chronological, from the film page."""
    html = _fetch(f"{TN_BASE}/movie/{slug}")
    # The page renders each table twice (desktop + mobile) — dedupe by
    # (kind, date), first occurrence wins, else week N indexes into a
    # duplicated list and reads the WRONG weekend.
    uniq: dict = {}
    for m in _TN_ROW_RE.finditer(html):
        key = (m.group("kind").lower(), m.group("y"), m.group("m"), m.group("d"))
        if key in uniq:
            continue
        uniq[key] = (
            m.group("kind").lower(),
            datetime(int(m.group("y")), int(m.group("m")), int(m.group("d")), tzinfo=timezone.utc),
            float(m.group("gross").replace(",", "")),
            bool(m.group("est")),
        )
    rows = sorted(uniq.values(), key=lambda r: (r[1], r[0]))
    return tuple(rows)


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


# Holdover-weekend structure (worked examples 2026-07-27: Odyssey W2
# 87.0/25.8 = 3.37×, Minions W4 9.6/2.89 = 3.32×; drops −29.4% to −41.5%).
FRIDAY_TO_WEEKEND_MULT = 3.35
HOLDOVER_DROP = 0.70


def boxoffice_outcome_probability(parsed: dict, outcome: str) -> Optional[float]:
    """Model probability that ``outcome`` ("Yes"/"No") wins the bracket.

    Fail-CLOSED with None (skip, never guess) for: opening weekends, film
    not found on The Numbers, no usable stage data, or any fetch failure.
    """
    try:
        side = str(outcome or "").strip().lower()
        if side not in ("yes", "no"):
            return None
        week = int(parsed["week"])
        if week < 2:
            return None  # opening weekends: no model
        slug = _tn_slug_for_title(_norm_title(parsed["title"]), _bucket())
        if not slug:
            return None
        rows = _tn_movie_rows(slug, _bucket())
        weekends = [r for r in rows if r[0] == "weekend"]
        fridays = [r for r in rows if r[0] == "daily" and r[1].weekday() == 4]
        if len(weekends) >= week:
            _, _, gross, est = weekends[week - 1]
            mu, sigma = gross, gross * (0.02 if est else 0.003)
        elif len(fridays) >= week:
            _, _, fri_gross, _ = fridays[week - 1]
            mu = fri_gross * FRIDAY_TO_WEEKEND_MULT
            sigma = mu * 0.06
        elif len(weekends) >= week - 1:
            _, _, prev, _ = weekends[week - 2]
            mu = prev * HOLDOVER_DROP
            sigma = mu * 0.15
        else:
            return None
        lo = float(parsed["lo"] or 0.0)
        hi = parsed["hi"]
        p_in = (_normal_cdf(hi, mu, sigma) if hi is not None else 1.0) - _normal_cdf(lo, mu, sigma)
        p_in = min(0.999, max(0.001, p_in))
        return p_in if side == "yes" else 1.0 - p_in
    except Exception:
        return None


def find_boxoffice_event_slugs(get_json: Any) -> tuple:
    """Slugs of open '<film> Nth Weekend Box Office' events on Gamma.

    ``get_json(url)`` is injected by the caller. Scans the top-volume open
    events (the generic capped scan usually misses these brackets)."""
    slugs = []
    try:
        for offset in (0, 100, 200, 300, 400):
            events = get_json(
                "https://gamma-api.polymarket.com/events"
                f"?closed=false&limit=100&offset={offset}&order=volume24hr&ascending=false"
            )
            if not events:
                break
            for event in events:
                title = str(event.get("title") or "")
                if "weekend box office" in title.lower():
                    slugs.append(str(event.get("slug") or ""))
    except Exception:
        pass
    return tuple(s for s in dict.fromkeys(slugs) if s)
