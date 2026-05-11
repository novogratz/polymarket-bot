"""Read-only local HTML dashboard.

Serves an auto-refreshing summary of the bot's current ledger state,
recent trades, scanner output, and tick activity at
``http://127.0.0.1:8765``. The dashboard never places orders; it is a
passive view over the JSON state files the trading loop reads and
writes.

Endpoints:
- ``GET /``           — HTML page (template from dashboard_html).
- ``GET /api/state``  — ledger, positions, candidates, mode (legacy shape).
- ``GET /api/live``   — last tick + history (for the Live tab).
- ``GET /api/stats``  — aggregated trade-journal stats (for the Stats tab).
- ``GET /api/tune``   — auto-tuner overrides + suggestions (for the Tune tab).
"""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .config import Settings
from .dashboard_html import HTML
from .gamma import GammaClient
from .models import utc_now
from .portfolio import Portfolio
from .pricing import ensure_open_positions_in_pool
from .strategy import rank_markets
from . import tick_state
from .trading import build_client


# Set of Settings fields that the auto-tuner can override. Used by build_tune
# to compute the "default vs override" pairs the dashboard renders.
_TUNABLE_PARAMS = (
    "smart_max_chase_premium",
    "smart_max_relative_spread",
    "smart_min_consensus",
    "smart_sports_score_penalty",
    "smart_min_copied_usdc",
    "smart_position_pct",
)


def build_state(settings: Settings) -> dict[str, Any]:
    client = GammaClient(settings.gamma_base_url)
    now = utc_now()
    candidates = rank_markets(
        client.get_markets(
            limit=settings.scan_limit,
            end_date_min=now,
            end_date_max=now + timedelta(hours=settings.soon_hours),
        ),
        settings,
    )
    portfolio = Portfolio.load(settings.state_path, settings.paper_balance_usd)
    pricing_pool = ensure_open_positions_in_pool(settings, portfolio, candidates)
    portfolio.mark_to_market(pricing_pool)
    balance_source = "dry_run_ledger" if settings.dry_run else "local_ledger"
    live_balance_error = None
    if settings.dry_run:
        live_balance: float | str | None = None
    else:
        live_balance = _live_available_balance(settings)
        if isinstance(live_balance, float) and live_balance > 0:
            portfolio.cash = round(live_balance, 2)
            balance_source = "live_clob"
        elif isinstance(live_balance, str):
            live_balance_error = live_balance
    # NOTE: read-only dashboard — do NOT persist portfolio here. Writing
    # state.json on every HTTP refresh races with the auto-loop process,
    # and (when state.json is missing) seeds the ledger with the dashboard's
    # default paper_balance_usd instead of the run's actual starting cash.
    positions = portfolio.positions
    recent_trades = sorted(
        positions,
        key=lambda item: str(item.get("opened_at") or ""),
        reverse=True,
    )[:20]

    # Flatten every exit (partial or full) into a "closed trade" row so the
    # dashboard can show wins / losses, P&L, and the exit reason that fired.
    closed_trades: list[dict[str, Any]] = []
    for position in positions:
        for exit_record in position.get("exits") or []:
            closed_trades.append({
                "closed_at": exit_record.get("closed_at"),
                "question": position.get("question"),
                "url": position.get("url"),
                "strategy": position.get("strategy"),
                "live": position.get("live"),
                "outcome": position.get("outcome"),
                "entry_price": position.get("entry_price"),
                "exit_price": exit_record.get("exit_price"),
                "cost_basis": exit_record.get("cost_basis"),
                "proceeds": exit_record.get("proceeds"),
                "realized_pnl": exit_record.get("realized_pnl"),
                "reason": exit_record.get("reason"),
            })
    closed_trades.sort(key=lambda item: str(item.get("closed_at") or ""), reverse=True)
    closed_trades = closed_trades[:30]

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "live_trading_enabled": settings.live_trading_enabled,
        "dry_run": settings.dry_run,
        "state_path": str(settings.state_path),
        "auto_interval_seconds": settings.auto_interval_seconds,
        "balance_source": balance_source,
        "live_balance_error": live_balance_error,
        "summary": portfolio.summary(),
        "positions": [position for position in positions if position.get("status") == "open"],
        "recent_trades": recent_trades,
        "closed_trades": closed_trades,
        "candidates": [candidate.to_dict() for candidate in candidates[:40]],
    }


def build_live(settings: Settings) -> dict[str, Any]:
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "dry_run" if settings.dry_run else "live",
        "auto_interval_seconds": settings.auto_interval_seconds,
        "last_tick": tick_state.read_last_tick(settings),
        "history": tick_state.read_tick_history(settings, limit=20),
    }


def build_stats(settings: Settings) -> dict[str, Any]:
    from .main import journal_stats  # local import to avoid circular dependency
    payload = journal_stats(settings)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    return payload


def build_tune(settings: Settings) -> dict[str, Any]:
    from .main import journal_stats  # local import to avoid circular dependency
    overrides_payload: dict[str, Any] = {}
    if settings.strategy_overrides_path.exists():
        try:
            overrides_payload = json.loads(settings.strategy_overrides_path.read_text())
        except Exception:
            overrides_payload = {}
    field_defaults = {
        f.name: f.default
        for f in fields(Settings)
        if f.name in _TUNABLE_PARAMS
    }
    overrides_active = dict(overrides_payload.get("overrides") or {})
    stats_payload = journal_stats(settings)
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "enabled": settings.smart_auto_tune_enabled,
        "min_trades_required": settings.smart_auto_tune_min_trades,
        "records_observed": int(overrides_payload.get("records_observed") or 0),
        "overrides_active": overrides_active,
        "defaults": field_defaults,
        "overrides_path": str(settings.strategy_overrides_path),
        "overrides_generated_at": overrides_payload.get("generated_at"),
        "suggestions": stats_payload.get("suggestions", []),
        "mode": "dry_run" if settings.dry_run else "live",
    }


def _live_available_balance(settings: Settings) -> float | str | None:
    if not (settings.private_key and settings.api_key and settings.api_secret and settings.api_passphrase):
        return None
    try:
        client = build_client(settings)
        return client.live_available_balance()
    except Exception as exc:
        return str(exc)


# Backwards-compatibility alias — kept so any caller still importing
# `snapshot` from this module keeps working.
snapshot = build_state


class DashboardHandler(BaseHTTPRequestHandler):
    settings = Settings()

    _ROUTES = {
        "/api/state": build_state,
        "/api/live": build_live,
        "/api/stats": build_stats,
        "/api/tune": build_tune,
    }

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/?"):
            self._send(HTML, "text/html; charset=utf-8")
            return
        for route, builder in self._ROUTES.items():
            if self.path == route:
                payload = builder(self.settings)
                self._send(json.dumps(payload, default=str), "application/json")
                return
        self.send_error(404)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, body: str, content_type: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        # Le ledger / journal / tick history changent à chaque tick : on
        # interdit tout cache (browser ou intermédiaire) pour que la page
        # reflète toujours l'état actuel des fichiers sur disque.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(encoded)


def serve(settings: Settings) -> None:
    DashboardHandler.settings = settings
    server = ThreadingHTTPServer((settings.dashboard_host, settings.dashboard_port), DashboardHandler)
    print(f"Dashboard: http://{settings.dashboard_host}:{settings.dashboard_port}")
    server.serve_forever()
