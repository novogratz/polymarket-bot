"""HTML/CSS/JS template for the dashboard.

Single-page vanilla JS app with a sticky header, sticky stats bar,
sticky tab bar, and per-tab content. Tabs:
- Live (default): last tick + history of recent ticks
- Positions: open positions
- Closed: closed trades with All/Wins/Losses filter
- Stats: journal aggregations + max drawdown
- Scanner: ranked Soon Markets
- Tune: auto-tuner overrides + suggestions

Style is "trading terminal" — dark, sober, monospace numerals, no
neon. The active tab is mirrored to localStorage but on first load
defaults to Live.

All polling is client-side: stats bar refreshes every 5s via
/api/state regardless of active tab; the active tab refreshes its own
endpoint at its own cadence (see PMBOT.refreshIntervalsMs).
"""

HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PMBOT</title>
  <style>
    :root {
      --bg:#0d1117; --panel:#161b22; --line:#30363d;
      --ink:#e6edf3; --muted:#8b949e;
      --accent:#f78166; --gain:#3fb950; --loss:#f85149; --warn:#d29922;
      color-scheme: dark;
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; background: var(--bg); color: var(--ink); }
    body {
      font: 13px/1.4 "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      min-height: 100vh;
    }
    code, .mono, .num, .stat .value, .pill, td.num, th.num, td.market, .tick-id {
      font-family: ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace;
      font-variant-numeric: tabular-nums;
    }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }

    header.topbar {
      position: sticky; top: 0; z-index: 5;
      background: var(--bg); border-bottom: 1px solid var(--line);
      padding: 10px 18px; display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    }
    header.topbar .brand { font-weight: 700; color: var(--accent); letter-spacing: 0.5px; font-family: ui-monospace, Menlo, monospace; }
    header.topbar .clock { color: var(--muted); font-family: ui-monospace, Menlo, monospace; }
    .pill {
      display: inline-block; padding: 2px 8px; border-radius: 3px;
      background: rgba(63,185,80,0.12); color: var(--gain);
      border: 1px solid rgba(63,185,80,0.4); font-size: 11px; font-weight: 600;
    }
    .pill.warn { background: rgba(210,153,34,0.12); color: var(--warn); border-color: rgba(210,153,34,0.4); }
    .pill.loss { background: rgba(248,81,73,0.12); color: var(--loss); border-color: rgba(248,81,73,0.4); }
    .pill.muted { background: transparent; color: var(--muted); border-color: var(--line); }

    .stats-bar {
      position: sticky; top: 49px; z-index: 4;
      background: var(--panel); border-bottom: 1px solid var(--line);
      display: grid; grid-template-columns: repeat(6, 1fr); gap: 0;
    }
    .stat { padding: 10px 14px; border-right: 1px solid var(--line); }
    .stat:last-child { border-right: 0; }
    .stat .label { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; }
    .stat .value { font-size: 18px; font-weight: 700; margin-top: 2px; color: var(--ink); }
    .stat .value.gain { color: var(--gain); }
    .stat .value.loss { color: var(--loss); }

    nav.tabs {
      position: sticky; top: 117px; z-index: 3;
      background: var(--bg); border-bottom: 1px solid var(--line);
      display: flex; gap: 0; overflow-x: auto; -webkit-overflow-scrolling: touch;
    }
    nav.tabs button {
      background: transparent; border: 0; border-bottom: 2px solid transparent;
      color: var(--muted); padding: 10px 16px; cursor: pointer; font: inherit; font-weight: 600;
      letter-spacing: 0.3px; white-space: nowrap;
    }
    nav.tabs button:hover { color: var(--ink); }
    nav.tabs button.active { color: var(--accent); border-bottom-color: var(--accent); }

    main { padding: 16px 18px 32px; max-width: 1400px; margin: 0 auto; }
    section.tab { display: none; }
    section.tab.active { display: block; }

    h2 { font-size: 14px; color: var(--accent); text-transform: uppercase; letter-spacing: 0.5px; margin: 18px 0 8px; font-weight: 700; }
    .empty { color: var(--muted); padding: 16px; border: 1px dashed var(--line); border-radius: 4px; }

    table { width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); border-radius: 4px; overflow: hidden; }
    th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--line); vertical-align: top; }
    th { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; background: var(--bg); }
    tr:last-child td { border-bottom: 0; }
    tr:hover td { background: rgba(247,129,102,0.04); }
    td.num, th.num { text-align: right; }
    td.gain { color: var(--gain); }
    td.loss { color: var(--loss); }
    .filter-bar { margin-bottom: 8px; display: flex; gap: 4px; }
    .filter-bar button {
      background: transparent; border: 1px solid var(--line); color: var(--muted);
      padding: 4px 10px; border-radius: 3px; cursor: pointer; font: inherit; font-size: 11px;
    }
    .filter-bar button.active { color: var(--accent); border-color: var(--accent); }

    .tick-card { background: var(--panel); border: 1px solid var(--line); border-radius: 4px; padding: 14px 16px; margin-bottom: 12px; }
    .tick-card .head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }
    .tick-card .head .tick-id { color: var(--accent); font-weight: 700; }
    .tick-card .head .meta { color: var(--muted); font-size: 11px; }
    .tick-card .scan { color: var(--muted); font-size: 12px; margin-bottom: 6px; }
    .tick-card ul { margin: 4px 0; padding-left: 18px; }
    .tick-card li { padding: 1px 0; }
    .tick-card li.buy { color: var(--gain); }
    .tick-card li.sell { color: var(--loss); }
    .tick-card li.skip { color: var(--muted); }

    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 4px; padding: 12px 14px; }
    .panel h3 { margin: 0 0 8px; font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
    .kv { display: grid; grid-template-columns: 1fr auto; gap: 4px 12px; font-size: 12px; }
    .kv dt { color: var(--muted); }
    .kv dd { margin: 0; font-family: ui-monospace, Menlo, monospace; text-align: right; }

    footer { margin-top: 32px; padding: 12px 18px; border-top: 1px solid var(--line); color: var(--muted); font-size: 11px; display: flex; gap: 16px; flex-wrap: wrap; }

    @media (max-width: 760px) {
      .stats-bar { grid-template-columns: repeat(2, 1fr); }
      .stat { border-right: 0; border-bottom: 1px solid var(--line); }
      .stats-bar .stat:nth-child(2n) { border-right: 0; }
      .grid-2 { grid-template-columns: 1fr; }
      table { display: block; overflow-x: auto; white-space: nowrap; }
      nav.tabs { top: 165px; }
      main { padding: 12px; }
    }
  </style>
</head>
<body>
  <header class="topbar">
    <span class="brand">PMBOT</span>
    <span class="clock" id="clock">--:--:--</span>
    <span id="mode-pill"></span>
    <span id="balance-pill"></span>
    <span id="updated" style="margin-left:auto;color:var(--muted);font-size:11px">loading…</span>
  </header>

  <div class="stats-bar" id="stats-bar"></div>

  <nav class="tabs" id="tabs">
    <button data-tab="live" class="active">Live</button>
    <button data-tab="positions">Positions</button>
    <button data-tab="closed">Closed</button>
    <button data-tab="stats">Stats</button>
    <button data-tab="scanner">Scanner</button>
    <button data-tab="tune">Tune</button>
  </nav>

  <main>
    <section class="tab active" data-tab="live"><div id="live-content"><div class="empty">Loading…</div></div></section>
    <section class="tab" data-tab="positions"><div id="positions-content"><div class="empty">Loading…</div></div></section>
    <section class="tab" data-tab="closed"><div id="closed-content"><div class="empty">Loading…</div></div></section>
    <section class="tab" data-tab="stats"><div id="stats-content"><div class="empty">Loading…</div></div></section>
    <section class="tab" data-tab="scanner"><div id="scanner-content"><div class="empty">Loading…</div></div></section>
    <section class="tab" data-tab="tune"><div id="tune-content"><div class="empty">Loading…</div></div></section>
  </main>

  <footer>
    <span id="ledger-info">—</span>
    <span id="refresh-info">—</span>
    <span style="margin-left:auto">read-only</span>
  </footer>

  <script>
    const PMBOT = {
      currentTab: localStorage.getItem('pmbot_dashboard_tab') || 'live',
      state: null,
      refreshIntervalsMs: { state: 5000, live: 5000, stats: 15000, tune: 15000 },
      timers: {},
    };

    const fmtUsd = new Intl.NumberFormat(undefined, {style:'currency', currency:'USD'});
    const fmt2 = new Intl.NumberFormat(undefined, {maximumFractionDigits: 2});
    const fmt4 = new Intl.NumberFormat(undefined, {maximumFractionDigits: 4});
    const fmtPct = (v) => (v == null ? '—' : (v * 100).toFixed(1) + '%');

    function el(tag, attrs, ...children) {
      const node = document.createElement(tag);
      for (const k in attrs) {
        if (k === 'class') node.className = attrs[k];
        else if (k === 'html') node.innerHTML = attrs[k];
        else if (k.startsWith('on')) node.addEventListener(k.slice(2), attrs[k]);
        else node.setAttribute(k, attrs[k]);
      }
      for (const c of children.flat()) {
        if (c == null) continue;
        node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
      }
      return node;
    }
    function td(content, cls) {
      const c = el('td'); if (cls) c.className = cls;
      if (content && content.nodeType) c.appendChild(content);
      else c.textContent = content == null ? '—' : String(content);
      return c;
    }
    function pnlClass(v) { return Number(v) < 0 ? 'loss' : Number(v) > 0 ? 'gain' : ''; }

    function fmtCloses(iso) {
      if (!iso) return '—';
      const d = new Date(iso);
      if (isNaN(d.getTime())) return iso;
      const diffMs = d.getTime() - Date.now();
      const absH = Math.abs(diffMs) / 3600000;
      const human = absH < 1 ? Math.round(Math.abs(diffMs)/60000)+'m' : absH < 48 ? absH.toFixed(1)+'h' : (absH/24).toFixed(1)+'d';
      return diffMs < 0 ? 'expired '+human+' ago' : 'in '+human;
    }

    async function getJson(url) {
      const r = await fetch(url, {cache: 'no-store'});
      if (!r.ok) throw new Error('HTTP '+r.status);
      return r.json();
    }

    function updateClock() {
      document.getElementById('clock').textContent = new Date().toLocaleTimeString();
    }

    function renderTopbar(state) {
      const modePill = state.dry_run
        ? '<span class="pill warn">DRY-RUN</span>'
        : state.live_trading_enabled ? '<span class="pill">LIVE</span>' : '<span class="pill warn">PAUSED</span>';
      document.getElementById('mode-pill').innerHTML = modePill;
      const bp = state.balance_source === 'live_clob' ? '<span class="pill muted">live cash synced</span>'
        : state.balance_source === 'dry_run_ledger' ? '<span class="pill muted">virtual cash</span>'
        : '<span class="pill muted">local ledger</span>';
      document.getElementById('balance-pill').innerHTML = bp;
      document.getElementById('updated').textContent = 'updated ' + new Date(state.updated_at).toLocaleTimeString();
      document.getElementById('ledger-info').textContent = state.state_path;
      document.getElementById('refresh-info').textContent = 'auto-refresh ' + (PMBOT.refreshIntervalsMs[PMBOT.currentTab] || 5000)/1000 + 's';
    }

    function renderStatsBar(state) {
      const s = state.summary;
      const items = [
        ['Equity', fmtUsd.format(s.equity), ''],
        ['Cash', fmtUsd.format(s.cash), ''],
        ['Invested', fmtUsd.format(s.invested), ''],
        ['Open', String(s.open_positions), ''],
        ['Unreal. PnL', fmtUsd.format(s.unrealized_pnl), pnlClass(s.unrealized_pnl)],
        ['Win %', '—', ''],
      ];
      document.getElementById('stats-bar').innerHTML = items.map(([k,v,cls]) =>
        `<div class="stat"><div class="label">${k}</div><div class="value ${cls}">${v}</div></div>`
      ).join('');
    }

    async function fillWinRate() {
      try {
        const stats = await getJson('/api/stats');
        const cells = document.querySelectorAll('#stats-bar .stat');
        if (cells.length >= 6 && stats && typeof stats.win_rate === 'number') {
          const v = cells[5].querySelector('.value');
          v.textContent = (stats.win_rate*100).toFixed(1)+'%';
        }
      } catch (e) {}
    }

    function renderLive(payload) {
      const root = document.getElementById('live-content');
      root.innerHTML = '';
      const last = payload.last_tick;
      if (!last) {
        root.appendChild(el('div', {class:'empty'}, 'En attente du premier tick après cette mise à jour…'));
        return;
      }
      const card = el('div', {class:'tick-card'});
      const head = el('div', {class:'head'},
        el('span', {class:'tick-id'}, '#'+(last.tick_id ?? '?')),
        el('span', {class:'meta'}, 'started ' + new Date(last.started_at).toLocaleTimeString()),
        el('span', {class:'meta'}, 'duration ' + (last.duration_s ?? '?') + 's'),
        el('span', {class:'meta'}, last.mode === 'dry_run' ? '· dry-run' : '· live'),
        el('span', {class:'meta', id:'next-tick-countdown'}, ''),
      );
      card.appendChild(head);

      const sc = last.scan_counts || {};
      card.appendChild(el('div', {class:'scan'},
        'scan: strict ' + (sc.strict ?? 0) + ' → cash ' + (sc.cash_pressure ?? 0)
        + ' → relaxed ' + (sc.relaxed ?? 0) + ' → deep ' + (sc.deep ?? 0)
        + ' (candidates: ' + (sc.candidates_total ?? 0) + ')'));
      const rej = last.rejection_summary || {};
      const rejEntries = Object.entries(rej).sort((a,b) => Number(b[1])-Number(a[1])).slice(0, 5);
      if (rejEntries.length) {
        card.appendChild(el('div', {class:'scan'},
          'top rejects: ' + rejEntries.map(([k,v]) => k + ' ' + v).join(' · ')));
      }

      const acts = last.actions || [];
      if (acts.length === 0) {
        card.appendChild(el('div', {class:'empty'}, 'idle — no action this tick'));
      } else {
        const ul = el('ul');
        for (const a of acts) {
          const cls = a.type === 'buy' ? 'buy' : a.type === 'sell' ? 'sell' : 'skip';
          const amt = a.amount_usd != null ? ' ' + fmtUsd.format(a.amount_usd) : '';
          const reason = a.reason ? ' (' + a.reason + ')' : '';
          ul.appendChild(el('li', {class:cls}, a.type.toUpperCase() + ' ' + (a.market || '?') + amt + reason));
        }
        card.appendChild(ul);
      }
      const tuner = last.tuner_changes || {};
      if (tuner.applied) {
        card.appendChild(el('div', {class:'scan'},
          'auto-tuner applied ' + Object.keys(tuner.overrides_active || {}).length + ' override(s) from '
          + (tuner.journal_size ?? 0) + ' closed trades'));
      }
      root.appendChild(card);

      if (last.next_tick_at) updateCountdown(last.next_tick_at);

      const hist = payload.history || [];
      if (hist.length > 1) {
        root.appendChild(el('h2', {}, 'Recent ticks'));
        const table = el('table');
        const thead = el('thead', {}, el('tr', {},
          el('th', {}, 'Tick'), el('th', {}, 'Started'), el('th', {class:'num'}, 'Duration'),
          el('th', {}, 'Result'), el('th', {}, 'Mode'),
        ));
        table.appendChild(thead);
        const tbody = el('tbody');
        for (const t of hist) {
          const buys = (t.actions || []).filter(a => a.type === 'buy').length;
          const sells = (t.actions || []).filter(a => a.type === 'sell').length;
          const summary = (buys+sells === 0) ? 'idle' : (buys+'B/'+sells+'S');
          tbody.appendChild(el('tr', {},
            td('#'+(t.tick_id ?? '?'), 'mono'),
            td(new Date(t.started_at).toLocaleTimeString()),
            td((t.duration_s ?? '?')+'s', 'num'),
            td(summary),
            td(t.mode || '—'),
          ));
        }
        table.appendChild(tbody);
        root.appendChild(table);
      }
    }

    function updateCountdown(nextIso) {
      const target = new Date(nextIso).getTime();
      function tick() {
        const sec = Math.max(0, Math.round((target - Date.now())/1000));
        const node = document.getElementById('next-tick-countdown');
        if (node) node.textContent = '· next in '+sec+'s';
      }
      tick();
      if (PMBOT.timers.countdown) clearInterval(PMBOT.timers.countdown);
      PMBOT.timers.countdown = setInterval(tick, 1000);
    }

    function renderPositions(state) {
      const root = document.getElementById('positions-content');
      const positions = state.positions || [];
      if (!positions.length) {
        root.innerHTML = '';
        root.appendChild(el('div', {class:'empty'}, 'No open positions yet.'));
        return;
      }
      const table = el('table');
      table.appendChild(el('thead', {}, el('tr', {},
        el('th', {}, 'Market'), el('th', {}, 'Strategy'), el('th', {}, 'Outcome'),
        el('th', {class:'num'}, 'Entry'), el('th', {class:'num'}, 'Now'),
        el('th', {class:'num'}, 'Stake'), el('th', {class:'num'}, 'PnL'), el('th', {}, 'Closes'),
      )));
      const tbody = el('tbody');
      for (const p of positions) {
        const pnl = Number(p.unrealized_pnl || 0);
        const link = el('a', {href: p.url, target:'_blank', rel:'noreferrer'}, p.question || '?');
        tbody.appendChild(el('tr', {},
          td(link, 'market'),
          td(p.strategy || (p.live ? 'live' : 'paper')),
          td(p.outcome),
          td(fmt4.format(p.entry_price), 'num'),
          td(fmt4.format(p.current_price), 'num'),
          td(fmtUsd.format(p.stake), 'num'),
          td(fmtUsd.format(pnl), 'num ' + pnlClass(pnl)),
          td(fmtCloses(p.end_date)),
        ));
      }
      table.appendChild(tbody);
      root.innerHTML = '';
      root.appendChild(table);
    }

    function renderClosed(state) {
      const root = document.getElementById('closed-content');
      const closed = state.closed_trades || [];
      const filter = root.dataset.filter || 'all';
      const filtered = filter === 'wins' ? closed.filter(t => Number(t.realized_pnl || 0) > 0)
        : filter === 'losses' ? closed.filter(t => Number(t.realized_pnl || 0) < 0)
        : closed;

      const bar = el('div', {class:'filter-bar'});
      for (const f of ['all','wins','losses']) {
        const btn = el('button', {class: filter === f ? 'active':'', onclick: () => {
          root.dataset.filter = f; renderClosed(state);
        }}, f.charAt(0).toUpperCase()+f.slice(1));
        bar.appendChild(btn);
      }

      root.innerHTML = '';
      root.appendChild(bar);

      if (!filtered.length) {
        root.appendChild(el('div', {class:'empty'}, 'No closed trades match this filter yet.'));
        return;
      }
      const table = el('table');
      table.appendChild(el('thead', {}, el('tr', {},
        el('th', {}, 'Closed'), el('th', {}, 'Market'), el('th', {}, 'Strategy'),
        el('th', {class:'num'}, 'Entry'), el('th', {class:'num'}, 'Exit'),
        el('th', {class:'num'}, 'Cost'), el('th', {class:'num'}, 'Proceeds'),
        el('th', {class:'num'}, 'PnL'), el('th', {class:'num'}, 'Return'), el('th', {}, 'Reason'),
      )));
      const tbody = el('tbody');
      for (const t of filtered) {
        const pnl = Number(t.realized_pnl || 0);
        const cost = Number(t.cost_basis || 0);
        const ret = cost > 0 ? (pnl/cost)*100 : 0;
        const link = el('a', {href: t.url, target:'_blank', rel:'noreferrer'}, t.question || '?');
        tbody.appendChild(el('tr', {},
          td(t.closed_at ? new Date(t.closed_at).toLocaleString() : '—'),
          td(link, 'market'),
          td(t.strategy || (t.live ? 'live' : 'paper')),
          td(fmt4.format(t.entry_price), 'num'),
          td(fmt4.format(t.exit_price), 'num'),
          td(fmtUsd.format(t.cost_basis), 'num'),
          td(fmtUsd.format(t.proceeds), 'num'),
          td(fmtUsd.format(pnl), 'num '+pnlClass(pnl)),
          td(ret.toFixed(1)+'%', 'num '+pnlClass(pnl)),
          td(t.reason || '—'),
        ));
      }
      table.appendChild(tbody);
      root.appendChild(table);
    }

    function renderStats(payload) {
      const root = document.getElementById('stats-content');
      root.innerHTML = '';
      if (!payload || !payload.records) {
        root.appendChild(el('div', {class:'empty'}, payload && payload.message || 'No closed trades yet.'));
        return;
      }
      const overview = el('div', {class:'panel'},
        el('h3', {}, 'Overview'),
        el('dl', {class:'kv'},
          el('dt', {}, 'Records'), el('dd', {}, String(payload.records)),
          el('dt', {}, 'Net PnL'), el('dd', {class: pnlClass(payload.net_total_pnl)}, fmtUsd.format(payload.net_total_pnl)),
          el('dt', {}, 'Closed PnL'), el('dd', {class: pnlClass(payload.closed_total_pnl)}, fmtUsd.format(payload.closed_total_pnl)),
          el('dt', {}, 'Open PnL'), el('dd', {class: pnlClass(payload.open_unrealized_pnl)}, fmtUsd.format(payload.open_unrealized_pnl)),
          el('dt', {}, 'Wins / Losses / Flats'), el('dd', {}, payload.wins+' / '+payload.losses+' / '+payload.flats),
          el('dt', {}, 'Win rate'), el('dd', {}, fmtPct(payload.win_rate)),
          el('dt', {}, 'Avg PnL'), el('dd', {class: pnlClass(payload.avg_pnl)}, fmtUsd.format(payload.avg_pnl)),
          el('dt', {title: 'Cumulative-PnL drawdown from the journal — does not include unrealized PnL of open positions.'},
            'Max drawdown'), el('dd', {class: pnlClass(payload.max_drawdown)}, fmtUsd.format(payload.max_drawdown)),
        ));
      root.appendChild(overview);

      const buckets = [
        ['By strategy', payload.by_strategy],
        ['By consensus', payload.by_consensus],
        ['By exit reason', payload.by_exit_reason],
        ['By entry price bucket', payload.by_entry_price_bucket],
      ];
      for (const [title, data] of buckets) {
        if (!data || !Object.keys(data).length) continue;
        root.appendChild(el('h2', {}, title));
        const table = el('table');
        table.appendChild(el('thead', {}, el('tr', {},
          el('th', {}, 'Bucket'), el('th', {class:'num'}, 'Count'),
          el('th', {class:'num'}, 'Total PnL'), el('th', {class:'num'}, 'Avg PnL'),
          el('th', {class:'num'}, 'Win rate'),
        )));
        const tbody = el('tbody');
        for (const [k, v] of Object.entries(data)) {
          tbody.appendChild(el('tr', {},
            td(k), td(String(v.count), 'num'),
            td(fmtUsd.format(v.total_pnl), 'num '+pnlClass(v.total_pnl)),
            td(fmtUsd.format(v.avg_pnl), 'num '+pnlClass(v.avg_pnl)),
            td(fmtPct(v.win_rate), 'num'),
          ));
        }
        table.appendChild(tbody);
        root.appendChild(table);
      }
    }

    function renderScanner(state) {
      const root = document.getElementById('scanner-content');
      const candidates = state.candidates || [];
      if (!candidates.length) {
        root.innerHTML = '';
        root.appendChild(el('div', {class:'empty'}, 'No soon markets in scan.'));
        return;
      }
      const table = el('table');
      table.appendChild(el('thead', {}, el('tr', {},
        el('th', {}, 'Market'), el('th', {}, 'Outcome'),
        el('th', {class:'num'}, 'Price'), el('th', {class:'num'}, 'Closes'),
        el('th', {class:'num'}, 'Liquidity'), el('th', {class:'num'}, 'Volume'),
        el('th', {class:'num'}, 'Score'),
      )));
      const tbody = el('tbody');
      for (const c of candidates) {
        const link = el('a', {href: c.url, target:'_blank', rel:'noreferrer'}, c.question || '?');
        tbody.appendChild(el('tr', {},
          td(link, 'market'), td(c.outcome),
          td(fmt2.format(c.price), 'num'),
          td(fmt2.format(c.hours_to_close)+'h', 'num'),
          td(fmtUsd.format(c.liquidity), 'num'),
          td(fmtUsd.format(c.volume), 'num'),
          td(fmt2.format(c.score), 'num'),
        ));
      }
      table.appendChild(tbody);
      root.innerHTML = '';
      root.appendChild(table);
    }

    function renderTune(payload) {
      const root = document.getElementById('tune-content');
      root.innerHTML = '';
      const status = !payload.enabled
        ? 'auto-tuner: disabled'
        : payload.records_observed >= payload.min_trades_required
          ? 'auto-tuner: enabled · '+payload.records_observed+' closed trades · ✓ active'
          : 'auto-tuner: paused ('+payload.records_observed+'/'+payload.min_trades_required+' trades)';
      root.appendChild(el('div', {class:'panel'}, el('h3', {}, 'Status'), document.createTextNode(status)));

      root.appendChild(el('h2', {}, 'Active overrides'));
      const overrides = payload.overrides_active || {};
      if (!Object.keys(overrides).length) {
        root.appendChild(el('div', {class:'empty'}, 'no overrides active — strategy running on env defaults.'));
      } else {
        const table = el('table');
        table.appendChild(el('thead', {}, el('tr', {},
          el('th', {}, 'Param'), el('th', {class:'num'}, 'Default'),
          el('th', {class:'num'}, 'Override'), el('th', {class:'num'}, 'Ratio'),
        )));
        const tbody = el('tbody');
        for (const [k, v] of Object.entries(overrides)) {
          const def = payload.defaults[k];
          const ratio = (typeof def === 'number' && def !== 0 && typeof v === 'number') ? (v/def).toFixed(2)+'x' : '—';
          tbody.appendChild(el('tr', {},
            td(k, 'mono'),
            td(def == null ? '—' : String(def), 'num'),
            td(String(v), 'num'),
            td(ratio, 'num'),
          ));
        }
        table.appendChild(tbody);
        root.appendChild(table);
      }

      root.appendChild(el('h2', {}, 'Suggestions'));
      const sugg = payload.suggestions || [];
      if (!sugg.length) {
        root.appendChild(el('div', {class:'empty'}, 'no suggestions.'));
      } else {
        const ul = el('ul');
        for (const s of sugg) {
          const text = (s.param ? '['+s.param+(s.ratio != null ? ' ×'+s.ratio:'')+'] ' : '') + (s.reason || '');
          ul.appendChild(el('li', {}, text));
        }
        root.appendChild(ul);
      }

      if (payload.mode) {
        root.appendChild(el('div', {style:'color:var(--muted);font-size:11px;margin-top:12px'},
          'mode: '+payload.mode+' · overrides file: '+payload.overrides_path));
      }
    }

    async function refreshState() {
      try {
        const state = await getJson('/api/state');
        PMBOT.state = state;
        renderTopbar(state);
        renderStatsBar(state);
        if (PMBOT.currentTab === 'positions') renderPositions(state);
        if (PMBOT.currentTab === 'closed') renderClosed(state);
        if (PMBOT.currentTab === 'scanner') renderScanner(state);
      } catch (e) {
        document.getElementById('updated').textContent = 'state error: '+e.message;
      }
    }

    async function refreshActiveTab() {
      try {
        if (PMBOT.currentTab === 'live') renderLive(await getJson('/api/live'));
        if (PMBOT.currentTab === 'stats') renderStats(await getJson('/api/stats'));
        if (PMBOT.currentTab === 'tune') renderTune(await getJson('/api/tune'));
        if (PMBOT.currentTab === 'positions' && PMBOT.state) renderPositions(PMBOT.state);
        if (PMBOT.currentTab === 'closed' && PMBOT.state) renderClosed(PMBOT.state);
        if (PMBOT.currentTab === 'scanner' && PMBOT.state) renderScanner(PMBOT.state);
      } catch (e) {}
      fillWinRate();
    }

    function setTab(tab) {
      PMBOT.currentTab = tab;
      localStorage.setItem('pmbot_dashboard_tab', tab);
      for (const btn of document.querySelectorAll('#tabs button')) {
        btn.classList.toggle('active', btn.dataset.tab === tab);
      }
      for (const sec of document.querySelectorAll('section.tab')) {
        sec.classList.toggle('active', sec.dataset.tab === tab);
      }
      refreshActiveTab();
      scheduleTabRefresh();
    }

    function scheduleTabRefresh() {
      if (PMBOT.timers.tab) clearInterval(PMBOT.timers.tab);
      const ms = PMBOT.refreshIntervalsMs[PMBOT.currentTab] || 5000;
      PMBOT.timers.tab = setInterval(refreshActiveTab, ms);
    }

    document.addEventListener('DOMContentLoaded', () => {
      for (const btn of document.querySelectorAll('#tabs button')) {
        btn.addEventListener('click', () => setTab(btn.dataset.tab));
      }
      setTab(PMBOT.currentTab);
      refreshState();
      updateClock();
      setInterval(updateClock, 1000);
      setInterval(refreshState, PMBOT.refreshIntervalsMs.state);
    });
  </script>
</body>
</html>
"""
