from __future__ import annotations

import inspect
import json
from importlib import import_module
from dataclasses import dataclass
from typing import Any

from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

from .config import Settings
from .models import Candidate
from .portfolio import Portfolio
from .polymarket import ApiCreds, PolymarketClient


@dataclass(frozen=True)
class LiveTradeResult:
    order: dict[str, Any]
    response: Any
    candidate: Candidate


def _normalize_api_creds(creds: Any) -> ApiCreds:
    for key_name, secret_name, passphrase_name in (
        ("key", "secret", "passphrase"),
        ("api_key", "api_secret", "api_passphrase"),
    ):
        key = getattr(creds, key_name, None)
        secret = getattr(creds, secret_name, None)
        passphrase = getattr(creds, passphrase_name, None)
        if key and secret and passphrase:
            return ApiCreds(str(key), str(secret), str(passphrase))
    if isinstance(creds, dict):
        key = creds.get("key") or creds.get("apiKey") or creds.get("api_key")
        secret = creds.get("secret") or creds.get("apiSecret") or creds.get("api_secret")
        passphrase = creds.get("passphrase") or creds.get("apiPassphrase") or creds.get("api_passphrase")
        if key and secret and passphrase:
            return ApiCreds(str(key), str(secret), str(passphrase))
    raise ValueError("unexpected API credentials payload from Polymarket client")


def _sdk_api_creds(source: Any, module: Any | None = None) -> Any | None:
    if module is None:
        module, _ = _load_sdk_client()
    sdk_api_creds = getattr(module, "ApiCreds", None) if module is not None else None
    if sdk_api_creds is None:
        return None

    if isinstance(source, Settings):
        if not (source.api_key and source.api_secret and source.api_passphrase):
            return None
        return sdk_api_creds(
            api_key=source.api_key,
            api_secret=source.api_secret,
            api_passphrase=source.api_passphrase,
        )

    if isinstance(source, dict):
        api_key = source.get("api_key") or source.get("key") or source.get("apiKey")
        api_secret = source.get("api_secret") or source.get("secret") or source.get("apiSecret")
        api_passphrase = source.get("api_passphrase") or source.get("passphrase") or source.get("apiPassphrase")
    else:
        api_key = getattr(source, "api_key", None) or getattr(source, "key", None)
        api_secret = getattr(source, "api_secret", None) or getattr(source, "secret", None)
        api_passphrase = getattr(source, "api_passphrase", None) or getattr(source, "passphrase", None)

    if not (api_key and api_secret and api_passphrase):
        return None

    return sdk_api_creds(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
    )


def _load_sdk_client():
    for module_name in ("py_clob_client_v2", "py_clob_client"):
        try:
            module = import_module(module_name)
        except ModuleNotFoundError:
            continue
        clob_client = getattr(module, "ClobClient", None)
        if clob_client is not None:
            return module, clob_client
    return None, None


def _build_sdk_client(settings: Settings, *, api_creds: ApiCreds | None = None) -> Any | None:
    module, clob_client = _load_sdk_client()
    if clob_client is None:
        return None

    kwargs: dict[str, Any] = {
        "host": settings.clob_base_url,
        "chain_id": settings.chain_id,
        "key": settings.private_key,
    }
    if api_creds is not None:
        kwargs["creds"] = _sdk_api_creds(api_creds, module)

    signature_type = settings.signature_type
    funder_address = settings.funder_address
    init_params = inspect.signature(clob_client.__init__).parameters
    if "signature_type" in init_params:
        kwargs["signature_type"] = signature_type
    if "funder" in init_params:
        kwargs["funder"] = funder_address
    elif "funder_address" in init_params:
        kwargs["funder_address"] = funder_address

    return clob_client(**kwargs)


@dataclass
class TradingSession:
    settings: Settings
    legacy_client: PolymarketClient
    sdk_client: Any | None
    api_creds: ApiCreds | None = None

    @property
    def wallet_address(self) -> str:
        # Use the proxy (funder) address if available, otherwise EOA
        return self.settings.funder_address or self.legacy_client.wallet_address

    def live_available_balance(self) -> float:
        if self.sdk_client is None:
            return 0.0
        
        target_wallet = self.wallet_address
        print(f"🔍 Checking balance for wallet: {target_wallet}")
        
        try:
            # The SDK client is already initialized with the funder/proxy address
            balance_info = self.sdk_client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
        except Exception as e:
            print(f"❌ Balance check failed: {str(e)}")
            return 0.0

        if not isinstance(balance_info, dict):
            print(f"❌ Balance check returned unexpected type: {type(balance_info)}")
            return 0.0

        balance = self._normalize_amount(balance_info.get("balance"))
        allowance = self._normalize_amount(balance_info.get("allowance"))
        if allowance is None and isinstance(balance_info.get("allowances"), dict):
            allowance_values = [
                normalized
                for value in balance_info["allowances"].values()
                if (normalized := self._normalize_amount(value)) is not None
            ]
            allowance = max(allowance_values) if allowance_values else None
        
        print(f"💰 Live Balance: {balance} USDC | Allowance: {allowance} USDC")
        
        values = [value for value in (balance, allowance) if value is not None]
        return min(values) if values else 0.0

    def derive_or_create_api_creds(self) -> ApiCreds:
        if self.sdk_client is not None:
            method = getattr(self.sdk_client, "create_or_derive_api_key", None)
            if method is None:
                method = getattr(self.sdk_client, "create_or_derive_api_creds", None)
            if method is None:
                raise ValueError("installed Polymarket client does not expose API credential bootstrap")
            creds = _normalize_api_creds(method())
            self.api_creds = creds
            setter = getattr(self.sdk_client, "set_api_creds", None)
            if callable(setter):
                setter(_sdk_api_creds(creds, _load_sdk_client()[0]))
            return creds

        creds = self.legacy_client.derive_or_create_api_creds()
        self.api_creds = creds
        return creds

    def place_live_order(self, *, candidate: Candidate, price: float, size: float) -> tuple[dict[str, Any], Any]:
        if self.sdk_client is not None:
            module, _ = _load_sdk_client()
            if module is not None:
                order_args_cls = getattr(module, "OrderArgs", None)
                order_type_cls = getattr(module, "OrderType", None)
                partial_options_cls = getattr(module, "PartialCreateOrderOptions", None)
                side_cls = getattr(module, "Side", None)
                if order_args_cls and order_type_cls and partial_options_cls and side_cls:
                    order_args = order_args_cls(
                        token_id=candidate.token_id or "",
                        price=price,
                        size=size,
                        side=getattr(side_cls, "BUY", "BUY"),
                    )
                    options = partial_options_cls(
                        tick_size=str(candidate.tick_size or "0.01"),
                        neg_risk=candidate.neg_risk,
                    )
                    order_type = getattr(order_type_cls, "GTC", "GTC")
                    method = getattr(self.sdk_client, "create_and_post_order", None)
                    if callable(method):
                        try:
                            response = method(order_args=order_args, options=options, order_type=order_type)
                        except TypeError:
                            response = method(order_args, options, order_type)
                        order_dict = {
                            "tokenId": candidate.token_id,
                            "price": price,
                            "size": size,
                            "side": "BUY",
                            "signatureType": self.settings.signature_type,
                        }
                        return order_dict, response

        order = self.legacy_client.build_limit_order(
            token_id=candidate.token_id or "",
            price=price,
            size=size,
            side="BUY",
            maker=self.settings.funder_address or self.wallet_address,
            signer=self.wallet_address,
            signature_type=self.settings.signature_type,
            neg_risk=candidate.neg_risk,
        )
        response = self.legacy_client.post_order(order, "GTC")
        return order, response

    @staticmethod
    def _normalize_amount(value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            text = str(value)
            amount = float(text)
        except (TypeError, ValueError):
            return None
        if "." in text:
            return amount
        return amount / 1_000_000.0


def build_client(settings: Settings) -> TradingSession:
    if not settings.private_key:
        raise ValueError("POLYMARKET_PRIVATE_KEY is required for live trading")
    api_creds = (
        ApiCreds(settings.api_key, settings.api_secret, settings.api_passphrase)
        if settings.api_key and settings.api_secret and settings.api_passphrase
        else None
    )
    legacy_client = PolymarketClient(
        settings.clob_base_url,
        settings.chain_id,
        settings.private_key,
        signature_type=settings.signature_type,
        funder=settings.funder_address,
        api_creds=api_creds,
    )
    sdk_client = _build_sdk_client(
        settings,
        api_creds=api_creds,
    )
    return TradingSession(settings=settings, legacy_client=legacy_client, sdk_client=sdk_client, api_creds=api_creds)


def choose_trade(candidates: list[Candidate], portfolio: Portfolio) -> Candidate | None:
    for candidate in candidates:
        if (
            candidate.token_id
            and candidate.accepts_orders
            and candidate.best_ask is not None
            and candidate.tick_size is not None
            and not portfolio.has_open_position(candidate.market_id)
        ):
            return candidate
    return None


def execute_live_trade(
    client: TradingSession,
    settings: Settings,
    candidate: Candidate,
    portfolio: Portfolio,
    *,
    min_trade_usd: float | None = None,
    max_trade_usd: float | None = None,
    strategy: str | None = None,
    signal: dict[str, Any] | None = None,
) -> LiveTradeResult:
    if candidate.best_ask is None or candidate.best_ask <= 0:
        raise ValueError("candidate has no executable ask price")
    if candidate.tick_size is None or candidate.tick_size <= 0:
        raise ValueError("candidate has no tick size")

    entry_price = round(min(candidate.best_ask + candidate.tick_size, 0.99), 3)

    # ANTI-PUMP PROTECTION: Don't follow if price moved > 15% from smart money entry
    if signal and "avg_copy_price" in signal:
        avg_copy = float(signal["avg_copy_price"])
        slippage = (entry_price - avg_copy) / avg_copy if avg_copy > 0 else 0
        if slippage > 0.15:
            raise ValueError(f"Anti-pump: Entry price {entry_price} is {slippage:.1%} above smart money avg {avg_copy}")

    live_balance = client.live_available_balance()
    if live_balance <= 0:
        raise ValueError("no live balance available")

    # TARGET EXPOSURE SIZING: Aim for total exposure = 50% of equity
    summary = portfolio.summary()
    total_equity = live_balance + summary.get("total_stake", 0.0)
    target_total_exposure = total_equity * settings.trade_fraction
    current_exposure = summary.get("total_stake", 0.0)
    
    # How much more do we need to reach the target?
    needed_usd = max(0.0, target_total_exposure - current_exposure)
    
    # Sizing logic
    minimum = min_trade_usd if min_trade_usd is not None else settings.btc_min_trade_usd
    maximum = max_trade_usd if max_trade_usd is not None else settings.btc_max_trade_usd
    
    # Use the needed amount, but capped by available balance and max per trade
    stake = min(needed_usd, live_balance, maximum)
    
    # If we are below minimum but have room to grow to target, take minimum
    if stake < minimum and needed_usd >= minimum and live_balance >= minimum:
        stake = minimum

    min_share_stake = settings.min_order_shares * entry_price
    if live_balance >= min_share_stake:
        stake = max(stake, min_share_stake)
    
    stake = round(min(stake, live_balance), 2)
    if stake <= 0:
        raise ValueError("target exposure already reached or no cash available")
    if stake < minimum:
        raise ValueError(f"trade size {stake} is below Polymarket's $1 minimum")

    size = round(stake / entry_price, 6)
    if size < settings.min_order_shares:
        raise ValueError(
            f"order size {size} shares is below Polymarket minimum of {settings.min_order_shares} shares"
        )

    print(f"\n🚀 EXECUTING TRADE: BUY {size} shares of '{candidate.outcome}' at {entry_price} on '{candidate.question}' (${stake} USDC)")
    order, response = client.place_live_order(candidate=candidate, price=entry_price, size=size)
    print(f"📡 API RESPONSE: {json.dumps(response, indent=2)}\n")

    position = portfolio.record_live_position(
        candidate,
        stake,
        entry_price=entry_price,
        order_id=response.get("orderID") if isinstance(response, dict) else None,
        order_response=response,
    )
    if position is not None:
        if strategy:
            position["strategy"] = strategy
        if signal is not None:
            position["signal"] = signal
    return LiveTradeResult(order=order, response=response, candidate=candidate)
