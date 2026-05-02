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


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Polymarket Bot Dashboard</title>
  <style>
    :root { color-scheme: light; --ink:#172026; --muted:#5d6973; --line:#d7dde2; --bg:#f6f7f8; --panel:#fff; --accent:#0f766e; --loss:#b42318; --warn:#9a6700; }
    * { box-sizing: border-box; }
    body { margin: 0; font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background: var(--bg); }
    header { padding: 20px 28px 14px; border-bottom: 1px solid var(--line); background: var(--panel); position: sticky; top: 0; z-index: 2; }
    h1 { margin: 0 0 8px; font-size: 24px; letter-spacing: 0; }
    main { padding: 20px 28px 32px; max-width: 1280px; margin: 0 auto; }
    .meta { display: flex; gap: 16px; flex-wrap: wrap; color: var(--muted); }
    .stats { display: grid; grid-template-columns: repeat(6, minmax(120px, 1fr)); gap: 12px; margin-bottom: 22px; }
    .stat, table { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
    .stat { padding: 14px; }
    .label { color: var(--muted); font-size: 12px; text-transform: uppercase; }
    .value { font-size: 22px; margin-top: 4px; font-weight: 650; }
    h2 { font-size: 16px; margin: 22px 0 10px; }
    table { width: 100%; border-collapse: separate; border-spacing: 0; overflow: hidden; }
    th, td { padding: 10px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { font-size: 12px; color: var(--muted); background: #f0f3f4; text-transform: uppercase; }
    tr:last-child td { border-bottom: 0; }
    a { color: var(--accent); text-decoration: none; }
    .num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
    .pnl-pos { color: var(--accent); }
    .pnl-neg { color: var(--loss); }
    .question { min-width: 300px; }
    .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; background: #e7f5f2; color: var(--accent); font-size: 12px; font-weight: 650; }
    .pill.off { background: #fff4d6; color: var(--warn); }
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
    <div class="meta"><span id="status">Loading</span><span id="mode"></span><span>Auto-refreshing local bot state</span></div>
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
        "summary": portfolio.summary(),
        "positions": [position for position in positions if position.get("status") == "open"],
        "recent_trades": recent_trades,
        "candidates": [candidate.to_dict() for candidate in candidates[:40]],
    }


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
