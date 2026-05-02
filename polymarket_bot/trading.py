from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Settings
from .models import Candidate, utc_now
from .portfolio import Portfolio
from .polymarket import ApiCreds, PolymarketClient


@dataclass(frozen=True)
class LiveTradeResult:
    order: dict[str, Any]
    response: Any
    candidate: Candidate


def build_client(settings: Settings) -> PolymarketClient:
    if not settings.private_key:
        raise ValueError("POLYMARKET_PRIVATE_KEY is required for live trading")
    creds = None
    if settings.api_key and settings.api_secret and settings.api_passphrase:
        creds = ApiCreds(settings.api_key, settings.api_secret, settings.api_passphrase)
    return PolymarketClient(
        settings.clob_base_url,
        settings.chain_id,
        settings.private_key,
        signature_type=settings.signature_type,
        funder=settings.funder_address,
        api_creds=creds,
    )


def choose_trade(candidates: list[Candidate], portfolio: Portfolio) -> Candidate | None:
    for candidate in candidates:
        if (
            candidate.token_id
            and candidate.accepts_orders
            and candidate.best_ask is not None
            and candidate.tick_size is not None
            and not portfolio.has_open_position(candidate.market_id, candidate.outcome)
        ):
            return candidate
    return None


def execute_live_trade(
    client: PolymarketClient,
    settings: Settings,
    candidate: Candidate,
    portfolio: Portfolio,
) -> LiveTradeResult:
    if candidate.best_ask is None or candidate.best_ask <= 0:
        raise ValueError("candidate has no executable ask price")
    if candidate.tick_size is None or candidate.tick_size <= 0:
        raise ValueError("candidate has no tick size")

    entry_price = round(min(candidate.best_ask + candidate.tick_size, 0.99), 3)
    stake = min(portfolio.cash, settings.max_position_usd)
    if stake <= 0:
        raise ValueError("no cash available")
    size = round(stake / entry_price, 6)
    maker = settings.funder_address or client.wallet_address
    signer = client.wallet_address
    order = client.build_limit_order(
        token_id=candidate.token_id or "",
        price=entry_price,
        size=size,
        side="BUY",
        maker=maker,
        signer=signer,
        signature_type=settings.signature_type,
        neg_risk=candidate.neg_risk,
    )
    response = client.post_order(order, "GTC")
    portfolio.open_paper_position(candidate, stake, entry_price=entry_price)
    portfolio.positions[-1]["order_id"] = response.get("orderID") if isinstance(response, dict) else None
    portfolio.positions[-1]["order_response"] = response
    portfolio.positions[-1]["live"] = True
    portfolio.positions[-1]["opened_at"] = utc_now().isoformat()
    return LiveTradeResult(order=order, response=response, candidate=candidate)
