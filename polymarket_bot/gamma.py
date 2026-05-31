"""HTTP client for the Polymarket Gamma API.

Provides a paginated active-markets endpoint used by the standard scan, and
a chunked ``get_markets_by_clob_token_ids`` lookup used by the smart-money
reverse-lookup so that arbitrarily large token-id lists do not blow past the
URL length limit.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any


class GammaClient:
    def __init__(self, base_url: str = "https://gamma-api.polymarket.com", timeout: int = 15) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get_markets(
        self,
        *,
        active: bool = True,
        closed: bool = False,
        limit: int = 200,
        order: str = "end_date",
        ascending: bool = True,
        end_date_min: datetime | None = None,
        end_date_max: datetime | None = None,
        question_contains: str | None = None,
    ) -> list[dict[str, Any]]:
        query: dict[str, str] = {
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "limit": str(limit),
            "order": order,
            "ascending": str(ascending).lower(),
        }
        if end_date_min is not None:
            query["end_date_min"] = end_date_min.isoformat()
        if end_date_max is not None:
            query["end_date_max"] = end_date_max.isoformat()
        if question_contains:
            query["question_contains"] = question_contains
        params = urllib.parse.urlencode(query)
        return self._get_json(f"/markets?{params}")

    def get_markets_by_clob_token_ids(
        self,
        token_ids: list[str],
        *,
        batch_size: int = 20,
    ) -> list[dict[str, Any]]:
        cleaned = [token for token in token_ids if token]
        if not cleaned:
            return []
        results: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for start in range(0, len(cleaned), max(1, batch_size)):
            batch = cleaned[start : start + batch_size]
            pairs: list[tuple[str, str]] = [("clob_token_ids", token) for token in batch]
            pairs.extend(
                [
                    ("active", "true"),
                    ("closed", "false"),
                    ("limit", str(max(len(batch) * 2, 50))),
                ]
            )
            try:
                payload = self._get_json(f"/markets?{urllib.parse.urlencode(pairs)}")
            except Exception:
                continue
            if not isinstance(payload, list):
                continue
            for market in payload:
                if not isinstance(market, dict):
                    continue
                key = str(market.get("id") or market.get("conditionId") or "")
                if key and key in seen_ids:
                    continue
                if key:
                    seen_ids.add(key)
                results.append(market)
        return results

    def is_market_resolved(self, token_id: str) -> bool:
        """Return True if Polymarket has officially resolved this market.

        Queries Gamma without active/closed filters so resolved markets are
        included. A market is considered resolved when closed=true and
        active=false. Used to verify a near-zero price is a genuine loss
        (game over) vs a thin-book price spike mid-game.
        """
        if not token_id:
            return False
        try:
            pairs = [
                ("clob_token_ids", token_id),
                ("closed", "true"),
                ("limit", "5"),
            ]
            payload = self._get_json(f"/markets?{urllib.parse.urlencode(pairs)}")
            if not isinstance(payload, list):
                return False
            for market in payload:
                if not isinstance(market, dict):
                    continue
                # Check all token slots for this market
                tokens = market.get("clobTokenIds") or []
                if isinstance(tokens, str):
                    try:
                        import json as _json
                        tokens = _json.loads(tokens)
                    except Exception:
                        tokens = [tokens]
                if token_id in tokens or str(market.get("id") or "") == token_id:
                    closed = market.get("closed") in (True, "true", "True", 1)
                    active = market.get("active") in (True, "true", "True", 1)
                    return closed and not active
        except Exception:
            pass
        return False

    def _get_json(self, path: str) -> Any:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            headers={
                "Accept": "application/json",
                "User-Agent": "polymarket-bot/0.1",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))
