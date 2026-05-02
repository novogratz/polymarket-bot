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
        params = urllib.parse.urlencode(query)
        return self._get_json(f"/markets?{params}")

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
