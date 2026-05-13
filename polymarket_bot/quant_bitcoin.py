"""QUANT BITCOIN deterministic BTC strategy.

The strategy only trades active Bitcoin markets when the ask price clears a
fixed threshold and the market is executable with acceptable spread and
liquidity. It does not call LLMs and does not inspect wallet leaderboards.
"""

from __future__ import annotations

import json
import re
import statistics
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .models import Candidate, as_float, parse_dt


@dataclass(frozen=True)
class QuantBtcSnapshot:
    symbol: str
    spot: float
    open_5m: float
    close_5m: float
    move_pct: float
    spot_binance: float = 0.0
    spot_coinbase: float = 0.0
    spot_kraken: float = 0.0
    exchange_divergence: bool = False
    exchange_count: int = 0
    tradingview: str = ""
    cryptoquant: str = ""
    graph_bias: str = ""
    bull_nodes: int = 0
    bear_nodes: int = 0
    edges: int = 0

    @property
    def direction(self) -> str:
        # If spot move is significant, it defines the direction.
        if self.move_pct > 0.0001:
            return "BULL"
        if self.move_pct < -0.0001:
            return "BEAR"
        
        # If spot is flat, we let the external validators (TV/Graph) drive the direction.
        if self.tradingview in {"BULL", "BEAR"}:
            return self.tradingview
        if self.graph_bias in {"BULL", "BEAR"}:
            return self.graph_bias
            
        return "NEUTRAL"


@dataclass(frozen=True)
class QuantBtcSignal:
    candidate: Candidate
    direction: str
    spot: float
    move_pct: float
    expected_probability: float
    clob_lag_pct: float
    edge: float
    spread: float
    validators: dict[str, str]
    price_threshold: float

    @property
    def score(self) -> float:
        return (self.edge * 1000.0) - (self.spread * 10.0)

    def to_dict(self) -> dict[str, Any]:
        side = "UP" if self.direction == "BULL" else "DOWN"
        reason = (
            f"QUANT BITCOIN {side}: CLOB ask {self.candidate.best_ask} > "
            f"${self.price_threshold:.2f} threshold; big-slot sizing and execution checks passed."
        )
        return {
            "market_id": self.candidate.market_id,
            "question": self.candidate.question,
            "outcome": self.candidate.outcome,
            "best_ask": self.candidate.best_ask,
            "best_bid": self.candidate.best_bid,
            "direction": self.direction,
            "spot": self.spot,
            "move_pct": round(self.move_pct, 6),
            "expected_probability": round(self.expected_probability, 6),
            "clob_lag_pct": round(self.clob_lag_pct, 6),
            "edge": round(self.edge, 6),
            "score": round(self.score, 4),
            "validators": dict(self.validators),
            "selection_reason": reason,
            "selection_metrics": {
                "current_ask": self.candidate.best_ask,
                "current_bid": self.candidate.best_bid,
                "spread": round(self.spread, 4),
                "hours_to_close": self.candidate.hours_to_close,
                "spot": self.spot,
                "spot_move_pct": round(self.move_pct, 6),
                "clob_lag_pct": round(self.clob_lag_pct, 6),
                "edge": round(self.edge, 6),
                "expected_probability": round(self.expected_probability, 6),
                "price_threshold": round(self.price_threshold, 6),
                "strategy": "quant_bitcoin",
            },
            "url": self.candidate.url,
        }


@dataclass(frozen=True)
class QuantBtcReport:
    selected: QuantBtcSignal | None
    opportunities: list[QuantBtcSignal]
    snapshot: QuantBtcSnapshot | None
    rejected: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": self.selected.to_dict() if self.selected else None,
            "opportunities": [signal.to_dict() for signal in self.opportunities[:10]],
            "snapshot": self.snapshot.__dict__ if self.snapshot else None,
            "rejected": dict(self.rejected),
        }


def _fetch_json(url: str, timeout: int = 5) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-bot/quant-bitcoin"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_coinbase_spot(timeout: int = 5) -> float | None:
    try:
        data = _fetch_json("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout)
        return as_float(data.get("data", {}).get("amount"))
    except Exception:
        return None


def _fetch_kraken_spot(timeout: int = 5) -> float | None:
    try:
        data = _fetch_json("https://api.kraken.com/0/public/Ticker?pair=XBTUSD", timeout)
        return as_float(data.get("result", {}).get("XXBTZUSD", {}).get("c", [None])[0])
    except Exception:
        return None


class BinanceBtcClient:
    def __init__(
        self, 
        base_url: str, 
        coinbase_url: str = "", 
        kraken_url: str = "", 
        timeout: int = 5,
        ws_client: Optional[Any] = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.coinbase_url = coinbase_url.rstrip("/") if coinbase_url else "https://api.coinbase.com"
        self.kraken_url = kraken_url.rstrip("/") if kraken_url else "https://api.kraken.com"
        self.timeout = timeout
        self.ws_client = ws_client

    def _get_json(self, path: str, params: dict[str, str]) -> Any:
        query = urllib.parse.urlencode(params)
        url = f"{self.base_url}{path}?{query}"
        return _fetch_json(url, self.timeout)

    def _snapshot_exchange_prices(self) -> tuple[float | None, float | None, float | None]:
        binance_spot: float | None = None
        coinbase_spot: float | None = None
        kraken_spot: float | None = None
        
        # Priority 1: WebSocket for sub-100ms Binance price
        ws_price = self.ws_client.get_latest_price() if self.ws_client else 0.0
        if ws_price > 0:
            binance_spot = ws_price
        else:
            try:
                ticker = self._get_json("/api/v3/ticker/price", {"symbol": "BTCUSDT"})
                binance_spot = as_float(ticker.get("price") if isinstance(ticker, dict) else None)
            except Exception:
                pass
                
        coinbase_spot = _fetch_coinbase_spot(self.timeout)
        kraken_spot = _fetch_kraken_spot(self.timeout)
        return binance_spot, coinbase_spot, kraken_spot

    def snapshot(self, symbol: str, signal_path: Path | None = None) -> QuantBtcSnapshot:
        klines = self._get_json("/api/v3/klines", {"symbol": symbol, "interval": "5m", "limit": "2"})
        latest = klines[-1] if isinstance(klines, list) and klines else []
        binance_spot, coinbase_spot, kraken_spot = self._snapshot_exchange_prices()
        open_5m = as_float(latest[1] if len(latest) > 1 else None)
        close_5m = as_float(latest[4] if len(latest) > 4 else (binance_spot or 0.0))
        prices = [p for p in (binance_spot, coinbase_spot, kraken_spot) if p is not None and p > 0]
        spot = statistics.median(prices) if len(prices) >= 2 else (prices[0] if prices else 0.0)
        move_pct = (spot - open_5m) / open_5m if open_5m > 0 else 0.0
        exchange_count = len(prices)
        extra = _load_local_validators(signal_path)
        return QuantBtcSnapshot(
            symbol=symbol,
            spot=spot,
            open_5m=open_5m,
            close_5m=close_5m,
            move_pct=move_pct,
            spot_binance=binance_spot or 0.0,
            spot_coinbase=coinbase_spot or 0.0,
            spot_kraken=kraken_spot or 0.0,
            exchange_divergence=False,
            exchange_count=exchange_count,
            tradingview=extra.get("tradingview", ""),
            cryptoquant=extra.get("cryptoquant", ""),
            graph_bias=extra.get("graph_bias", ""),
            bull_nodes=int(as_float(extra.get("bull_nodes"))),
            bear_nodes=int(as_float(extra.get("bear_nodes"))),
            edges=int(as_float(extra.get("edges"))),
        )


def _check_exchange_divergence(
    snapshot: QuantBtcSnapshot,
    max_divergence_pct: float,
) -> tuple[bool, str]:
    prices = {
        "binance": snapshot.spot_binance,
        "coinbase": snapshot.spot_coinbase,
        "kraken": snapshot.spot_kraken,
    }
    valid = {name: p for name, p in prices.items() if p > 0}
    if len(valid) < 2:
        return False, ""
    median = statistics.median(valid.values())
    for name, p in valid.items():
        if abs(p - median) / median > max_divergence_pct:
            return True, f"{name}_diverged"
    return False, ""


def analyze_quant_bitcoin(
    candidates: list[Candidate],
    settings: Settings,
    snapshot: QuantBtcSnapshot,
) -> QuantBtcReport:
    rejected: dict[str, int] = {}
    if not settings.quant_btc_enabled:
        return QuantBtcReport(None, [], snapshot, {"disabled": 1})
    opportunities: list[QuantBtcSignal] = []
    for candidate in candidates:
        reason = _reject_candidate(candidate, settings)
        if reason:
            rejected[reason] = rejected.get(reason, 0) + 1
            continue

        ask = float(candidate.best_ask or 0.0)
        bid = float(candidate.best_bid or 0.0)
        if ask <= settings.quant_btc_min_buy_price:
            rejected["price_too_low"] = rejected.get("price_too_low", 0) + 1
            continue

        edge = ask - settings.quant_btc_min_buy_price
        spread = ask - bid
        opportunities.append(
            QuantBtcSignal(
                candidate=candidate,
                direction=_candidate_direction(candidate),
                spot=snapshot.spot,
                move_pct=snapshot.move_pct,
                expected_probability=ask,
                clob_lag_pct=0.0,
                edge=edge,
                spread=spread,
                validators={},
                price_threshold=settings.quant_btc_min_buy_price,
            )
        )

    opportunities.sort(key=lambda item: item.score, reverse=True)
    return QuantBtcReport(
        selected=opportunities[0] if opportunities else None,
        opportunities=opportunities,
        snapshot=snapshot,
        rejected=rejected,
    )


def _load_local_validators(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    graph = raw.get("force_graph") if isinstance(raw.get("force_graph"), dict) else {}
    bull_nodes = int(as_float(graph.get("bull_nodes") or raw.get("bull_nodes")))
    bear_nodes = int(as_float(graph.get("bear_nodes") or raw.get("bear_nodes")))
    graph_bias = str(raw.get("graph_bias") or "").upper()
    if not graph_bias and bull_nodes != bear_nodes:
        graph_bias = "BULL" if bull_nodes > bear_nodes else "BEAR"
    return {
        "tradingview": str(raw.get("tradingview") or "").upper(),
        "cryptoquant": str(raw.get("cryptoquant") or "").upper(),
        "graph_bias": graph_bias,
        "bull_nodes": str(bull_nodes),
        "bear_nodes": str(bear_nodes),
        "edges": str(int(as_float(graph.get("edges") or raw.get("edges")))),
    }


def _validators(snapshot: QuantBtcSnapshot) -> dict[str, str]:
    return {}


def _reject_candidate(candidate: Candidate, settings: Settings) -> str:
    if not _is_btc_market(candidate):
        return "not_btc_market"
    if not candidate.token_id or not candidate.accepts_orders:
        return "not_executable"
    if candidate.best_bid is None or candidate.best_ask is None or candidate.tick_size is None:
        return "missing_quote"
    if candidate.liquidity < settings.quant_btc_min_liquidity_usd:
        return "liquidity_too_low"
    if candidate.volume < settings.quant_btc_min_volume_usd:
        return "volume_too_low"
    hours = float(candidate.hours_to_close or 0.0)
    if hours < settings.quant_btc_min_hours_to_close:
        return "too_close"
    spread = candidate.best_ask - candidate.best_bid
    if spread < 0 or spread > settings.quant_btc_max_spread:
        return "spread_too_wide"
    if candidate.best_ask > 0 and spread / candidate.best_ask > settings.quant_btc_max_relative_spread:
        return "relative_spread_too_wide"
    return ""


def _is_btc_market(candidate: Candidate) -> bool:
    text = f"{candidate.question} {candidate.slug} {candidate.event_slug}".lower()
    return "bitcoin" in text or "btc" in text


def _candidate_direction(candidate: Candidate) -> str:
    outcome = (candidate.outcome or "").strip().lower()
    if outcome in {"up", "yes", "bull", "long", "higher", "above"}:
        return "BULL"
    if outcome in {"down", "no", "bear", "short", "lower", "below"}:
        return "BEAR"
    text = f"{candidate.question} {candidate.slug} {candidate.event_slug}".lower()
    if any(token in text for token in ("up", "yes", "bull", "above", "higher")):
        return "BULL"
    if any(token in text for token in ("down", "no", "bear", "below", "lower")):
        return "BEAR"
    return "BULL"


def _is_btc_micro_market(candidate: Candidate) -> bool:
    text = f"{candidate.question} {candidate.slug} {candidate.event_slug}".lower()
    has_micro = (
        "5m" in text 
        or "5 min" in text 
        or "5-minute" in text 
        or "up or down" in text
        or "up/down" in text
        or "updown" in text
        or "price-up-down" in text
        or "micro" in text
    )
    return _is_btc_market(candidate) and has_micro


def _is_btc_5m_market(candidate: Candidate) -> bool:
    text = f"{candidate.question} {candidate.slug} {candidate.event_slug}".lower()
    return _is_btc_micro_market(candidate) and (
        "btc-updown-5m" in text
        or re.search(r"(^|[^0-9a-z])5m([^0-9a-z]|$)", text) is not None
        or "5 min" in text
        or "5-minute" in text
    )


def quant_btc_series_key(candidate: Candidate) -> str:
    text = f"{candidate.event_slug or ''} {candidate.slug or ''} {candidate.question or ''}".lower()
    match = re.search(r"(btc-updown-(?:5m|15m|4h))-\d{6,}", text)
    if match:
        return match.group(1)
    match = re.search(r"(btc-updown-(?:5m|15m|4h))", text)
    if match:
        return match.group(1)
    if "bitcoin up or down" in text and "5m" in text:
        return "bitcoin-up-or-down-5m"
    return candidate.market_id or candidate.slug or candidate.question or ""


def recent_quant_btc_stop_loss_series_keys(
    journal_path: Path,
    *,
    now: datetime,
    cooldown_minutes: int,
    minimum_losses: int,
) -> set[str]:
    if cooldown_minutes <= 0 or minimum_losses <= 0 or not journal_path.is_file():
        return set()
    cutoff = now - timedelta(minutes=cooldown_minutes)
    counts: dict[str, int] = {}
    try:
        lines = journal_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(record.get("strategy") or "") != "quant_bitcoin":
            continue
        if str(record.get("exit_reason") or "") != "stop_loss":
            continue
        closed_at = parse_dt(str(record.get("closed_at") or ""))
        if closed_at is None or closed_at < cutoff:
            continue
        event_slug = str(record.get("event_slug") or "")
        market_id = str(record.get("market_id") or "")
        question = str(record.get("question") or "")
        slug_text = f"{event_slug} {market_id} {question}".lower()
        match = re.search(r"(btc-updown-(?:5m|15m|4h))-\d{6,}", slug_text)
        if match:
            key = match.group(1)
        elif "bitcoin up or down" in slug_text and "5m" in slug_text:
            key = "bitcoin-up-or-down-5m"
        else:
            key = event_slug or market_id or question
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count >= minimum_losses}
