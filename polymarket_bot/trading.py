"""Live trade execution.

Wraps :class:`PolymarketClient` and the Polymarket CLOB SDK to compute the
final stake per trade, place authenticated FOK market BUY orders, and
execute partial SELLs against an open position. Sizing combines the
percentage-based :class:`Settings` knobs with the conviction multiplier
returned by ``main._signal_quality_multiplier`` and the high-conviction
balance fraction.
"""

from __future__ import annotations

import inspect
import json
import sys
import time
from importlib import import_module
from dataclasses import dataclass
from typing import Any

from . import notifications
from .config import Settings
from .models import Candidate
from .portfolio import Portfolio
from .polymarket import ApiCreds, PolymarketClient


def _load_clob_types():
    """Lazy-load clob types from py-clob-client (v2 preferred, v1 fallback).

    Importing at module load time would crash any environment that hasn't
    installed the SDK (notably CI, where live trading is never invoked).
    """
    last_error: Exception | None = None
    for module_name in ("py_clob_client_v2", "py_clob_client"):
        try:
            module = import_module(f"{module_name}.clob_types")
            return module.AssetType, module.BalanceAllowanceParams
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(
        "py-clob-client SDK is not installed; live balance lookup is unavailable"
    ) from last_error


# Polymarket migrated from USDC.e to pUSD on 2026-04-28. py-clob-client <=0.34.6
# still references the legacy USDC.e contract for balance lookups, so we read
# pUSD on-chain directly via JSON-RPC as a workaround.
PUSD_TOKEN_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
DEFAULT_POLYGON_RPC_URL = "https://polygon-bor-rpc.publicnode.com"


def read_pusd_balance(holder: str, rpc_url: str | None = None, timeout: int = 10) -> float:
    import requests

    rpc = rpc_url or DEFAULT_POLYGON_RPC_URL
    addr_padded = holder.lower().replace("0x", "").rjust(64, "0")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": PUSD_TOKEN_ADDRESS, "data": "0x70a08231" + addr_padded}, "latest"],
    }
    response = requests.post(rpc, json=payload, timeout=timeout).json()
    if "result" not in response:
        raise RuntimeError(f"pUSD RPC error: {response.get('error', response)}")
    return int(response["result"], 16) / 1_000_000


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
        target_wallet = self.wallet_address
        quiet = bool(getattr(self.settings, "quiet", False))
        if not quiet:
            print(f"🔍 Checking pUSD balance for wallet: {target_wallet}")

        rpc_url = getattr(self.settings, "polygon_rpc_url", None) or DEFAULT_POLYGON_RPC_URL
        try:
            balance = read_pusd_balance(target_wallet, rpc_url=rpc_url)
        except Exception as e:
            # Rate-limit log spam: only print once per 5 min so terminal
            # stays readable when the public RPC is throttling us.
            import time as _t
            last = getattr(self, "_last_rpc_err_ts", 0)
            now = _t.time()
            if now - last > 300:
                print(f"❌ pUSD on-chain balance check failed: {str(e)}")
                self._last_rpc_err_ts = now
            balance = 0.0

        allowance: float | None = None
        if self.sdk_client is not None:
            try:
                asset_type, balance_params = _load_clob_types()
                balance_info = self.sdk_client.get_balance_allowance(
                    balance_params(asset_type=asset_type.COLLATERAL)
                )
                if isinstance(balance_info, dict):
                    allowance = self._normalize_amount(balance_info.get("allowance"))
                    if allowance is None and isinstance(balance_info.get("allowances"), dict):
                        allowance_values = [
                            normalized
                            for value in balance_info["allowances"].values()
                            if (normalized := self._normalize_amount(value)) is not None
                        ]
                        allowance = max(allowance_values) if allowance_values else None
            except Exception as e:
                print(f"⚠️  SDK allowance check skipped: {str(e)}")

        if not quiet:
            print(f"💰 Live Balance: {balance} pUSD | Allowance: {allowance} (legacy USDC.e via SDK)")

        if balance <= 0.0:
            # On-chain RPC failed (rate-limited or down). The static
            # assumed_live_balance_usd is the STARTING value — using it
            # as cash mid-session over-counts (positions already deployed).
            # Prefer the local ledger's current cash, which reflects the
            # post-trade reality. Only fall back to the static assume
            # when the ledger is empty (first tick).
            ledger_cash = self._read_ledger_cash()
            # Same throttle as the RPC error — once per 5 min.
            import time as _t
            last = getattr(self, "_last_fallback_log_ts", 0)
            now = _t.time()
            if ledger_cash is not None and ledger_cash > 0:
                if now - last > 300:
                    print(
                        f"⚠️  pUSD RPC unavailable — using local ledger cash "
                        f"${ledger_cash:.2f} (instead of stale assume "
                        f"${self.settings.assumed_live_balance_usd:.2f})"
                    )
                    self._last_fallback_log_ts = now
                return ledger_cash
            if self.settings.assumed_live_balance_usd > 0.0:
                if now - last > 300:
                    print(
                        f"⚠️  Using POLYMARKET_ASSUME_LIVE_BALANCE_USD="
                        f"{self.settings.assumed_live_balance_usd} (no ledger cash yet)"
                    )
                    self._last_fallback_log_ts = now
                return self.settings.assumed_live_balance_usd
        return balance

    def _read_ledger_cash(self) -> float | None:
        """Estimate the current cash when on-chain RPC fails.

        Returns ``min(ledger_cash, assume - sum(open_positions_at_cost))``
        when ledger is present. The min guards against the LIVE_SYNC
        path that imports positions WITHOUT debiting cash — leaving
        ledger.cash + sum(positions) > assume (i.e. phantom money).

        Returns ``None`` if file unreadable; let caller fall back to
        the static assume (first-tick path).
        """
        try:
            state_path = getattr(self.settings, "state_path", None)
            if not state_path:
                return None
            import json as _j
            from pathlib import Path as _P
            p = _P(state_path) if not isinstance(state_path, _P) else state_path
            if not p.exists():
                return None
            data = _j.loads(p.read_text(encoding="utf-8"))
            cash = data.get("cash")
            if cash is None:
                return None
            ledger_cash = float(cash)
            # Compute the upper bound consistent with assumed_live_balance.
            # Open positions have a cost basis (stake or entry × shares);
            # the bot's real CLOB cash cannot exceed assume - sum(costs)
            # regardless of what the ledger says, because that money is
            # locked in the positions.
            assume = float(getattr(self.settings, "assumed_live_balance_usd", 0.0) or 0.0)
            if assume > 0:
                invested = 0.0
                for pos in data.get("positions", []) or []:
                    if pos.get("status") != "open":
                        continue
                    cost = pos.get("stake")
                    if cost is None:
                        entry = float(pos.get("entry_price") or 0)
                        shares = float(pos.get("shares") or 0)
                        cost = entry * shares
                    invested += float(cost or 0)
                derived_max = max(0.0, assume - invested)
                # Use the LOWER of ledger cash and the derived max —
                # protects against both stale ledger AND under-debited sync.
                return min(ledger_cash, derived_max)
            return ledger_cash
        except Exception:
            return None

    def live_share_balance(self, token_id: str) -> float | None:
        """Query the wallet's available share balance for a specific outcome
        token (CONDITIONAL asset type). Returns the number of shares the
        wallet can actually sell right now (locked-in-resting-orders
        excluded by the CLOB itself). Returns None on failure.
        """
        if not token_id or self.sdk_client is None:
            return None
        try:
            asset_type, balance_params = _load_clob_types()
            info = self.sdk_client.get_balance_allowance(
                balance_params(asset_type=asset_type.CONDITIONAL, token_id=token_id)
            )
        except Exception as exc:
            print(
                f"   share-balance lookup failed for token {token_id[:12]}…: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            return None
        if not isinstance(info, dict):
            return None
        raw = info.get("balance")
        normalized = self._normalize_amount(raw)
        return normalized

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

    def place_live_order(
        self,
        *,
        candidate: Candidate,
        price: float,
        size: float,
        side: str = "BUY",
    ) -> tuple[dict[str, Any], Any]:
        side = side.upper()
        if self.sdk_client is not None:
            module, _ = _load_sdk_client()
            if module is not None:
                order_args_cls = getattr(module, "OrderArgs", None)
                partial_options_cls = getattr(module, "PartialCreateOrderOptions", None)
                if order_args_cls and partial_options_cls:
                    order_args = order_args_cls(
                        token_id=candidate.token_id or "",
                        price=price,
                        size=size,
                        side=side,
                    )
                    options = partial_options_cls(
                        tick_size=str(candidate.tick_size or "0.01"),
                        neg_risk=candidate.neg_risk,
                    )
                    method = getattr(self.sdk_client, "create_and_post_order", None)
                    if callable(method):
                        response = method(order_args=order_args, options=options)
                        order_dict = {
                            "tokenId": candidate.token_id,
                            "price": price,
                            "size": size,
                            "side": side,
                            "signatureType": self.settings.signature_type,
                        }
                        return order_dict, response

        order = self.legacy_client.build_limit_order(
            token_id=candidate.token_id or "",
            price=price,
            size=size,
            side=side,
            maker=self.settings.funder_address or self.wallet_address,
            signer=self.legacy_client.wallet_address,
            signature_type=self.settings.signature_type,
            neg_risk=candidate.neg_risk,
        )
        response = self.legacy_client.post_order(order, "GTC")
        return order, response

    def place_market_order(
        self,
        *,
        candidate: Candidate,
        amount: float,
        side: str = "BUY",
        price: float = 0.0,
    ) -> tuple[dict[str, Any], Any]:
        side = side.upper()
        if self.sdk_client is not None:
            module, _ = _load_sdk_client()
            if module is not None:
                market_order_args_cls = getattr(module, "MarketOrderArgs", None)
                order_type_cls = getattr(module, "OrderType", None)
                partial_options_cls = getattr(module, "PartialCreateOrderOptions", None)
                side_cls = getattr(module, "Side", None)
                if market_order_args_cls and order_type_cls and partial_options_cls and side_cls:
                    order_args = market_order_args_cls(
                        token_id=candidate.token_id or "",
                        amount=amount,
                        side=getattr(side_cls, side, side),
                        price=price,
                        order_type=getattr(order_type_cls, "FOK", "FOK"),
                        user_usdc_balance=amount if side == "BUY" else 0,
                    )
                    options = partial_options_cls(
                        tick_size=str(candidate.tick_size or "0.01"),
                        neg_risk=candidate.neg_risk,
                    )
                    method = getattr(self.sdk_client, "create_and_post_market_order", None)
                    if callable(method):
                        response = method(
                            order_args=order_args,
                            options=options,
                            order_type=getattr(order_type_cls, "FOK", "FOK"),
                        )
                        order_dict = {
                            "tokenId": candidate.token_id,
                            "amount": amount,
                            "price": price,
                            "side": side,
                            "orderType": "FOK",
                            "signatureType": self.settings.signature_type,
                        }
                        return order_dict, response

        return self.place_live_order(candidate=candidate, price=price, size=amount, side=side)

    def cancel_order(self, order_id: str) -> Any:
        if self.sdk_client is not None:
            module, _ = _load_sdk_client()
            order_payload_cls = getattr(module, "OrderPayload", None) if module is not None else None
            method = getattr(self.sdk_client, "cancel_order", None)
            if callable(method) and order_payload_cls is not None:
                return method(order_payload_cls(orderID=order_id))
        raise ValueError("installed Polymarket client does not support order cancellation")

    def cancel_active_orders_for_token(self, token_id: str) -> list[str]:
        """Best-effort enumerate-and-cancel of resting orders on a token.

        Tries SDK v2 get_orders → legacy get_orders. If neither works the
        caller is expected to fall back to force-closing the position locally,
        since enumerate-less bulk cancel methods on this CLOB version all
        return 400/405 with different shapes.
        """
        cancelled: list[str] = []
        if not token_id:
            return cancelled
        orders = None
        if self.sdk_client is not None:
            getter = getattr(self.sdk_client, "get_orders", None)
            if callable(getter):
                try:
                    orders = getter()
                except Exception:
                    orders = None
        if orders is None:
            try:
                orders = self.legacy_client.get_orders()
            except Exception:
                return cancelled
        if not isinstance(orders, list):
            return cancelled
        target = str(token_id)
        for order in orders:
            if not isinstance(order, dict):
                continue
            order_token = str(
                order.get("asset_id")
                or order.get("asset")
                or order.get("token_id")
                or order.get("tokenId")
                or ""
            )
            if order_token != target:
                continue
            order_id = str(
                order.get("id") or order.get("orderID") or order.get("order_id") or ""
            )
            if not order_id:
                continue
            try:
                self.cancel_order(order_id)
                cancelled.append(order_id)
            except Exception as exc:
                print(f"⚠️  cancel_active_orders_for_token: cancel {order_id} failed: {type(exc).__name__}: {exc}", flush=True)
        return cancelled

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


class _DryRunClient:
    """No-op trading client used when ``settings.dry_run`` is True.

    Implements only the read/cancel surface the smart-money loop hits
    on every tick (``live_available_balance``, ``cancel_order``,
    ``cancel_active_orders_for_token``). Real buys/sells are
    short-circuited inside ``execute_live_trade`` / ``execute_live_sell``
    via ``settings.dry_run`` before this object is touched, so we don't
    need to stub ``place_market_order``.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def live_available_balance(self) -> float:
        # No CLOB to ask — return 0 so the smart-money loop falls back
        # on portfolio.cash from the simulated ledger.
        return 0.0

    def live_share_balance(self, token_id: str) -> float | None:
        # Dry-run: no on-chain balance to fetch — caller should fall back
        # on portfolio.shares.
        return None

    def cancel_order(self, order_id: str) -> dict:
        return {"dry_run": True, "cancelled": order_id}

    def cancel_active_orders_for_token(self, token_id: str) -> list[str]:
        return []

    def derive_or_create_api_creds(self):
        raise RuntimeError(
            "derive_or_create_api_creds() is unavailable in dry-run mode "
            "(no private key). Run without --dry-run for credential setup."
        )


def build_client(settings: Settings) -> "TradingSession | _DryRunClient":
    if settings.dry_run:
        return _DryRunClient(settings)
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
            and not portfolio.has_open_event_position(candidate)
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
    if portfolio.has_open_event_position(candidate):
        raise ValueError("duplicate_open_sports_event")

    entry_price = round(min(candidate.best_ask + candidate.tick_size, 0.99), 3)

    # ANTI-PUMP PROTECTION: Don't follow if price moved too far from smart money entry.
    if signal and "avg_copy_price" in signal:
        avg_copy = float(signal["avg_copy_price"])
        slippage = (entry_price - avg_copy) / avg_copy if avg_copy > 0 else 0
        metrics = signal.get("selection_metrics", {}) if isinstance(signal.get("selection_metrics"), dict) else {}
        max_slippage = (
            settings.smart_crypto_micro_max_entry_slippage
            if metrics.get("is_crypto_micro")
            else settings.smart_max_entry_slippage
        )
        if slippage > max_slippage:
            raise ValueError(f"Anti-pump: Entry price {entry_price} is {slippage:.1%} above smart money avg {avg_copy}")

    if settings.dry_run:
        live_balance = portfolio.cash
    else:
        live_balance = client.live_available_balance()
    if live_balance <= 0:
        raise ValueError("no live balance available")

    # Target exposure sizing: deploy until invested capital reaches the configured equity fraction.
    summary = portfolio.summary()
    current_exposure = float(summary.get("invested", 0.0))
    total_equity = live_balance + current_exposure
    target_total_exposure = total_equity * settings.trade_fraction
    needed_usd = max(0.0, target_total_exposure - current_exposure)
    
    # Sizing logic
    minimum = min_trade_usd if min_trade_usd is not None else settings.btc_min_trade_usd
    maximum = max_trade_usd if max_trade_usd is not None else settings.btc_max_trade_usd
    if signal:
        metrics = signal.get("selection_metrics", {}) if isinstance(signal.get("selection_metrics"), dict) else {}
        if metrics.get("is_crypto_micro"):
            maximum = min(maximum, settings.smart_crypto_micro_max_trade_usd)
        consensus = float(metrics.get("profitable_wallet_count") or signal.get("consensus") or 0.0)
        copied_usdc = float(metrics.get("copied_usdc") or signal.get("copied_usdc") or 0.0)
        if settings.smart_position_pct <= 0:
            quality_multiplier = 1.0
            if consensus >= 4 and copied_usdc >= 1000:
                quality_multiplier = 2.0
            elif consensus >= 3 and copied_usdc >= 250:
                quality_multiplier = 1.5
            maximum = min(maximum, settings.max_position_usd * quality_multiplier)
        if _is_high_conviction_signal(signal) and settings.smart_high_conviction_balance_fraction > 0:
            maximum = max(maximum, live_balance * settings.smart_high_conviction_balance_fraction)
            maximum = min(maximum, live_balance)
    
    # Use the needed amount, but capped by available balance and max per trade
    stake = min(needed_usd, live_balance, maximum)
    
    # If we are below minimum but have room to grow to target, take minimum
    if stake < minimum and needed_usd >= minimum and live_balance >= minimum:
        stake = minimum

    min_share_stake = settings.min_order_shares * entry_price
    if stake > 0 and live_balance >= min_share_stake:
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

    if settings.quiet:
        # SELLs still print so operators can see exits without the noise of
        # individual entries. Toggle via POLYMARKET_SUPPRESS_BUY_LOGS=1.
        if not settings.suppress_buy_logs:
            prefix = "[DRY-RUN] " if settings.dry_run else ""
            print(
                f"🚀 {prefix}BUY {candidate.outcome} ${stake} @ {entry_price} | "
                f"{candidate.question[:60]}"
            )
    else:
        print(f"\n🚀 MARKET BUY: {candidate.outcome} on {candidate.question}")
        print(f"   Stake: ${stake} USDC | Max price guard: {entry_price} | Est. shares: {size}")
        print(f"   Market: {candidate.url}")
        if signal:
            metrics = signal.get("selection_metrics", {}) if isinstance(signal.get("selection_metrics"), dict) else {}
            print(f"   Why: {signal.get('selection_reason', 'smart-money signal passed filters')}")
            print(
                "   Signal: "
                f"wallets={metrics.get('profitable_wallet_count', signal.get('consensus'))} "
                f"copied=${metrics.get('copied_usdc', signal.get('copied_usdc'))} "
                f"avg_copy={metrics.get('avg_copy_price', signal.get('avg_copy_price'))} "
                f"ask={metrics.get('current_ask', signal.get('best_ask'))} "
                f"bid={metrics.get('current_bid', signal.get('best_bid'))} "
                f"spread={metrics.get('spread')} "
                f"wallet_pnl=${metrics.get('total_trader_pnl', signal.get('total_trader_pnl'))}"
            )
    if settings.dry_run:
        if not settings.quiet:
            print("   [DRY-RUN] Skipping SDK call, simulating matched fill.")
        order = {"dry_run": True, "side": "BUY", "amount": stake, "price": entry_price}
        response = {
            "success": True,
            "status": "matched",
            "orderID": f"dry-run-buy-{int(time.time() * 1000)}",
            "makingAmount": str(stake),
            "takingAmount": str(size),
            "dry_run": True,
        }
    else:
        if not settings.quiet:
            print("   Sending FOK market order...")
        order, response = client.place_market_order(candidate=candidate, amount=stake, price=entry_price, side="BUY")
    if isinstance(response, dict) and response.get("success") and not settings.quiet:
        status = str(response.get("status") or "")
        label = "✅ BUY FILLED" if _is_filled_buy_response(response) else "⚠️  BUY NOT FILLED"
        print(
            f"{label}: "
            f"status={status} order_id={response.get('orderID')} "
            f"making={response.get('makingAmount')} taking={response.get('takingAmount')}"
        )
    if not settings.quiet:
        print(f"📡 BUY API RESPONSE: {json.dumps(response, indent=2)}\n")

    order_id = response.get("orderID") if isinstance(response, dict) else None
    if _is_filled_buy_response(response):
        position = portfolio.record_live_position(
            candidate,
            stake,
            entry_price=entry_price,
            order_id=order_id,
            order_response=response,
        )
        if position is not None:
            if strategy:
                position["strategy"] = strategy
            if signal is not None:
                position["signal"] = signal
        try:
            title = candidate.question or ""
            signal_payload: dict[str, Any] = {}
            if isinstance(signal, dict):
                metrics = (
                    signal.get("selection_metrics", {})
                    if isinstance(signal.get("selection_metrics"), dict)
                    else {}
                )
                wallets = (
                    metrics.get("profitable_wallet_count")
                    or signal.get("consensus")
                    or 0
                )
                copied = (
                    metrics.get("copied_usdc")
                    or signal.get("copied_usdc")
                    or 0
                )
                signal_payload = {
                    "wallets": int(float(wallets or 0)),
                    "copied_usdc": float(copied or 0),
                    "tag": strategy,
                }
            elif strategy:
                signal_payload = {"tag": strategy}
            market_url = candidate.url or None
            notifications.notify_trade_buy(
                market_title=title,
                token_id=str(candidate.token_id or ""),
                price=float(entry_price),
                size_usd=float(stake),
                signal=signal_payload,
                outcome=str(candidate.outcome or ""),
                market_url=market_url,
                strategy=strategy,
            )
        except Exception as exc:
            print(f"[notif] trade_buy hook failed: {exc}", file=sys.stderr, flush=True)
    return LiveTradeResult(order=order, response=response, candidate=candidate)


def _is_filled_buy_response(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    if str(response.get("status") or "").lower() == "matched":
        return True
    for key in ("takingAmount", "makingAmount"):
        value = response.get(key)
        if value not in (None, ""):
            try:
                if float(value) > 0:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def _is_high_conviction_signal(signal: dict[str, Any]) -> bool:
    metrics = signal.get("selection_metrics", {}) if isinstance(signal.get("selection_metrics"), dict) else {}
    consensus = float(metrics.get("profitable_wallet_count") or signal.get("consensus") or 0.0)
    copied_usdc = float(metrics.get("copied_usdc") or signal.get("copied_usdc") or 0.0)
    total_trader_pnl = float(metrics.get("total_trader_pnl") or signal.get("total_trader_pnl") or 0.0)
    value_score = float(metrics.get("value_score") or 0.0)
    value_discount_pct = float(metrics.get("value_discount_pct") or 0.0)
    if consensus >= 4 and copied_usdc >= 1000:
        return True
    if consensus >= 3 and copied_usdc >= 5000:
        return True
    if consensus >= 2 and copied_usdc >= 1000 and total_trader_pnl >= 250000 and value_discount_pct >= -0.10:
        return True
    return consensus >= 2 and copied_usdc >= 250 and value_score >= 10 and value_discount_pct >= 0


def execute_live_sell(
    client: TradingSession,
    settings: Settings,
    candidate: Candidate,
    portfolio: Portfolio,
    position: dict[str, Any],
    *,
    shares: float,
    reason: str,
) -> LiveTradeResult:
    if candidate.best_bid is None or candidate.best_bid <= 0:
        raise ValueError("candidate has no executable bid price")
    if candidate.tick_size is None or candidate.tick_size <= 0:
        raise ValueError("candidate has no tick size")

    sell_price = round(min(max(candidate.best_bid, candidate.tick_size), 0.99), 3)
    available_shares = float(position.get("shares", 0.0))
    # Clamp to on-chain share balance — local ledger can drift slightly from
    # the wallet due to rounding, partial fills, or resting orders. Asking the
    # CLOB to sell more than the wallet holds returns "balance is not enough"
    # which leaves the position stuck. Query the live share balance and use
    # the minimum.
    if not settings.dry_run:
        try:
            on_chain = client.live_share_balance(str(candidate.token_id or ""))
            if on_chain is not None and on_chain >= 0:
                if on_chain < available_shares:
                    available_shares = on_chain
        except Exception as exc:
            print(
                f"   live share-balance check failed: {type(exc).__name__}: {exc}",
                flush=True,
            )
    size = round(min(shares, available_shares), 6)
    if size <= 0:
        raise ValueError("no shares available to sell")
    min_order_tolerance = max(0.001, settings.min_order_shares * 0.001)
    selling_all_available = abs(size - available_shares) <= min_order_tolerance
    if size < settings.min_order_shares - min_order_tolerance and not selling_all_available:
        raise ValueError(
            f"sell size {size} shares is below Polymarket minimum of {settings.min_order_shares} shares"
        )
    proceeds = round(size * sell_price, 2)
    if proceeds < settings.smart_min_sell_usd and not selling_all_available:
        raise ValueError(f"sell proceeds {proceeds} is below minimum ${settings.smart_min_sell_usd}")

    if settings.quiet:
        prefix = "[DRY-RUN] " if settings.dry_run else ""
        print(
            f"💸 {prefix}SELL {size} '{candidate.outcome}' @ {sell_price} "
            f"(${proceeds}) reason={reason}"
        )
    else:
        print(
            f"\n💸 EXECUTING EXIT: SELL {size} shares of '{candidate.outcome}' at {sell_price} "
            f"on '{candidate.question}' (${proceeds} USDC) reason={reason}"
        )
    if settings.dry_run:
        if not settings.quiet:
            print("   [DRY-RUN] Skipping SDK call, simulating matched SELL fill.")
        order = {"dry_run": True, "side": "SELL", "size": size, "price": sell_price}
        response = {
            "success": True,
            "status": "matched",
            "orderID": f"dry-run-sell-{int(time.time() * 1000)}",
            "makingAmount": str(size),
            "takingAmount": str(proceeds),
            "dry_run": True,
        }
    else:
        order, response = client.place_live_order(candidate=candidate, price=sell_price, size=size, side="SELL")
    if not settings.quiet:
        print(f"📡 SELL RESPONSE: {json.dumps(response, indent=2)}\n")
    portfolio.record_live_exit(
        position,
        shares=size,
        exit_price=sell_price,
        order_id=response.get("orderID") if isinstance(response, dict) else None,
        order_response=response,
        reason=reason,
    )
    return LiveTradeResult(order=order, response=response, candidate=candidate)
