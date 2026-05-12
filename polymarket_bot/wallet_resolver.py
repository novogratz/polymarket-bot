"""Resolve mirror-target tokens (usernames, Polymarket profile URLs, or
``0x`` addresses) into proxy-wallet addresses.

Used by the mirror loop so a user can write a profile like::

    target = "bossoskil1, https://polymarket.com/profile/0xABC, 0xDEF"

instead of having to look up each username's wallet by hand.

Username resolution hits the Data API leaderboard across several
categories/periods and is cached locally so re-launching the bot doesn't
re-query the API. Unresolved usernames are kept out of the final list
(and logged so the operator can investigate).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .smart_money import DataApiClient

_ADDRESS_RE = re.compile(r"^0[xX][0-9a-fA-F]{40}$")
_URL_ADDRESS_RE = re.compile(r"(0[xX][0-9a-fA-F]{40})")

_CATEGORIES = (
    "OVERALL", "POLITICS", "SPORTS", "FINANCE",
    "ECONOMICS", "TECH", "CULTURE", "WEATHER",
)
_PERIODS = ("ALL", "MONTH", "WEEK", "DAY")


def is_address(token: str) -> bool:
    return bool(_ADDRESS_RE.match(token.strip()))


def extract_address_from_url(token: str) -> str | None:
    """Extract a 0x address from a URL-like string, or ``None``."""
    match = _URL_ADDRESS_RE.search(token)
    return match.group(1).lower() if match else None


def load_cache(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k).lower(): str(v).lower() for k, v in data.items() if isinstance(v, str)}


def save_cache(path: Path, cache: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {k.lower(): v.lower() for k, v in cache.items()}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def resolve_username(
    username: str,
    api: DataApiClient,
    cache: dict[str, str],
    *,
    sleep_between: float = 0.5,
    verbose: bool = True,
) -> str | None:
    """Resolve ``username`` to its proxy-wallet address.

    Checks the cache first. On miss, iterates the leaderboard across the
    known categories × periods and updates the cache. Returns ``None`` if
    the username is not in any visited leaderboard slice.
    """
    key = username.strip().lower()
    if not key:
        return None
    if key in cache:
        return cache[key]
    for category in _CATEGORIES:
        for period in _PERIODS:
            try:
                traders = api.leaderboard(
                    category=category, time_period=period, limit=500
                )
            except Exception:
                time.sleep(sleep_between * 2)
                continue
            for trader in traders:
                trader_name = (trader.username or "").lower()
                if not trader_name:
                    continue
                if trader_name == key:
                    cache[key] = trader.wallet.lower()
                    if verbose:
                        print(
                            f"[wallet-resolver] {username} → {trader.wallet}",
                            flush=True,
                        )
                    return trader.wallet.lower()
                # Cache every seen username while we're here — speeds up
                # subsequent resolutions in the same process.
                cache.setdefault(trader_name, trader.wallet.lower())
            time.sleep(sleep_between)
    return None


def resolve_target(
    token: str,
    api: DataApiClient,
    cache: dict[str, str],
    *,
    verbose: bool = True,
    sleep_between: float = 0.5,
) -> str | None:
    """Resolve a single token to an address.

    Accepts: raw ``0x...`` (passthrough), profile URL (extract), or username.
    """
    token = token.strip()
    if not token:
        return None
    if is_address(token):
        return token.lower()
    url_addr = extract_address_from_url(token)
    if url_addr:
        return url_addr
    return resolve_username(
        token, api, cache, verbose=verbose, sleep_between=sleep_between
    )


def resolve_all(
    raw: str,
    *,
    cache_path: Path,
    api: DataApiClient | None = None,
    verbose: bool = True,
    sleep_between: float = 0.5,
) -> tuple[list[str], list[str]]:
    """Resolve a CSV of mixed tokens into ``(addresses, unresolved)``.

    Side effects: writes the updated cache back to ``cache_path``.
    """
    if not raw:
        return [], []
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    api = api or DataApiClient()
    cache = load_cache(cache_path)
    resolved: list[str] = []
    seen: set[str] = set()
    unresolved: list[str] = []
    for token in tokens:
        addr = resolve_target(
            token, api, cache, verbose=verbose, sleep_between=sleep_between
        )
        if not addr:
            unresolved.append(token)
            continue
        if addr in seen:
            continue
        seen.add(addr)
        resolved.append(addr)
    try:
        save_cache(cache_path, cache)
    except Exception as exc:
        if verbose:
            print(f"[wallet-resolver] cache save failed: {exc}", flush=True)
    return resolved, unresolved
