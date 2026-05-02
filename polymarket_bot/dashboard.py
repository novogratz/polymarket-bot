from __future__ import annotations

import json
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .config import Settings
from .gamma import GammaClient
from .models import utc_now
from .portfolio import Portfolio
from .strategy import rank_markets
from .trading import build_client


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Polymarket Bot Dashboard</title>
  <style>
    :root { color-scheme: dark; --ink:#e9fff6; --muted:#7fa89b; --line:#13392d; --bg:#020504; --panel:#07110e; --panel2:#0a1a14; --accent:#32ff9f; --accent2:#00d9ff; --loss:#ff4f6d; --warn:#f4c95d; }
    * { box-sizing: border-box; }
    body { margin: 0; font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background: radial-gradient(circle at 20% 0%, rgba(50,255,159,.14), transparent 32%), linear-gradient(180deg, #020504 0%, #03100b 55%, #020504 100%); }
    header { padding: 22px 28px 16px; border-bottom: 1px solid var(--line); background: rgba(3, 12, 9, .92); backdrop-filter: blur(14px); position: sticky; top: 0; z-index: 2; box-shadow: 0 0 32px rgba(50,255,159,.08); }
    h1 { margin: 0 0 8px; font-size: 25px; letter-spacing: 0; background: linear-gradient(90deg, var(--accent), var(--accent2)); -webkit-background-clip: text; color: transparent; }
    main { padding: 20px 28px 32px; max-width: 1280px; margin: 0 auto; }
    .meta { display: flex; gap: 16px; flex-wrap: wrap; color: var(--muted); }
    .stats { display: grid; grid-template-columns: repeat(6, minmax(120px, 1fr)); gap: 12px; margin-bottom: 22px; }
    .stat, table { background: linear-gradient(180deg, rgba(10,26,20,.95), rgba(5,12,10,.95)); border: 1px solid var(--line); border-radius: 8px; box-shadow: 0 0 0 1px rgba(50,255,159,.03), 0 12px 40px rgba(0,0,0,.28); }
    .stat { padding: 14px; position: relative; overflow: hidden; }
    .stat::before { content: ""; position: absolute; inset: 0 0 auto; height: 2px; background: linear-gradient(90deg, var(--accent), transparent); opacity: .8; }
    .label { color: var(--muted); font-size: 12px; text-transform: uppercase; }
    .value { font-size: 22px; margin-top: 4px; font-weight: 700; color: var(--accent); text-shadow: 0 0 18px rgba(50,255,159,.26); }
    h2 { font-size: 16px; margin: 22px 0 10px; }
    table { width: 100%; border-collapse: separate; border-spacing: 0; overflow: hidden; }
    th, td { padding: 10px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { font-size: 12px; color: var(--muted); background: rgba(50,255,159,.06); text-transform: uppercase; }
    tr:last-child td { border-bottom: 0; }
    tr:hover td { background: rgba(50,255,159,.035); }
    a { color: var(--accent); text-decoration: none; }
    .num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
    .pnl-pos { color: var(--accent); }
    .pnl-neg { color: var(--loss); }
    .question { min-width: 300px; }
    .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; background: rgba(50,255,159,.12); color: var(--accent); border: 1px solid rgba(50,255,159,.28); font-size: 12px; font-weight: 650; }
    .pill.off { background: rgba(244,201,93,.12); color: var(--warn); border-color: rgba(244,201,93,.32); }
    @media (max-width: 760px) {
      header, main { padding-left: 14px; padding-right: 14px; }
      .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      table { display: block; overflow-x: auto; white-space: nowrap; }
      .question { min-width: 240px; white-space: normal; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Polymarket Bot Dashboard</h1>
    <div class="meta"><span id="status">Loading</span><span id="mode"></span><span id="balance-source"></span><span>Auto-refreshing bot state</span></div>
  </header>
  <main>
    <section class="stats" id="stats"></section>
    <h2>Open Positions</h2>
    <table><thead><tr><th>Market</th><th>Strategy</th><th>Outcome</th><th class="num">Entry</th><th class="num">Now</th><th class="num">Stake</th><th class="num">PnL</th></tr></thead><tbody id="positions"></tbody></table>
    <h2>Recent Bot Trades</h2>
    <table><thead><tr><th>Opened</th><th>Market</th><th>Strategy</th><th>Outcome</th><th class="num">Entry</th><th class="num">Stake</th><th>Order</th></tr></thead><tbody id="trades"></tbody></table>
    <h2>Soon Markets</h2>
    <table><thead><tr><th>Market</th><th>Outcome</th><th class="num">Price</th><th class="num">Closes</th><th class="num">Liquidity</th><th class="num">Volume</th><th class="num">Score</th></tr></thead><tbody id="candidates"></tbody></table>
  </main>
  <script>
    const fmtUsd = new Intl.NumberFormat(undefined, {style:'currency', currency:'USD'});
    const fmt = new Intl.NumberFormat(undefined, {maximumFractionDigits: 2});
    function cell(value, cls='') { return `<td class="${cls}">${value}</td>`; }
    async function refresh() {
      const response = await fetch('/api/state');
      const data = await response.json();
      document.getElementById('status').textContent = `Updated ${new Date(data.updated_at).toLocaleTimeString()} · ${data.candidates.length} candidates`;
      document.getElementById('mode').innerHTML = data.live_trading_enabled ? '<span class="pill">live enabled</span>' : '<span class="pill off">live disabled</span>';
      document.getElementById('balance-source').innerHTML = data.balance_source === 'live_clob' ? '<span class="pill">live balance synced</span>' : '<span class="pill off">local ledger balance</span>';
      const s = data.summary;
      document.getElementById('stats').innerHTML = [
        ['Equity', fmtUsd.format(s.equity)], ['Cash', fmtUsd.format(s.cash)], ['Invested', fmtUsd.format(s.invested)],
        ['Open', s.open_positions], ['Trades', data.recent_trades.length], ['Unrealized PnL', fmtUsd.format(s.unrealized_pnl)]
      ].map(([k,v]) => `<div class="stat"><div class="label">${k}</div><div class="value">${v}</div></div>`).join('');
      document.getElementById('positions').innerHTML = data.positions.length ? data.positions.map(p => {
        const pnl = Number(p.unrealized_pnl || 0);
        return `<tr>${cell(`<a href="${p.url}" target="_blank" rel="noreferrer">${p.question}</a>`, 'question')}${cell(p.strategy || (p.live ? 'live' : 'paper'))}${cell(p.outcome)}${cell(fmt.format(p.entry_price), 'num')}${cell(fmt.format(p.current_price), 'num')}${cell(fmtUsd.format(p.stake), 'num')}${cell(fmtUsd.format(pnl), `num ${pnl < 0 ? 'pnl-neg' : 'pnl-pos'}`)}</tr>`;
      }).join('') : '<tr><td colspan="7">No open positions yet.</td></tr>';
      document.getElementById('trades').innerHTML = data.recent_trades.length ? data.recent_trades.map(p => {
        const orderId = p.order_id || (p.order_response && (p.order_response.orderID || p.order_response.orderId)) || '';
        return `<tr>${cell(new Date(p.opened_at).toLocaleString())}${cell(`<a href="${p.url}" target="_blank" rel="noreferrer">${p.question}</a>`, 'question')}${cell(p.strategy || (p.live ? 'live' : 'paper'))}${cell(p.outcome)}${cell(fmt.format(p.entry_price), 'num')}${cell(fmtUsd.format(p.stake), 'num')}${cell(orderId || '-')}</tr>`;
      }).join('') : '<tr><td colspan="7">No trades recorded in the local ledger yet.</td></tr>';
      document.getElementById('candidates').innerHTML = data.candidates.map(c => {
        return `<tr>${cell(`<a href="${c.url}" target="_blank" rel="noreferrer">${c.question}</a>`, 'question')}${cell(c.outcome)}${cell(fmt.format(c.price), 'num')}${cell(fmt.format(c.hours_to_close) + 'h', 'num')}${cell(fmtUsd.format(c.liquidity), 'num')}${cell(fmtUsd.format(c.volume), 'num')}${cell(fmt.format(c.score), 'num')}</tr>`;
      }).join('');
    }
    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>
"""


def snapshot(settings: Settings) -> dict[str, Any]:
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
    portfolio.mark_to_market(candidates)
    balance_source = "local_ledger"
    live_balance_error = None
    live_balance = _live_available_balance(settings)
    if isinstance(live_balance, float) and live_balance > 0:
        portfolio.cash = round(live_balance, 2)
        balance_source = "live_clob"
    elif isinstance(live_balance, str):
        live_balance_error = live_balance
    portfolio.save(settings.state_path)
    positions = portfolio.positions
    recent_trades = sorted(
        positions,
        key=lambda item: str(item.get("opened_at") or ""),
        reverse=True,
    )[:20]
    return {
        "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "live_trading_enabled": settings.live_trading_enabled,
        "auto_interval_seconds": settings.auto_interval_seconds,
        "balance_source": balance_source,
        "live_balance_error": live_balance_error,
        "summary": portfolio.summary(),
        "positions": [position for position in positions if position.get("status") == "open"],
        "recent_trades": recent_trades,
        "candidates": [candidate.to_dict() for candidate in candidates[:40]],
    }


def _live_available_balance(settings: Settings) -> float | str | None:
    if not (settings.private_key and settings.api_key and settings.api_secret and settings.api_passphrase):
        return None
    try:
        client = build_client(settings)
        return client.live_available_balance()
    except Exception as exc:
        return str(exc)


class DashboardHandler(BaseHTTPRequestHandler):
    settings = Settings()

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/?"):
            self._send(HTML, "text/html; charset=utf-8")
            return
        if self.path == "/api/state":
            self._send(json.dumps(snapshot(self.settings)), "application/json")
            return
        self.send_error(404)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, body: str, content_type: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def serve(settings: Settings) -> None:
    DashboardHandler.settings = settings
    server = ThreadingHTTPServer((settings.dashboard_host, settings.dashboard_port), DashboardHandler)
    print(f"Dashboard: http://{settings.dashboard_host}:{settings.dashboard_port}")
    server.serve_forever()
