"""
Weather forecast integration for the weather-only grinder bot.

Uses Open-Meteo (https://open-meteo.com, free, no API key required) to fetch
daily max/min temperature forecasts and compute the probability that a Polymarket
temperature bracket market resolves "No" (i.e. the actual temperature falls
OUTSIDE the market's stated range).

Edge formula:
    forecast_P(outcome_correct) − market_ask ≥ race_weather_forecast_min_edge

Fail-open: if the question cannot be parsed or Open-Meteo is unreachable,
the function returns None and the trade is allowed (normal price filters apply).

Caching: Open-Meteo is queried at most once per (city, date, UTC-hour) to keep
tick overhead near zero after the first API call.
"""
from __future__ import annotations

import json
import math
import re
import urllib.request
from datetime import date, datetime, timezone
from functools import lru_cache
from typing import Optional

# ---------------------------------------------------------------------------
# City → (lat, lon) lookup
# Covering major cities that appear in Polymarket temperature markets.
# Sorted longest-name-first at lookup time to avoid partial collisions.
# ---------------------------------------------------------------------------
CITY_COORDS: dict[str, tuple[float, float]] = {
    "los angeles": (34.0522, -118.2437),
    "new york": (40.7128, -74.0060),
    "san antonio": (29.4241, -98.4936),
    "san diego": (32.7157, -117.1611),
    "san jose": (37.3382, -121.8863),
    "mexico city": (19.4326, -99.1332),
    "sao paulo": (-23.5505, -46.6333),
    "rio de janeiro": (-22.9068, -43.1729),
    "buenos aires": (-34.6037, -58.3816),
    "ho chi minh": (10.8231, 106.6297),
    "kuala lumpur": (3.1390, 101.6869),
    "hong kong": (22.3193, 114.1694),
    "cape town": (-33.9249, 18.4241),
    "saint petersburg": (59.9311, 30.3609),
    "abu dhabi": (24.4539, 54.3773),
    "tel aviv": (32.0853, 34.7818),
    "london": (51.5074, -0.1278),
    "paris": (48.8566, 2.3522),
    "istanbul": (41.0082, 28.9784),
    "madrid": (40.4168, -3.7038),
    "berlin": (52.5200, 13.4050),
    "rome": (41.9028, 12.4964),
    "amsterdam": (52.3676, 4.9041),
    "barcelona": (41.3851, 2.1734),
    "vienna": (48.2082, 16.3738),
    "zurich": (47.3769, 8.5417),
    "brussels": (50.8503, 4.3517),
    "stockholm": (59.3293, 18.0686),
    "oslo": (59.9139, 10.7522),
    "copenhagen": (55.6761, 12.5683),
    "athens": (37.9838, 23.7275),
    "warsaw": (52.2297, 21.0122),
    "prague": (50.0755, 14.4378),
    "budapest": (47.4979, 19.0402),
    "bucharest": (44.4268, 26.1025),
    "chicago": (41.8781, -87.6298),
    "houston": (29.7604, -95.3698),
    "phoenix": (33.4484, -112.0740),
    "dallas": (32.7767, -96.7970),
    "austin": (30.2672, -97.7431),
    "seattle": (47.6062, -122.3321),
    "denver": (39.7392, -104.9903),
    "boston": (42.3601, -71.0589),
    "miami": (25.7617, -80.1918),
    "atlanta": (33.7490, -84.3880),
    "minneapolis": (44.9778, -93.2650),
    "toronto": (43.6510, -79.3470),
    "montreal": (45.5017, -73.5673),
    "vancouver": (49.2827, -123.1207),
    "bogota": (4.7110, -74.0721),
    "lima": (-12.0464, -77.0428),
    "santiago": (-33.4489, -70.6693),
    "tokyo": (35.6762, 139.6503),
    "osaka": (34.6937, 135.5023),
    "seoul": (37.5665, 126.9780),
    "beijing": (39.9042, 116.4074),
    "shanghai": (31.2304, 121.4737),
    "guangzhou": (23.1291, 113.2644),
    "shenzhen": (22.5431, 114.0579),
    "chengdu": (30.5728, 104.0668),
    "wuhan": (30.5928, 114.3055),
    "taipei": (25.0330, 121.5654),
    "bangkok": (13.7563, 100.5018),
    "singapore": (1.3521, 103.8198),
    "jakarta": (-6.2088, 106.8456),
    "manila": (14.5995, 120.9842),
    "hanoi": (21.0285, 105.8542),
    "mumbai": (19.0760, 72.8777),
    "delhi": (28.6139, 77.2090),
    "bangalore": (12.9716, 77.5946),
    "hyderabad": (17.3850, 78.4867),
    "kolkata": (22.5726, 88.3639),
    "chennai": (13.0827, 80.2707),
    "karachi": (24.8607, 67.0011),
    "lahore": (31.5204, 74.3587),
    "dhaka": (23.8103, 90.4125),
    "cairo": (30.0444, 31.2357),
    "johannesburg": (-26.2041, 28.0473),
    "lagos": (6.5244, 3.3792),
    "nairobi": (-1.2921, 36.8219),
    "casablanca": (33.5731, -7.5898),
    "algiers": (36.7372, 3.0863),
    "tunis": (36.8065, 10.1815),
    "accra": (5.6037, -0.1870),
    "dubai": (25.2048, 55.2708),
    "riyadh": (24.7136, 46.6753),
    "jeddah": (21.2854, 39.2376),
    "doha": (25.2854, 51.5310),
    "tehran": (35.6892, 51.3890),
    "baghdad": (33.3152, 44.3661),
    "amman": (31.9454, 35.9284),
    "beirut": (33.8938, 35.5018),
    "moscow": (55.7558, 37.6173),
    "kyiv": (50.4501, 30.5234),
    "minsk": (53.9045, 27.5615),
    "sydney": (-33.8688, 151.2093),
    "melbourne": (-37.8136, 144.9631),
    "brisbane": (-27.4698, 153.0251),
    "perth": (-31.9505, 115.8605),
    "auckland": (-36.8509, 174.7645),
}

# Sorted longest-name-first to avoid "paris" matching "paris, texas"
_CITY_LIST: list[tuple[str, float, float]] = sorted(
    ((city, lat, lon) for city, (lat, lon) in CITY_COORDS.items()),
    key=lambda x: -len(x[0]),
)

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
    month_name = m.group(1).lower()[:3]
    month = _MONTH_MAP.get(month_name)
    if not month:
        return None
    day = int(m.group(2))
    year = int(m.group(3)) if m.group(3) else datetime.now(timezone.utc).year
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_weather_question(question: str) -> Optional[dict]:
    """
    Parse a Polymarket temperature market question into structured data.

    Returns a dict with:
        city, lat, lon,
        temp_low_c, temp_high_c  — inclusive bracket in °C,
        is_upper_tail            — "or higher" market (Yes = temp >= low),
        is_lower_tail            — "or lower" market (Yes = temp <= high),
        is_max                   — True = daily high, False = daily low,
        target_date              — datetime.date of resolution,
        unit                     — 'C' or 'F' (original unit in question).

    Returns None if any required field cannot be parsed.
    """
    ql = question.lower()

    # daily high vs low
    is_max = "highest" in ql or "high temperature" in ql or "maximum" in ql

    # city lookup (longest name first)
    city_match: Optional[tuple[str, float, float]] = None
    for city, lat, lon in _CITY_LIST:
        if city in ql:
            city_match = (city, lat, lon)
            break
    if city_match is None:
        return None
    city, lat, lon = city_match

    # temperature range / value
    m = _TEMP_RE.search(question)
    if not m:
        return None
    val1 = float(m.group(1))
    val2 = float(m.group(2)) if m.group(2) else val1
    unit_raw = m.group(3).replace("°", "").replace(" ", "").upper()
    unit = unit_raw[0] if unit_raw and unit_raw[0] in ("C", "F") else "C"

    # tail detection ("or higher" / "or lower")
    tail_m = _TAIL_RE.search(question[m.end():])  # only after the temp token
    is_upper_tail = bool(tail_m and tail_m.group(1).lower() == "higher")
    is_lower_tail = bool(tail_m and tail_m.group(1).lower() == "lower")

    def to_c(v: float) -> float:
        return (v - 32.0) * 5.0 / 9.0 if unit == "F" else v

    low_c  = to_c(min(val1, val2))
    high_c = to_c(max(val1, val2))

    # For a single-degree bracket ("38°C" or "68°F"), extend the top by 1 unit
    # so the bracket width reflects the resolution rule (e.g. "38°C" = [38, 39)).
    if val1 == val2:
        high_c = to_c(val1 + 1)

    target_date = _parse_date(question)
    if target_date is None:
        return None

    return {
        "city": city,
        "lat": lat,
        "lon": lon,
        "temp_low_c": round(low_c, 4),
        "temp_high_c": round(high_c, 4),
        "is_upper_tail": is_upper_tail,
        "is_lower_tail": is_lower_tail,
        "is_max": is_max,
        "target_date": target_date,
        "unit": unit,
    }


# ---------------------------------------------------------------------------
# Open-Meteo API fetch
# ---------------------------------------------------------------------------

@lru_cache(maxsize=512)
def _fetch_open_meteo_cached(
    lat: float,
    lon: float,
    target_date_iso: str,
    utc_hour_bucket: int,  # refreshes cache once per UTC hour
) -> Optional[tuple[float, float]]:
    """Return (temp_max_c, temp_min_c) for target_date_iso, or None on error."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&daily=temperature_2m_max,temperature_2m_min"
        "&timezone=auto"
        "&forecast_days=10"
    )
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "polymarket-weather-bot/1.0"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None

    daily = data.get("daily", {})
    dates = daily.get("time", [])
    maxes = daily.get("temperature_2m_max", [])
    mins  = daily.get("temperature_2m_min", [])

    try:
        idx = dates.index(target_date_iso)
        return (float(maxes[idx]), float(mins[idx]))
    except (ValueError, IndexError, TypeError):
        return None


def fetch_forecast_temp(lat: float, lon: float, target_date: date) -> Optional[tuple[float, float]]:
    """
    Return (forecast_max_c, forecast_min_c) for (lat, lon) on target_date,
    or None if Open-Meteo is unavailable or the date is out of range.
    Cache refreshes once per UTC hour.
    """
    utc_hour = datetime.now(timezone.utc).hour
    return _fetch_open_meteo_cached(
        round(lat, 4),
        round(lon, 4),
        target_date.isoformat(),
        utc_hour,
    )


# ---------------------------------------------------------------------------
# Probability computation
# ---------------------------------------------------------------------------

def _normal_cdf(x: float) -> float:
    """Standard Normal CDF via the math.erf approximation (accurate to 1e-7)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bracket_yes_prob(forecast_c: float, sigma_c: float, low_c: float, high_c: float) -> float:
    """P(low_c <= temp <= high_c) under Normal(forecast_c, sigma_c)."""
    return (
        _normal_cdf((high_c - forecast_c) / sigma_c)
        - _normal_cdf((low_c  - forecast_c) / sigma_c)
    )


def forecast_outcome_probability(parsed: dict, outcome: str) -> Optional[float]:
    """
    Return the model probability that `outcome` ("Yes" or "No") is correct for
    the temperature market described by `parsed` (from parse_weather_question).

    Uses Open-Meteo daily max/min + a horizon-scaled Gaussian uncertainty model:
        σ ≈ 1.5°C for a 0-day forecast, +0.4°C per additional day out.
    Typical single-bracket width is 1°C / 1.8°F; the uncertainty dominates
    anything beyond ~1 day, making the forecast meaningfully discriminating.

    Returns None if the forecast cannot be fetched or inputs are invalid.
    Fail-open: the caller should allow the trade when None is returned.
    """
    target_date = parsed.get("target_date")
    if not target_date:
        return None

    temps = fetch_forecast_temp(parsed["lat"], parsed["lon"], target_date)
    if temps is None:
        return None

    forecast_max_c, forecast_min_c = temps
    forecast_c = forecast_max_c if parsed.get("is_max", True) else forecast_min_c

    today = datetime.now(timezone.utc).date()
    days_out = max(0, (target_date - today).days)
    sigma_c = 1.5 + days_out * 0.4  # °C; day-0: 1.5, day-1: 1.9, day-3: 2.7

    low  = parsed["temp_low_c"]
    high = parsed["temp_high_c"]

    if parsed.get("is_upper_tail"):
        # Yes = temp >= low_c
        p_yes = 1.0 - _normal_cdf((low - forecast_c) / sigma_c)
    elif parsed.get("is_lower_tail"):
        # Yes = temp <= high_c
        p_yes = _normal_cdf((high - forecast_c) / sigma_c)
    else:
        # Bracket: Yes = temp ∈ [low, high]
        p_yes = _bracket_yes_prob(forecast_c, sigma_c, low, high)

    p_no = 1.0 - p_yes
    p_outcome = p_no if outcome.strip().lower() == "no" else p_yes
    return round(max(0.0, min(1.0, p_outcome)), 6)
