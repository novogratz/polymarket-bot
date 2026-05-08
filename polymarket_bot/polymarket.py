"""Authenticated Polymarket CLOB HTTP client.

Implements the two-tier auth path Polymarket uses (L1 EIP-712 signed wallet
auth and L2 HMAC API-key auth), order signing for the negative-risk and
binary CTF exchanges, and the order placement endpoints used by the live
trading layer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True)
class ApiCreds:
    key: str
    secret: str
    passphrase: str

    def to_dict(self) -> dict[str, str]:
        return {"key": self.key, "secret": self.secret, "passphrase": self.passphrase}


class PolymarketClient:
    def __init__(
        self,
        host: str,
        chain_id: int,
        private_key: str | None = None,
        *,
        signature_type: int = 0,
        funder: str | None = None,
        api_creds: ApiCreds | None = None,
        timeout: int = 20,
    ) -> None:
        self.host = host.rstrip("/")
        self.chain_id = chain_id
        self.private_key = private_key
        self.signature_type = signature_type
        self.funder = funder
        self.api_creds = api_creds
        self.timeout = timeout
        self.account = Account.from_key(private_key) if private_key else None

    @property
    def wallet_address(self) -> str:
        if self.account is None:
            raise ValueError("private key is required")
        return self.account.address

    def get_markets(self, *, limit: int, end_date_min=None, end_date_max=None) -> list[dict[str, Any]]:
        params: dict[str, str] = {
            "active": "true",
            "closed": "false",
            "limit": str(limit),
            "order": "end_date",
            "ascending": "true",
        }
        if end_date_min is not None:
            params["end_date_min"] = end_date_min.isoformat()
        if end_date_max is not None:
            params["end_date_max"] = end_date_max.isoformat()
        return self._get_json("/markets", params=params)

    def get_price(self, token_id: str, side: str) -> dict[str, Any]:
        return self._get_json("/price", params={"token_id": token_id, "side": side})

    def get_order_book(self, token_id: str) -> dict[str, Any]:
        return self._get_json("/book", params={"token_id": token_id})

    def get_trades(self) -> Any:
        return self._request("GET", "/trades", auth=True)

    def get_orders(self) -> Any:
        return self._request("GET", "/orders", auth=True)

    def derive_or_create_api_creds(self) -> ApiCreds:
        try:
            payload = self._request("GET", "/auth/derive-api-key", auth="l1")
        except Exception:
            payload = self._request("POST", "/auth/api-key", auth="l1")
        creds = ApiCreds(
            key=str(payload["apiKey"] if "apiKey" in payload else payload["key"]),
            secret=str(payload["secret"]),
            passphrase=str(payload["passphrase"]),
        )
        self.api_creds = creds
        return creds

    def post_order(self, order_body: dict[str, Any], order_type: str) -> Any:
        if self.api_creds is None:
            raise ValueError("API credentials are required before posting orders")
        payload = {
            "order": order_body,
            "owner": self.api_creds.key,
            "orderType": order_type,
        }
        return self._request("POST", "/order", auth=True, body=payload)

    def build_limit_order(
        self,
        *,
        token_id: str,
        price: float,
        size: float,
        side: str,
        maker: str,
        signer: str,
        signature_type: int | None = None,
        taker: str | None = None,
        expiration: int = 0,
        nonce: int = 0,
        fee_rate_bps: int = 0,
        builder: str = "0x0000000000000000000000000000000000000000000000000000000000000000",
        metadata: str = "0x0000000000000000000000000000000000000000000000000000000000000000",
        salt: int | None = None,
        neg_risk: bool = False,
    ) -> dict[str, Any]:
        sig_type = self.signature_type if signature_type is None else signature_type
        buy = side.upper() == "BUY"
        quote_amount = round(price * size, 6)
        maker_amount = int(round((quote_amount if buy else size) * 1_000_000))
        taker_amount = int(round((size if buy else quote_amount) * 1_000_000))
        order = {
            "salt": str(salt if salt is not None else secrets.randbits(128)),
            "maker": maker,
            "signer": signer,
            "taker": taker or "0x0000000000000000000000000000000000000000",
            "tokenId": str(token_id),
            "makerAmount": str(maker_amount),
            "takerAmount": str(taker_amount),
            "expiration": str(expiration),
            "nonce": str(nonce),
            "feeRateBps": str(fee_rate_bps),
            "side": side.upper(),
            "signatureType": sig_type,
            "timestamp": str(int(time.time() * 1000)),
            "metadata": metadata,
            "builder": builder,
        }
        signed = self._sign_order(order, neg_risk=neg_risk)
        order["signature"] = signed
        return order

    def _sign_order(self, order: dict[str, Any], *, neg_risk: bool) -> str:
        if self.account is None:
            raise ValueError("private key is required")
        domain = {
            "name": "Polymarket CTF Exchange",
            "version": "2",
            "chainId": self.chain_id,
            "verifyingContract": (
                "0xe2222d279d744050d28e00520010520000310F59"
                if neg_risk
                else "0xE111180000d2663C0091e4f400237545B87B996B"
            ),
        }
        message = {
            "salt": int(order["salt"]),
            "maker": order["maker"],
            "signer": order["signer"],
            "tokenId": int(order["tokenId"]),
            "makerAmount": int(order["makerAmount"]),
            "takerAmount": int(order["takerAmount"]),
            "side": 0 if order["side"] == "BUY" else 1,
            "signatureType": int(order["signatureType"]),
            "timestamp": int(order["timestamp"]),
            "metadata": order["metadata"],
            "builder": order["builder"],
        }
        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Order": [
                    {"name": "salt", "type": "uint256"},
                    {"name": "maker", "type": "address"},
                    {"name": "signer", "type": "address"},
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "makerAmount", "type": "uint256"},
                    {"name": "takerAmount", "type": "uint256"},
                    {"name": "side", "type": "uint8"},
                    {"name": "signatureType", "type": "uint8"},
                    {"name": "timestamp", "type": "uint256"},
                    {"name": "metadata", "type": "bytes32"},
                    {"name": "builder", "type": "bytes32"},
                ],
            },
            "primaryType": "Order",
            "domain": domain,
            "message": message,
        }
        signed = Account.sign_message(encode_typed_data(full_message=typed_data), self.account.key)
        return signed.signature.hex()

    def _l1_headers(self, nonce: int = 0) -> dict[str, str]:
        if self.account is None:
            raise ValueError("private key is required")
        ts = str(int(time.time()))
        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                ],
                "ClobAuth": [
                    {"name": "address", "type": "address"},
                    {"name": "timestamp", "type": "string"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "message", "type": "string"},
                ],
            },
            "primaryType": "ClobAuth",
            "domain": {"name": "ClobAuthDomain", "version": "1", "chainId": self.chain_id},
            "message": {
                "address": self.wallet_address,
                "timestamp": ts,
                "nonce": nonce,
                "message": "This message attests that I control the given wallet",
            },
        }
        signature = Account.sign_message(encode_typed_data(full_message=typed_data), self.account.key).signature.hex()
        return {
            "POLY_ADDRESS": self.wallet_address,
            "POLY_SIGNATURE": signature,
            "POLY_TIMESTAMP": ts,
            "POLY_NONCE": str(nonce),
        }

    def _l2_headers(self, method: str, request_path: str, body: str | None = None) -> dict[str, str]:
        if self.api_creds is None:
            raise ValueError("API credentials are required")
        timestamp = str(int(time.time()))
        message = timestamp + method.upper() + request_path
        if body:
            message += body
        secret = base64.urlsafe_b64decode(self.api_creds.secret)
        signature = base64.urlsafe_b64encode(
            hmac.new(secret, message.encode("utf-8"), hashlib.sha256).digest()
        ).decode("utf-8")
        return {
            "POLY_ADDRESS": self.wallet_address,
            "POLY_SIGNATURE": signature,
            "POLY_TIMESTAMP": timestamp,
            "POLY_API_KEY": self.api_creds.key,
            "POLY_PASSPHRASE": self.api_creds.passphrase,
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        auth: bool | str = False,
    ) -> Any:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        url = f"{self.host}{path}{query}"
        serialized = None
        request_body = None
        headers = {"Accept": "application/json", "User-Agent": "polymarket-bot/0.1"}

        if body is not None:
            serialized = _json_dumps(body)
            request_body = serialized.encode("utf-8")
            headers["Content-Type"] = "application/json"

        if auth == "l1":
            headers.update(self._l1_headers())
        elif auth:
            headers.update(self._l2_headers(method, path, serialized))

        request = urllib.request.Request(url, data=request_body, method=method.upper(), headers=headers)
        if method.upper() in {"GET", "DELETE"} and request_body is not None:
            request.method = method.upper()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Polymarket {method.upper()} {path} failed with HTTP {exc.code}: {body or exc.reason}"
            ) from exc
        return json.loads(payload) if payload else None

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params, auth=False)
