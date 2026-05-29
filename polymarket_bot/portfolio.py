"""Local persistent ledger for cash, open positions, and pending orders.

The :class:`Portfolio` dataclass is loaded from and saved to
``data/paper_state.json`` on every tick. It also owns the share-level
accounting for live entries and exits, including partial fills, cost-basis
tracking, peak-PnL tracking, and per-position event-key dedupe for sports.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import notifications
from ._atomic_io import atomic_write_text
from .config import Settings
from .models import Candidate, parse_dt, utc_now


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
        # Prune closed positions older than 2 hours — they are already in the
        # trade journal and aren't needed in the state file. Keeping 2 hours of
        # recently-closed positions lets _has_recent_closed_token() block
        # re-entry on stale Gamma data between ticks.
        cutoff = (utc_now() - dt.timedelta(hours=2)).isoformat()
        positions_to_save = [
            p for p in self.positions
            if p.get("status") != "closed" or str(p.get("closed_at", "")) >= cutoff
        ]
        atomic_write_text(
            path,
            json.dumps(
                {"cash": self.cash, "positions": positions_to_save, "pending_orders": self.pending_orders or []},
                indent=2,
                sort_keys=True,
            ),
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

    def _has_recent_closed_token(self, token_id: str | None, *, within_minutes: int = 60) -> bool:
        """True if this token was closed within the last N minutes.

        Prevents re-entry on stale Gamma prices: after a resolved exit the
        Gamma cache can keep showing ask≈0.95 for several ticks while the live
        CLOB is already at 0.97+. Without this guard the dry-run bot re-enters
        the same market every tick, generating hundreds of fake journal wins.
        """
        if not token_id:
            return False
        cutoff = (utc_now() - dt.timedelta(minutes=within_minutes)).isoformat()
        return any(
            str(p.get("token_id", "")) == token_id
            and p.get("status") == "closed"
            and str(p.get("closed_at", "")) >= cutoff
            for p in self.positions
        )

    def has_open_event_position(self, candidate: Candidate) -> bool:
        event_key = _event_key(candidate)
        if not event_key:
            return False
        return any(
            position.get("status") == "open" and _event_key(position) == event_key
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
        if (
            stake <= 0.0
            or stake > self.cash
            or self.has_open_position(candidate.market_id, candidate.outcome)
            or self.has_open_event_position(candidate)
            or self._has_recent_closed_token(candidate.token_id)
        ):
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
            "event_slug": candidate.event_slug,
            "url": candidate.url,
            "outcome": candidate.outcome,
            "token_id": candidate.token_id,
            "tick_size": candidate.tick_size,
            "neg_risk": candidate.neg_risk,
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
        if stake <= 0.0 or self.has_open_position(candidate.market_id, candidate.outcome) or self.has_open_event_position(candidate):
            return None
        if self._has_recent_closed_token(candidate.token_id):
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
        if shares <= 0.0 or exit_price < 0.0 or current_shares <= 0.0:
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
        # BIG WIN / BIG LOSS stdout banner — uses the same thresholds as the
        # Telegram alerts so it's tunable from one place.
        import os as _os
        try:
            big_win = float(_os.environ.get("TELEGRAM_BIG_WIN_USD", "10.0"))
            big_loss = float(_os.environ.get("TELEGRAM_BIG_LOSS_USD", "5.0"))
        except ValueError:
            big_win, big_loss = 10.0, 5.0
        title = (
            position.get("question")
            or position.get("title")
            or position.get("market_title")
            or "?"
        )
        side = position.get("outcome") or "?"
        strategy = position.get("strategy") or "?"
        force_big_win = "big_win" in str(reason or "").lower()
        if realized_pnl >= big_win or (force_big_win and realized_pnl >= 0):
            print(
                f"🟢 BIG WIN [{strategy}] +${realized_pnl:.2f} on '{str(title)[:55]}' ({side})",
                flush=True,
            )
        elif realized_pnl <= -big_loss:
            print(
                f"🔴 BIG LOSS [{strategy}] -${abs(realized_pnl):.2f} on '{str(title)[:55]}' ({side})",
                flush=True,
            )
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
        try:
            held_seconds: int | None = None
            opened_at = position.get("opened_at")
            if opened_at:
                try:
                    opened_dt = dt.datetime.fromisoformat(
                        str(opened_at).replace("Z", "+00:00")
                    )
                    closed_at_str = exit_record.get("closed_at")
                    if closed_at_str:
                        closed_dt = dt.datetime.fromisoformat(
                            str(closed_at_str).replace("Z", "+00:00")
                        )
                        held_seconds = int((closed_dt - opened_dt).total_seconds())
                except (ValueError, TypeError):
                    held_seconds = None
            pnl_pct: float | None = None
            if cost_basis > 0:
                pnl_pct = realized_pnl / cost_basis * 100.0
            title = ""
            for key in ("title", "market_title", "question", "name"):
                val = position.get(key)
                if val:
                    title = str(val)
                    break
            notifications.notify_trade_sell(
                market_title=title,
                token_id=str(position.get("token_id", "") or ""),
                price=float(exit_price),
                size_usd=float(proceeds),
                realized_pnl_usd=float(realized_pnl),
                realized_pnl_pct=pnl_pct,
                reason=str(reason or ""),
                outcome=str(position.get("outcome", "") or ""),
                held_seconds=held_seconds,
                market_url=str(position.get("url") or "") or None,
                strategy=str(position.get("strategy") or "") or None,
            )
        except Exception as exc:
            print(f"[notif] trade_sell hook failed: {exc}", file=sys.stderr, flush=True)
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
            "event_slug": candidate.event_slug,
            "url": candidate.url,
            "outcome": candidate.outcome,
            "token_id": candidate.token_id,
            "tick_size": candidate.tick_size,
            "neg_risk": candidate.neg_risk,
            "entry_price": trade_price,
            "current_price": trade_price,
            "stake": round(stake, 2),
            "shares": shares,
            "initial_shares": shares,
            "unrealized_pnl": 0.0,
            "end_date": candidate.end_date.isoformat() if candidate.end_date else None,
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
            # Mark to the bid (what we'd actually sell at). outcomePrices is the
            # last-trade print and can be stale for minutes, producing fake huge
            # PnL spikes that wrongly arm peak/trailing exits.
            mark_price = (
                candidate.best_bid
                if candidate.best_bid is not None and candidate.best_bid > 0
                else candidate.price
            )
            current_value = float(position["shares"]) * mark_price
            position["current_price"] = mark_price
            position["unrealized_pnl"] = round(current_value - float(position["stake"]), 2)
            entry_price = float(position.get("entry_price", 0.0))
            if entry_price > 0:
                pnl_pct = (mark_price - entry_price) / entry_price
                position["peak_pnl_pct"] = max(float(position.get("peak_pnl_pct", pnl_pct)), pnl_pct)
            if candidate.end_date:
                current_end = parse_dt(str(position.get("end_date") or ""))
                if current_end is None or candidate.end_date > current_end:
                    position["end_date"] = candidate.end_date.isoformat()

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


def _event_key(item: Candidate | dict[str, Any]) -> str | None:
    """Stable event identifier shared across YES and NO outcomes of one market.

    Used by ``has_open_event_position`` to block opening both sides of the
    same binary market - and any other position on the same event - even
    when ``market_id`` happens to differ between the Gamma scan and a live
    sync from the Data API.
    """
    if isinstance(item, Candidate):
        event_slug = item.event_slug
        url = item.url
    else:
        event_slug = str(item.get("event_slug") or "")
        url = str(item.get("url") or "")
    key = event_slug.strip().lower() or _event_slug_from_url(url)
    if not key:
        return None
    return _normalize_key(key)


def _sports_event_key(item: Candidate | dict[str, Any]) -> str | None:
    """Sports-specific event key (kept for backward compatibility)."""
    if isinstance(item, Candidate):
        question = item.question
        slug = item.slug
        event_slug = item.event_slug
        url = item.url
    else:
        question = str(item.get("question") or "")
        slug = str(item.get("slug") or "")
        event_slug = str(item.get("event_slug") or "")
        url = str(item.get("url") or "")

    text = f"{question} {slug} {event_slug} {url}".lower()
    if not _looks_sports_like(text):
        return None
    key = event_slug.strip().lower() or _event_slug_from_url(url)
    if not key:
        return None
    return _normalize_key(key)


def _looks_sports_like(text: str) -> bool:
    markers = (
        " fc ",
        " cf ",
        " sc ",
        " vfl ",
        " vs.",
        " vs ",
        "-vs-",
        "nba",
        "nfl",
        "nhl",
        "mlb",
        "epl",
        "laliga",
        "serie",
        "champions league",
        "playoffs",
        "o/u",
    )
    padded = f" {text} "
    return any(marker in padded for marker in markers)


def _event_slug_from_url(url: str) -> str:
    match = re.search(r"/event/([^/?#]+)", url)
    return match.group(1) if match else ""


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
