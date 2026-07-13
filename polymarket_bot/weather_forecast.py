"""
Weather forecast integration for the weather-only grinder bot.

Uses Open-Meteo (https://open-meteo.com, free, no API key required).

Improvements over v1:
  - Multi-model consensus: GFS, ECMWF IFS, and UK Met Office fetched in
    parallel; σ is computed from the actual spread between models instead of
    a fixed formula.  Models that disagree by >MAX_SPREAD_C are silently
    skipped (the trade is allowed — fail-open applies).
  - Intraday current-temperature kill-switch: for same-day markets, if the
    current observed temperature already makes the bracket physically
    impossible (e.g. it's 3PM and currently 29°C; bracket is 35–36°C),
    the function returns a near-certain "No" probability without waiting
    for the daily max to be recorded.
  - Automatic skip when fewer than MIN_MODELS respond (API outage guard).

Edge formula (unchanged):
    model_P(outcome) − market_ask ≥ weather_forecast_min_edge

Fail-open: if parsing fails or the API is unreachable, returns None and the
normal price filters apply.

Caching: each (lat, lon, date, UTC-hour, model) is cached so API calls
collapse to at most 3 per city per hour after the first tick.
"""
from __future__ import annotations

import concurrent.futures
import json
import math
import re
import urllib.request
from datetime import date, datetime, timezone
from functools import lru_cache
from typing import Optional

# ---------------------------------------------------------------------------
# City → (lat, lon) lookup
# ---------------------------------------------------------------------------
CITY_COORDS: dict[str, tuple[float, float]] = {
    "los angeles":    (34.0522, -118.2437),
    "new york":       (40.7128,  -74.0060),
    "san antonio":    (29.4241,  -98.4936),
    "san diego":      (32.7157, -117.1611),
    "san jose":       (37.3382, -121.8863),
    "san francisco":  (37.7749, -122.4194),
    "mexico city":    (19.4326,  -99.1332),
    "sao paulo":      (-23.5505, -46.6333),
    "rio de janeiro": (-22.9068, -43.1729),
    "buenos aires":   (-34.6037, -58.3816),
    "ho chi minh":    (10.8231,  106.6297),
    "kuala lumpur":   ( 3.1390,  101.6869),
    "hong kong":      (22.3193,  114.1694),
    "cape town":      (-33.9249,  18.4241),
    "saint petersburg": (59.9311, 30.3609),
    "abu dhabi":      (24.4539,   54.3773),
    "tel aviv":       (32.0853,   34.7818),
    "london":         (51.5074,   -0.1278),
    "paris":          (48.8566,    2.3522),
    "istanbul":       (41.0082,   28.9784),
    "madrid":         (40.4168,   -3.7038),
    "berlin":         (52.5200,   13.4050),
    "munich":         (48.1351,   11.5820),
    "rome":           (41.9028,   12.4964),
    "amsterdam":      (52.3676,    4.9041),
    "barcelona":      (41.3851,    2.1734),
    "vienna":         (48.2082,   16.3738),
    "zurich":         (47.3769,    8.5417),
    "brussels":       (50.8503,    4.3517),
    "stockholm":      (59.3293,   18.0686),
    "oslo":           (59.9139,   10.7522),
    "copenhagen":     (55.6761,   12.5683),
    "athens":         (37.9838,   23.7275),
    "warsaw":         (52.2297,   21.0122),
    "prague":         (50.0755,   14.4378),
    "budapest":       (47.4979,   19.0402),
    "bucharest":      (44.4268,   26.1025),
    "chicago":        (41.8781,  -87.6298),
    "houston":        (29.7604,  -95.3698),
    "phoenix":        (33.4484, -112.0740),
    "dallas":         (32.7767,  -96.7970),
    "austin":         (30.2672,  -97.7431),
    "seattle":        (47.6062, -122.3321),
    "denver":         (39.7392, -104.9903),
    "boston":         (42.3601,  -71.0589),
    "miami":          (25.7617,  -80.1918),
    "atlanta":        (33.7490,  -84.3880),
    "minneapolis":    (44.9778,  -93.2650),
    "toronto":        (43.6510,  -79.3470),
    "montreal":       (45.5017,  -73.5673),
    "vancouver":      (49.2827, -123.1207),
    "bogota":         ( 4.7110,  -74.0721),
    "lima":           (-12.0464, -77.0428),
    "santiago":       (-33.4489, -70.6693),
    "tokyo":          (35.6762,  139.6503),
    "osaka":          (34.6937,  135.5023),
    "seoul":          (37.5665,  126.9780),
    "beijing":        (39.9042,  116.4074),
    "shanghai":       (31.2304,  121.4737),
    "guangzhou":      (23.1291,  113.2644),
    "shenzhen":       (22.5431,  114.0579),
    "chengdu":        (30.5728,  104.0668),
    "wuhan":          (30.5928,  114.3055),
    "lucknow":        (26.8467,   80.9462),
    "taipei":         (25.0330,  121.5654),
    "bangkok":        (13.7563,  100.5018),
    "singapore":      ( 1.3521,  103.8198),
    "jakarta":        (-6.2088,  106.8456),
    "manila":         (14.5995,  120.9842),
    "hanoi":          (21.0285,  105.8542),
    "mumbai":         (19.0760,   72.8777),
    "delhi":          (28.6139,   77.2090),
    "bangalore":      (12.9716,   77.5946),
    "hyderabad":      (17.3850,   78.4867),
    "kolkata":        (22.5726,   88.3639),
    "chennai":        (13.0827,   80.2707),
    "karachi":        (24.8607,   67.0011),
    "lahore":         (31.5204,   74.3587),
    "dhaka":          (23.8103,   90.4125),
    "cairo":          (30.0444,   31.2357),
    "johannesburg":   (-26.2041,  28.0473),
    "lagos":          ( 6.5244,    3.3792),
    "nairobi":        (-1.2921,   36.8219),
    "casablanca":     (33.5731,   -7.5898),
    "algiers":        (36.7372,    3.0863),
    "tunis":          (36.8065,   10.1815),
    "accra":          ( 5.6037,   -0.1870),
    "dubai":          (25.2048,   55.2708),
    "riyadh":         (24.7136,   46.6753),
    "jeddah":         (21.2854,   39.2376),
    "doha":           (25.2854,   51.5310),
    "tehran":         (35.6892,   51.3890),
    "baghdad":        (33.3152,   44.3661),
    "amman":          (31.9454,   35.9284),
    "beirut":         (33.8938,   35.5018),
    "moscow":         (55.7558,   37.6173),
    "kyiv":           (50.4501,   30.5234),
    "minsk":          (53.9045,   27.5615),
    "wellington":     (-41.2865,  174.7762),
    "sydney":         (-33.8688,  151.2093),
    "melbourne":      (-37.8136,  144.9631),
    "brisbane":       (-27.4698,  153.0251),
    "perth":          (-31.9505,  115.8605),
    "auckland":       (-36.8509,  174.7645),
}

_CITY_LIST: list[tuple[str, float, float]] = sorted(
    ((c, la, lo) for c, (la, lo) in CITY_COORDS.items()),
    key=lambda x: -len(x[0]),
)

# ---------------------------------------------------------------------------
# Multi-model configuration
# ---------------------------------------------------------------------------
# Three independent NWP models queried in parallel. Using the Open-Meteo
# models= parameter on the standard forecast endpoint (no extra cost, no key).
_MODELS = [
    "best_match",    # Open-Meteo auto-selects the best regional model
    "ecmwf_ifs025",  # European Centre (global, 0.25°)
    "gfs_global",    # NOAA GFS (global, 0.25°)
]

MAX_SPREAD_C = 3.0   # skip if max−min across models > this (°C)
MIN_MODELS   = 2     # need at least this many successful fetches

# ---------------------------------------------------------------------------
# Question parser
# ---------------------------------------------------------------------------
_MONTH_MAP: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_DATE_RE = re.compile(
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)"
    r"\s+(\d{1,2})(?:[,\s]+(\d{4}))?",
    re.IGNORECASE,
)

_TEMP_RE = re.compile(
    r"be(?:tween)?\s+"
    r"(\d+(?:\.\d+)?)"
    r"(?:\s*[-–]\s*(\d+(?:\.\d+)?))?"
    r"\s*"
    r"(°\s*[cCfF]|degrees?\s*[cCfF]?)",
    re.IGNORECASE,
)

_TAIL_RE = re.compile(r"\bor\s+(higher|lower)\b", re.IGNORECASE)


def _parse_date(question: str) -> Optional[date]:
    m = _DATE_RE.search(question)
    if not m:
        return None
    month = _MONTH_MAP.get(m.group(1).lower()[:3])
    if not month:
        return None
    day = int(m.group(2))
    year = int(m.group(3)) if m.group(3) else datetime.now(timezone.utc).year
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_weather_question(question: str) -> Optional[dict]:
    """Parse a Polymarket temperature question into structured data, or None."""
    ql = question.lower()
    is_max = "highest" in ql or "high temperature" in ql or "maximum" in ql

    city_match: Optional[tuple[str, float, float]] = None
    for city, lat, lon in _CITY_LIST:
        if city in ql:
            city_match = (city, lat, lon)
            break
    if city_match is None:
        return None
    city, lat, lon = city_match

    m = _TEMP_RE.search(question)
    if not m:
        return None
    val1 = float(m.group(1))
    val2 = float(m.group(2)) if m.group(2) else val1
    unit_raw = m.group(3).replace("°", "").replace(" ", "").upper()
    unit = unit_raw[0] if unit_raw and unit_raw[0] in ("C", "F") else "C"

    tail_m = _TAIL_RE.search(question[m.end():])
    is_upper_tail = bool(tail_m and tail_m.group(1).lower() == "higher")
    is_lower_tail = bool(tail_m and tail_m.group(1).lower() == "lower")

    def to_c(v: float) -> float:
        return (v - 32.0) * 5.0 / 9.0 if unit == "F" else v

    low_c  = to_c(min(val1, val2))
    high_c = to_c(max(val1, val2))
    if val1 == val2:
        high_c = to_c(val1 + 1)

    target_date = _parse_date(question)
    if target_date is None:
        return None

    return {
        "city": city, "lat": lat, "lon": lon,
        "temp_low_c": round(low_c, 4), "temp_high_c": round(high_c, 4),
        "is_upper_tail": is_upper_tail, "is_lower_tail": is_lower_tail,
        "is_max": is_max, "target_date": target_date, "unit": unit,
    }


# ---------------------------------------------------------------------------
# Open-Meteo API — multi-model parallel fetch
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1024)
def _fetch_model_cached(
    lat: float,
    lon: float,
    date_iso: str,
    utc_hour: int,
    model: str,
    with_current: bool,
) -> Optional[tuple[float, float, Optional[float]]]:
    """Fetch (max_c, min_c, current_c_or_None) for one NWP model. Cached per hour."""
    current_param = "&current=temperature_2m" if with_current else ""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&daily=temperature_2m_max,temperature_2m_min"
        f"{current_param}"
        "&timezone=auto&forecast_days=10"
        f"&models={model}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polymarket-weather-bot/2.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None

    daily = data.get("daily", {})
    dates = daily.get("time", [])
    try:
        idx = dates.index(date_iso)
        max_c = float(daily["temperature_2m_max"][idx])
        min_c = float(daily["temperature_2m_min"][idx])
    except (ValueError, IndexError, KeyError, TypeError):
        return None

    current_c: Optional[float] = None
    if with_current:
        try:
            current_c = float(data["current"]["temperature_2m"])
        except (KeyError, TypeError):
            pass

    return (max_c, min_c, current_c)


def _fetch_consensus(
    lat: float, lon: float, target_date: date
) -> Optional[tuple[float, float, float, Optional[float]]]:
    """
    Fetch GFS + ECMWF + best-match in parallel.

    Returns (mean_max_c, mean_min_c, sigma_c, current_c) or None if:
      - fewer than MIN_MODELS respond, or
      - model spread exceeds MAX_SPREAD_C (too uncertain to trade).

    sigma_c reflects actual model disagreement:
      sigma = max(model_std_dev × 1.5, horizon_floor)
    where horizon_floor = 0.8 + 0.25 × days_out.
    This produces a tight σ when models agree and a wide σ when they don't.
    """
    lat_r = round(lat, 4)
    lon_r = round(lon, 4)
    date_iso = target_date.isoformat()
    utc_hour = datetime.now(timezone.utc).hour

    model_args = [
        (lat_r, lon_r, date_iso, utc_hour, _MODELS[0], True),   # best_match + current
        (lat_r, lon_r, date_iso, utc_hour, _MODELS[1], False),  # ecmwf
        (lat_r, lon_r, date_iso, utc_hour, _MODELS[2], False),  # gfs
    ]

    # Check cache first — avoids spawning threads on warm ticks
    results: list[tuple[float, float, Optional[float]]] = []
    to_fetch: list[tuple] = []
    for args in model_args:
        cached = _fetch_model_cached(*args)
        if cached is not None:
            results.append(cached)
        else:
            to_fetch.append(args)

    # Fetch uncached models in parallel
    if to_fetch:
        def _call(args: tuple) -> Optional[tuple]:
            return _fetch_model_cached(*args)

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(to_fetch)) as pool:
            for r in pool.map(_call, to_fetch):
                if r is not None:
                    results.append(r)

    if len(results) < MIN_MODELS:
        return None  # not enough models responded

    maxes   = [r[0] for r in results]
    mins    = [r[1] for r in results]
    current_c = next((r[2] for r in results if r[2] is not None), None)

    # Reject if models disagree too much on the daily max
    spread = max(maxes) - min(maxes)
    if spread > MAX_SPREAD_C:
        return None

    mean_max = sum(maxes) / len(maxes)
    mean_min = sum(mins)  / len(mins)

    # σ from model ensemble spread — the real uncertainty signal
    n = len(maxes)
    model_std = math.sqrt(sum((x - mean_max) ** 2 for x in maxes) / n)

    today = datetime.now(timezone.utc).date()
    days_out = max(0, (target_date - today).days)
    horizon_floor = 0.8 + 0.25 * days_out  # minimum grows with forecast horizon

    sigma = max(model_std * 1.5, horizon_floor)

    return (mean_max, mean_min, sigma, current_c, max(maxes))


def fetch_forecast_temp(
    lat: float, lon: float, target_date: date
) -> Optional[tuple[float, float]]:
    """
    Public compat shim — returns (forecast_max_c, forecast_min_c) using the
    multi-model consensus mean, or None.  Tests that mock this function still work.
    """
    result = _fetch_consensus(lat, lon, target_date)
    if result is None:
        return None
    mean_max, mean_min, _sigma, _current, _max_model = result
    return (mean_max, mean_min)


# ---------------------------------------------------------------------------
# Probability computation
# ---------------------------------------------------------------------------

def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bracket_yes_prob(forecast_c: float, sigma_c: float, low_c: float, high_c: float) -> float:
    return (
        _normal_cdf((high_c - forecast_c) / sigma_c)
        - _normal_cdf((low_c  - forecast_c) / sigma_c)
    )


def _solar_hour(lon: float) -> float:
    """Approximate local solar hour from UTC time + longitude (±15 min accuracy)."""
    now = datetime.now(timezone.utc)
    return (now.hour + now.minute / 60.0 + lon / 15.0) % 24.0


def forecast_outcome_probability(
    parsed: dict, outcome: str, min_bracket_margin_c: float = 0.0
) -> Optional[float]:
    """
    Return model P(outcome correct) for the temperature market in `parsed`.

    Uses multi-model consensus (GFS + ECMWF + best-match) with σ derived from
    actual model spread instead of a fixed formula.  Returns None on any API
    failure (fail-open: caller allows the trade when None is returned).

    Intraday kill-switch (same-day markets only, daily-max questions):
      • If it is already past solar 3PM AND the current temperature is more than
        INTRADAY_IMPOSSIBLE_BUFFER_C below the bracket low, the daily max cannot
        physically reach the bracket → returns a near-certain No probability.
      • If the current temperature has already exceeded the bracket high for a
        bracket ("be X°C") market, the daily max is above the bracket → No wins.
      • If the current temperature has already exceeded the bracket low for an
        upper-tail ("or higher") market, Yes is near-certain → No loses.
    """
    target_date = parsed.get("target_date")
    if not target_date:
        return None

    consensus = _fetch_consensus(parsed["lat"], parsed["lon"], target_date)
    if consensus is None:
        return None

    mean_max, mean_min, sigma_c, current_c, max_model_c = consensus
    forecast_c = mean_max if parsed.get("is_max", True) else mean_min

    low  = parsed["temp_low_c"]
    high = parsed["temp_high_c"]

    # Bracket margin guard: for "No" bets, skip if models are too close to the
    # bracket threshold (Qingdao lesson 2026-06-28: ECMWF 28.1°C vs 29°C → loss).
    if min_bracket_margin_c > 0 and outcome.strip().lower() == "no" and parsed.get("is_max", True):
        is_upper_tail = parsed.get("is_upper_tail")
        is_lower_tail = parsed.get("is_lower_tail")
        if is_upper_tail:
            # ≥X°C markets: guard fires when hottest model is below-but-close to
            # threshold. When forecast exceeds threshold, edge gate blocks it instead
            # (predicted No prob ≈ 0%). Negative margin (forecast above threshold)
            # is NOT guarded here — the edge gate already handles it.
            threshold = low
            margin = threshold - max_model_c
            if 0 <= margin < min_bracket_margin_c:
                return None
        elif not is_lower_tail:
            # Exact-bracket "be X°C" markets: guard fires when the model CONSENSUS
            # (mean_max) is within min_bracket_margin_c of the bracket's nominal
            # degree (low). Unlike ≥X°C, being above OR near the bracket puts the
            # forecast inside the bucket — "No" loses. Use mean_max not max_model_c
            # so one outlier model far above the bracket can't mask two others that
            # land squarely in it (Munich lesson 2026-06-29: BM 29.8°C masked
            # ECMWF 27.9°C + GFS 27.3°C, both inside the 27°C bucket).
            if abs(mean_max - low) < min_bracket_margin_c:
                return None
    is_max = parsed.get("is_max", True)

    # ── Intraday kill-switch ─────────────────────────────────────────────────
    today = datetime.now(timezone.utc).date()
    if target_date == today and current_c is not None and is_max:
        solar_hr = _solar_hour(parsed["lon"])

        # After solar 3PM temperature has almost certainly peaked
        if solar_hr >= 15.0:
            BUFFER = 4.0  # °C — generous afternoon rise budget

            if not parsed.get("is_upper_tail") and not parsed.get("is_lower_tail"):
                # Bracket market: Yes = temp ∈ [low, high)
                if current_c + BUFFER < low:
                    # Temperature can't reach bracket floor — No near-certain
                    p_outcome = 0.99 if outcome.strip().lower() == "no" else 0.01
                    return round(p_outcome, 6)
                if current_c > high:
                    # Daily max already above bracket ceiling — No near-certain
                    p_outcome = 0.99 if outcome.strip().lower() == "no" else 0.01
                    return round(p_outcome, 6)

            elif parsed.get("is_upper_tail"):
                # Upper-tail market: Yes = temp >= low
                if current_c > low:
                    # Already exceeded threshold — Yes near-certain
                    p_outcome = 0.01 if outcome.strip().lower() == "no" else 0.99
                    return round(p_outcome, 6)
                if current_c + BUFFER < low:
                    # Can't reach threshold today — No near-certain
                    p_outcome = 0.99 if outcome.strip().lower() == "no" else 0.01
                    return round(p_outcome, 6)

    # ── Normal probability via Gaussian model ───────────────────────────────
    if parsed.get("is_upper_tail"):
        p_yes = 1.0 - _normal_cdf((low  - forecast_c) / sigma_c)
    elif parsed.get("is_lower_tail"):
        p_yes = _normal_cdf((high - forecast_c) / sigma_c)
    else:
        p_yes = _bracket_yes_prob(forecast_c, sigma_c, low, high)

    p_no = 1.0 - p_yes
    p_outcome = p_no if outcome.strip().lower() == "no" else p_yes
    return round(max(0.0, min(1.0, p_outcome)), 6)
