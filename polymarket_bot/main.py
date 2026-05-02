from __future__ import annotations

import argparse
import json

from datetime import timedelta

from .config import Settings
from .dashboard import serve
from .gamma import GammaClient
from .models import utc_now
from .portfolio import paper_tick
from .strategy import rank_markets


def load_candidates(settings: Settings):
    client = GammaClient(settings.gamma_base_url)
    now = utc_now()
    markets = client.get_markets(
        limit=settings.scan_limit,
        end_date_min=now,
        end_date_max=now + timedelta(hours=settings.soon_hours),
    )
    return rank_markets(markets, settings)


def scan(settings: Settings) -> list[dict[str, object]]:
    return [candidate.to_dict() for candidate in load_candidates(settings)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket read-only scanner and paper dashboard")
    parser.add_argument("command", choices=["scan", "paper-tick", "dashboard"])
    parser.add_argument("--limit", type=int, default=20, help="Rows to print for scan/paper-tick")
    args = parser.parse_args()

    settings = Settings()
    if args.command == "scan":
        print(json.dumps(scan(settings)[: args.limit], indent=2))
    elif args.command == "paper-tick":
        candidates = load_candidates(settings)
        portfolio, opened = paper_tick(candidates, settings)
        print(json.dumps({"opened": opened, "summary": portfolio.summary()}, indent=2))
    else:
        serve(settings)


if __name__ == "__main__":
    main()
