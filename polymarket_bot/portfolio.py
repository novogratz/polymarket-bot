from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .models import Candidate, utc_now


@dataclass
class Portfolio:
    cash: float
    positions: list[dict[str, Any]]

    @classmethod
    def load(cls, path: Path, starting_cash: float) -> "Portfolio":
        if not path.exists():
            return cls(cash=starting_cash, positions=[])
        data = json.loads(path.read_text())
        return cls(cash=float(data.get("cash", starting_cash)), positions=list(data.get("positions", [])))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"cash": self.cash, "positions": self.positions}, indent=2, sort_keys=True))

    def has_open_position(self, market_id: str, outcome: str) -> bool:
        return any(
            position.get("market_id") == market_id
            and position.get("outcome") == outcome
            and position.get("status") == "open"
            for position in self.positions
        )

    def open_paper_position(self, candidate: Candidate, stake: float) -> dict[str, Any] | None:
        if stake <= 0.0 or stake > self.cash or self.has_open_position(candidate.market_id, candidate.outcome):
            return None
        shares = stake / candidate.price
        self.cash = round(self.cash - stake, 2)
        position = {
            "status": "open",
            "opened_at": utc_now().isoformat(),
            "market_id": candidate.market_id,
            "question": candidate.question,
            "slug": candidate.slug,
            "url": candidate.url,
            "outcome": candidate.outcome,
            "token_id": candidate.token_id,
            "entry_price": candidate.price,
            "current_price": candidate.price,
            "stake": round(stake, 2),
            "shares": shares,
            "unrealized_pnl": 0.0,
        }
        self.positions.append(position)
        return position

    def mark_to_market(self, candidates: list[Candidate]) -> None:
        by_token = {candidate.token_id: candidate for candidate in candidates if candidate.token_id}
        by_market_outcome = {(candidate.market_id, candidate.outcome): candidate for candidate in candidates}
        for position in self.positions:
            if position.get("status") != "open":
                continue
            candidate = None
            token_id = position.get("token_id")
            if token_id:
                candidate = by_token.get(token_id)
            if candidate is None:
                candidate = by_market_outcome.get((position.get("market_id"), position.get("outcome")))
            if candidate is None:
                continue
            current_value = float(position["shares"]) * candidate.price
            position["current_price"] = candidate.price
            position["unrealized_pnl"] = round(current_value - float(position["stake"]), 2)

    def summary(self) -> dict[str, Any]:
        open_positions = [position for position in self.positions if position.get("status") == "open"]
        invested = sum(float(position.get("stake", 0.0)) for position in open_positions)
        unrealized = sum(float(position.get("unrealized_pnl", 0.0)) for position in open_positions)
        return {
            "cash": round(self.cash, 2),
            "invested": round(invested, 2),
            "unrealized_pnl": round(unrealized, 2),
            "equity": round(self.cash + invested + unrealized, 2),
            "open_positions": len(open_positions),
        }


def paper_tick(candidates: list[Candidate], settings: Settings) -> tuple[Portfolio, dict[str, Any] | None]:
    portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)
    portfolio.mark_to_market(candidates)
    opened = None
    if candidates:
        top = candidates[0]
        stake = min(portfolio.cash, settings.max_position_usd)
        opened = portfolio.open_paper_position(top, stake)
    portfolio.save(settings.state_path)
    return portfolio, opened
