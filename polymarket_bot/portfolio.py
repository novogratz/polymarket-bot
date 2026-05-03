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
    pending_orders: list[dict[str, Any]] | None = None

    @classmethod
    def load(cls, path: Path, starting_cash: float) -> "Portfolio":
        if not path.exists():
            return cls(cash=starting_cash, positions=[], pending_orders=[])
        data = json.loads(path.read_text())
        return cls(
            cash=float(data.get("cash", starting_cash)),
            positions=list(data.get("positions", [])),
            pending_orders=list(data.get("pending_orders", [])),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"cash": self.cash, "positions": self.positions, "pending_orders": self.pending_orders or []},
                indent=2,
                sort_keys=True,
            )
        )

    def has_open_position(self, market_id: str, outcome: str | None = None) -> bool:
        """
        Returns True if any outcome for this market is open.
        If outcome is provided, it specifically checks for that outcome (used internally).
        """
        return any(
            position.get("market_id") == market_id
            and (outcome is None or position.get("outcome") == outcome)
            and position.get("status") == "open"
            for position in self.positions
        )

    def has_exact_position(self, market_id: str, outcome: str) -> bool:
        return self.has_open_position(market_id, outcome=outcome)

    def has_open_token(self, token_id: str | None) -> bool:
        if not token_id:
            return False
        return any(
            position.get("token_id") == token_id
            and position.get("status") == "open"
            for position in self.positions
        )

    def has_pending_token(self, token_id: str | None) -> bool:
        if not token_id:
            return False
        return any(
            order.get("token_id") == token_id
            and order.get("status") == "live"
            for order in (self.pending_orders or [])
        )

    def open_paper_position(self, candidate: Candidate, stake: float, *, entry_price: float | None = None) -> dict[str, Any] | None:
        if stake <= 0.0 or stake > self.cash or self.has_open_position(candidate.market_id, candidate.outcome):
            return None
        position = self._build_position(candidate, stake, entry_price=entry_price)
        self.cash = round(self.cash - stake, 2)
        self.positions.append(position)
        return position

    def record_pending_order(
        self,
        candidate: Candidate,
        stake: float,
        *,
        entry_price: float,
        size: float,
        order_id: str | None,
        order_response: Any = None,
        strategy: str | None = None,
        signal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.pending_orders is None:
            self.pending_orders = []
        pending = {
            "status": "live",
            "created_at": utc_now().isoformat(),
            "market_id": candidate.market_id,
            "question": candidate.question,
            "slug": candidate.slug,
            "url": candidate.url,
            "outcome": candidate.outcome,
            "token_id": candidate.token_id,
            "price": entry_price,
            "stake": round(stake, 2),
            "size": size,
            "order_id": order_id,
            "order_response": order_response,
        }
        if strategy:
            pending["strategy"] = strategy
        if signal is not None:
            pending["signal"] = signal
        self.pending_orders.append(pending)
        return pending

    def record_live_position(
        self,
        candidate: Candidate,
        stake: float,
        *,
        entry_price: float | None = None,
        order_id: str | None = None,
        order_response: Any = None,
    ) -> dict[str, Any] | None:
        if stake <= 0.0 or self.has_open_position(candidate.market_id, candidate.outcome):
            return None
        position = self._build_position(candidate, stake, entry_price=entry_price)
        position["live"] = True
        position["order_id"] = order_id
        position["order_response"] = order_response
        self.cash = round(max(0.0, self.cash - stake), 2)
        self.positions.append(position)
        return position

    def record_live_exit(
        self,
        position: dict[str, Any],
        *,
        shares: float,
        exit_price: float,
        order_id: str | None = None,
        order_response: Any = None,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        current_shares = float(position.get("shares", 0.0))
        if shares <= 0.0 or exit_price <= 0.0 or current_shares <= 0.0:
            return None
        sold_shares = min(shares, current_shares)
        stake = float(position.get("stake", 0.0))
        entry_price = float(position.get("entry_price", 0.0))
        proceeds = round(sold_shares * exit_price, 2)
        cost_basis = round(stake * (sold_shares / current_shares), 2) if current_shares else 0.0
        realized_pnl = round(proceeds - cost_basis, 2)
        exit_record = {
            "closed_at": utc_now().isoformat(),
            "shares": sold_shares,
            "exit_price": exit_price,
            "proceeds": proceeds,
            "cost_basis": cost_basis,
            "realized_pnl": realized_pnl,
            "order_id": order_id,
            "order_response": order_response,
        }
        if reason:
            exit_record["reason"] = reason
        position.setdefault("exits", []).append(exit_record)
        position["realized_pnl"] = round(float(position.get("realized_pnl", 0.0)) + realized_pnl, 2)
        self.cash = round(self.cash + proceeds, 2)
        position["shares"] = round(current_shares - sold_shares, 6)
        position["stake"] = round(max(0.0, stake - cost_basis), 2)
        position["current_price"] = exit_price
        position["unrealized_pnl"] = round(float(position["shares"]) * exit_price - float(position["stake"]), 2)
        if position["shares"] <= 0.000001 or float(position["stake"]) <= 0.01:
            position["status"] = "closed"
            position["closed_at"] = exit_record["closed_at"]
        elif entry_price > 0:
            position["peak_pnl_pct"] = max(float(position.get("peak_pnl_pct", 0.0)), (exit_price - entry_price) / entry_price)
        return exit_record

    def _build_position(
        self,
        candidate: Candidate,
        stake: float,
        *,
        entry_price: float | None = None,
    ) -> dict[str, Any]:
        trade_price = entry_price if entry_price is not None else candidate.price
        shares = stake / trade_price
        return {
            "status": "open",
            "opened_at": utc_now().isoformat(),
            "market_id": candidate.market_id,
            "question": candidate.question,
            "slug": candidate.slug,
            "url": candidate.url,
            "outcome": candidate.outcome,
            "token_id": candidate.token_id,
            "entry_price": trade_price,
            "current_price": trade_price,
            "stake": round(stake, 2),
            "shares": shares,
            "initial_shares": shares,
            "unrealized_pnl": 0.0,
        }

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
            entry_price = float(position.get("entry_price", 0.0))
            if entry_price > 0:
                pnl_pct = (candidate.price - entry_price) / entry_price
                position["peak_pnl_pct"] = max(float(position.get("peak_pnl_pct", pnl_pct)), pnl_pct)

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
        stake = round(portfolio.cash * settings.trade_fraction, 2)
        opened = portfolio.open_paper_position(top, stake)
    portfolio.save(settings.state_path)
    return portfolio, opened
