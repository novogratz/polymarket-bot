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

    def get_markets_by_clob_token_ids(self, token_ids: list[str]) -> list[dict[str, Any]]:
        if not token_ids:
            return []
        pairs: list[tuple[str, str]] = [("clob_token_ids", token) for token in token_ids if token]
        if not pairs:
            return []
        pairs.extend(
            [
                ("active", "true"),
                ("closed", "false"),
                ("limit", str(max(len(token_ids) * 2, 200))),
            ]
        )
        return self._get_json(f"/markets?{urllib.parse.urlencode(pairs)}")

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
