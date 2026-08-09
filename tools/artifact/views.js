/* ==========================================================================
   Dashboard and Backtest views. Both render snapshots taken from the running
   API, so every figure is one the service actually produced.
   ========================================================================== */

/* ============================================================== DASHBOARD */

function sparkline(values, up, width = 104, height = 26) {
  if (!values || values.length < 2) return el('div');
  const lo = Math.min(...values), hi = Math.max(...values);
  const span = hi - lo || 1;
  const step = width / (values.length - 1);
  const svg = svgEl('svg', { width, height, viewBox: `0 0 ${width} ${height}` });
  const d = values.map((v, i) =>
    `${i ? 'L' : 'M'}${(i * step).toFixed(1)},${(height - ((v - lo) / span) * height).toFixed(1)}`).join(' ');
  svg.appendChild(svgEl('path', { d, fill: 'none',
    stroke: up ? 'var(--status-good)' : 'var(--status-critical)',
    'stroke-width': 1.5, 'stroke-linejoin': 'round' }));
  return svg;
}

function renderDashboard() {
  const d = DATA.watchlist;
  const bars = DATA.bars;
  const page = el('div', 'page');

  const card = el('div', 'card');
  card.appendChild(el('div', 'card-head',
    `<h2>Watchlist</h2><span class="sub">${d.summary.tracked} tracked</span>`));

  const tiles = el('div', 'tiles');
  const tile = (k, v, n, cls = '') =>
    `<div class="tile"><div class="k">${k}</div><div class="v ${cls}">${v}</div><div class="n">${n}</div></div>`;
  tiles.innerHTML =
    tile('Names tracked', d.summary.tracked, 'all responding') +
    tile('Average IV rank', pctS(d.summary.average_iv_rank, 0), 'from stored daily history') +
    tile('Avg variance premium',
         (d.summary.average_variance_premium >= 0 ? '+' : '') +
           (d.summary.average_variance_premium * 100).toFixed(1),
         'implied − realized, points',
         d.summary.average_variance_premium >= 0 ? 'pos' : 'neg') +
    tile('Reporting soon', d.earnings_soon.filter((e) => e.days_away <= 14).length, 'within 14 days');
  card.appendChild(tiles);

  const scroll = el('div', 'scroll');
  scroll.style.maxHeight = '520px';
  const t = el('table', 'table');
  t.innerHTML = '<thead><tr><th>Symbol</th><th>Last</th><th>Change</th>' +
    '<th style="text-align:center">Trend</th><th>IV rank</th><th>ATM IV</th>' +
    '<th>Realized 20d</th><th>Variance prem.</th><th>Earnings</th><th></th></tr></thead>';
  const tb = el('tbody');

  for (const r of d.rows) {
    const up = (r.change_pct ?? 0) >= 0;
    const rank = r.iv_rank;
    const tr = el('tr');
    const cells = el('td', null, `<strong>${r.symbol}</strong>`);
    tr.appendChild(cells);
    tr.appendChild(el('td', null, r.last.toFixed(2)));
    tr.appendChild(el('td', up ? 'pos' : 'neg',
      `${up ? '+' : ''}${r.change_pct.toFixed(2)}%`));
    const sp = el('td');
    sp.style.textAlign = 'center';
    sp.appendChild(sparkline(bars[r.symbol].slice(-40), up));
    tr.appendChild(sp);
    tr.appendChild(el('td', null, rank == null ? '—' :
      `<div style="display:flex;align-items:center;gap:6px;justify-content:flex-end">` +
      `<span>${pctS(rank, 0)}</span><div class="meter" style="width:44px">` +
      `<div style="width:${Math.min(rank, 1) * 100}%;background:${rank > 0.5 ? 'var(--series-2)' : 'var(--series-1)'}"></div>` +
      `</div></div>`));
    tr.appendChild(el('td', null, pctS(r.atm_iv_30d, 1)));
    tr.appendChild(el('td', null, pctS(r.realized_vol_20d, 1)));
    tr.appendChild(el('td', (r.variance_risk_premium ?? 0) >= 0 ? 'pos' : 'neg',
      r.variance_risk_premium == null ? '—'
        : `${r.variance_risk_premium >= 0 ? '+' : ''}${(r.variance_risk_premium * 100).toFixed(1)}`));
    tr.appendChild(el('td', null, r.earnings
      ? `<span class="tiny" style="${r.earnings.days_away <= 7 ? 'color:var(--status-warning)' : ''}" title="${r.earnings.date} · ${r.earnings.confirmed ? 'confirmed' : 'projected'}">${r.earnings.days_away}d</span>`
      : '<span class="tiny muted">—</span>'));
    const open = el('td');
    const btn = el('button', 'btn ghost tiny', 'open');
    btn.style.padding = '2px 8px';
    btn.onclick = () => { state.symbol = r.symbol; state.view = 'lab'; render(); };
    open.appendChild(btn);
    tr.appendChild(open);
    tb.appendChild(tr);
  }
  t.appendChild(tb);
  scroll.appendChild(t);
  card.appendChild(scroll);
  page.appendChild(card);

  const grid = el('div', 'grid-2');

  const short = el('div', 'card');
  short.appendChild(el('div', 'card-head',
    '<h2>Short-premium shortlist</h2><span class="sub">IV rank ≥ 45% and options above realized</span>'));
  if (d.premium_candidates.length) {
    const st = el('table', 'table');
    st.innerHTML = '<thead><tr><th>Symbol</th><th>IV rank</th><th>ATM IV</th><th>Var. premium</th><th>Earnings</th></tr></thead>';
    const stb = el('tbody');
    for (const c of d.premium_candidates) {
      stb.appendChild(el('tr', null,
        `<td><strong>${c.symbol}</strong></td><td>${pctS(c.iv_rank, 0)}</td>` +
        `<td>${pctS(c.atm_iv_30d, 1)}</td>` +
        `<td class="pos">+${((c.variance_risk_premium ?? 0) * 100).toFixed(1)}</td>` +
        `<td class="tiny muted">${c.earnings_days != null ? c.earnings_days + 'd' : '—'}</td>`));
    }
    st.appendChild(stb);
    short.appendChild(st);
  } else {
    short.appendChild(el('div', 'card-body muted tiny',
      'Nothing on this list currently has implied volatility both elevated in its own range and above what the stock has recently delivered. That is a normal reading, not an error.'));
  }
  grid.appendChild(short);

  const earn = el('div', 'card');
  earn.appendChild(el('div', 'card-head',
    '<h2>Earnings calendar</h2><span class="sub">next report per name</span>'));
  const et = el('table', 'table');
  et.innerHTML = '<thead><tr><th>Symbol</th><th>Date</th><th>In</th><th>Session</th><th>Status</th></tr></thead>';
  const etb = el('tbody');
  for (const e of d.earnings_soon) {
    etb.appendChild(el('tr', null,
      `<td><strong>${e.symbol}</strong></td><td>${e.date}</td>` +
      `<td class="${e.days_away <= 7 ? 'neg' : ''}">${e.days_away}d</td>` +
      `<td class="tiny muted">${e.session === 'bmo' ? 'before open' : e.session === 'amc' ? 'after close' : e.session}</td>` +
      `<td class="tiny" style="${e.confirmed ? '' : 'color:var(--status-warning)'}">${e.confirmed ? 'confirmed' : 'projected'}</td>`));
  }
  et.appendChild(etb);
  earn.appendChild(et);
  grid.appendChild(earn);
  page.appendChild(grid);

  const note = el('div', 'card');
  note.appendChild(el('div', 'card-head', '<h2>Portfolio risk</h2><span class="sub">no positions tracked</span>'));
  note.appendChild(el('div', 'card-body muted tiny',
    'In the running application this panel aggregates greeks, beta-weighted delta and concentration warnings across open positions, persisted to Postgres. Nothing is tracked in this static preview.'));
  page.appendChild(note);

  if (DATA.board) page.appendChild(renderBoard(DATA.board));
  if (DATA.news) page.appendChild(renderImpactFeed(DATA.news));
  if (DATA.tv_status) page.appendChild(renderTvStatus(DATA.tv_status));

  return page;
}


/* ========================================================== MARKET BOARD */

const BAND_COLOR = {
  high: 'var(--status-critical)',
  elevated: 'var(--status-serious)',
  moderate: 'var(--status-warning)',
  low: 'var(--text-muted)',
  unscored: 'var(--axis)',
};

function moveCell(v) {
  if (v == null) return '<td class="muted">—</td>';
  return `<td class="${v >= 0 ? 'pos' : 'neg'}">${pctS(v, 2)}</td>`;
}

function renderBoard(b) {
  const card = el('div', 'card');
  card.appendChild(el('div', 'card-head',
    `<div><h2>Market board</h2><span class="sub">${b.breadth.advancing} of ` +
    `${b.breadth.instruments} advancing · benchmark ${b.benchmark}</span></div>` +
    `<span class="chip">${b.futures_available ? 'futures' : 'ETF proxies for futures'}</span>`));

  const body = el('div', 'card-body flush');
  for (const seg of b.segments) {
    body.appendChild(el('div', 'seg-head', seg.name));
    const t = el('table', 'table');
    t.innerHTML =
      '<thead><tr><th>Instrument</th><th>Last</th><th>Day</th><th>1W</th><th>1M</th>' +
      `<th>3M</th><th>vs ${b.benchmark}</th><th>RV 20d</th><th>From 52w high</th>` +
      '<th style="width:118px">60 sessions</th></tr></thead><tbody>' +
      seg.rows.map((r) =>
        '<tr><td><span class="inst">' +
        `<span class="inst-label">${r.label}</span>` +
        `<span class="inst-sym">${r.symbol}` +
        // The proxy marker goes on the row, not in a footnote: an ETF standing
        // in for a futures contract is most wrong out of hours, which is
        // exactly when a futures board gets opened.
        (r.is_proxy
          ? `<span class="proxy-tag" title="No futures feed is configured, so this row quotes ${r.symbol} in place of ${r.proxy_for}.">≈ ${r.proxy_for}</span>`
          : '') +
        '</span></span></td>' +
        `<td>${r.last != null ? numS(r.last) : '—'}</td>` +
        moveCell(r.change_1d) + moveCell(r.change_1w) + moveCell(r.change_1m) +
        moveCell(r.change_3m) + moveCell(r.rs_1m_vs_benchmark) +
        `<td>${r.realized_vol_20d != null ? pctS(r.realized_vol_20d, 0) : '—'}</td>` +
        `<td class="${(r.pct_from_52w_high ?? 0) < -0.1 ? 'neg' : ''}">` +
        `${r.pct_from_52w_high != null ? pctS(r.pct_from_52w_high, 1) : '—'}</td>` +
        '<td class="spark"></td></tr>').join('') +
      '</tbody></table>';
    body.appendChild(t);

    // Sparklines are SVG nodes, so they are attached after the row HTML rather
    // than serialised into it.
    t.querySelectorAll('td.spark').forEach((cell, i) => {
      cell.style.textAlign = 'right';
      cell.appendChild(sparkline(seg.rows[i].sparkline));
    });
  }
  card.appendChild(body);
  card.appendChild(el('div', 'card-body tiny muted', b.provenance));
  return card;
}

function renderImpactFeed(n) {
  const card = el('div', 'card');
  const scored = n.items.filter((i) => i.scoring.impact != null).length;
  const chips = ['high', 'elevated', 'moderate', 'low']
    .filter((b) => n.bands[b])
    .map((b) => `<span class="chip"><span class="swatch" style="background:${BAND_COLOR[b]}"></span>${n.bands[b]} ${b}</span>`)
    .join('');
  card.appendChild(el('div', 'card-head',
    `<div><h2>Watchlist news by impact</h2><span class="sub">${scored} of ${n.count} scored</span></div>` +
    `<div class="row" style="gap:6px">${chips}</div>`));

  const body = el('div', 'card-body flush');
  const scroll = el('div', 'scroll');
  scroll.style.maxHeight = '460px';
  scroll.innerHTML = n.items.map((item) => {
    const s = item.scoring, c = s.components || {};
    const meta = s.impact != null
      ? `<span class="${s.direction === 'down' ? 'neg' : 'pos'}">` +
        `${c.realized_move_pct != null ? (c.realized_move_pct > 0 ? '+' : '') + c.realized_move_pct.toFixed(2) + '%' : '—'}</span>` +
        `<span>${c.move_sigma != null ? c.move_sigma.toFixed(1) : '—'}σ move</span>` +
        (c.volume_vs_median != null ? `<span>${c.volume_vs_median.toFixed(1)}× volume</span>` : '') +
        ((c.coverage_items_24h || 0) > 1 ? `<span>${c.coverage_items_24h} items</span>` : '') +
        (c.days_to_earnings != null && Math.abs(c.days_to_earnings) <= 10
          ? `<span class="warn">earnings in ${c.days_to_earnings}d</span>` : '') +
        `<span class="muted">measured ${s.window ? s.window.prior_close + ' → ' + s.window.reaction_close : ''}</span>`
      : `<span class="muted">${s.reason || 'not yet measurable'}</span>`;
    return '<div class="news-row"><div class="news-impact">' +
      `<div class="news-meter"><div style="width:${s.impact != null ? s.impact : 0}%;background:${BAND_COLOR[s.band]}"></div></div>` +
      '<div class="news-score">' +
      (s.impact != null ? `<strong>${s.impact.toFixed(0)}</strong><span class="band">${s.band}</span>`
                        : '<span class="band">unscored</span>') +
      '</div></div><div class="news-body"><div class="news-title">' +
      `<span class="chip">${item.symbol}</span>` +
      (item.media_type === 'video' ? '<span class="chip">video</span>' : '') +
      `<span>${item.title}</span></div>` +
      `<div class="news-meta">${meta}</div></div></div>`;
  }).join('');
  body.appendChild(scroll);
  card.appendChild(body);
  card.appendChild(el('div', 'card-body tiny muted',
    `<p style="margin:0 0 6px">${n.method}</p><p style="margin:0">${n.limits}</p>`));
  return card;
}

function renderTvStatus(tv) {
  const card = el('div', 'card');
  card.appendChild(el('div', 'card-head',
    '<div><h2>TradingView integration</h2><span class="sub">what is wired, and what each ' +
    'tier would take</span></div>'));
  const body = el('div', 'card-body flush');
  for (const key of ['widget', 'charting_library', 'broker_integration']) {
    const t = tv[key];
    const tier = el('div', 'tier');
    tier.innerHTML =
      '<div class="tier-head">' +
      `<span class="dot ${t.live ? 'on' : 'off'}"></span>` +
      `<span class="tier-name">${t.tier}</span>` +
      `<span class="chip ${t.live ? 'accent' : ''}">${t.live ? 'live' : 'not wired'}</span>` +
      `<span class="chip">${t.trading ? 'order entry' : 'charts only'}</span></div>` +
      '<div class="tier-body">' +
      `<div class="kv"><span>Requires</span><strong>${t.requires}</strong></div>` +
      (t.data ? `<div class="kv"><span>Data source</span><strong>${t.data}</strong></div>` : '') +
      (t.datafeed_ready
        ? `<div class="kv"><span>Datafeed</span><strong class="pos">implemented at <code>${t.datafeed_url}</code></strong></div>`
        : '') +
      `<p class="tiny muted" style="margin:6px 0 0">${t.note}</p>` +
      (t.blocked_on ? `<ul class="blocked">${t.blocked_on.map((b) => `<li>${b}</li>`).join('')}</ul>` : '') +
      '</div>';
    body.appendChild(tier);
  }
  card.appendChild(body);
  card.appendChild(el('div', 'card-body tiny muted',
    tv.summary + ' The live chart itself is not in this static preview — the widget is ' +
    'fetched from TradingView at runtime, so a single-file document cannot carry it.'));
  return card;
}

/* =============================================================== BACKTEST */

function renderBacktest() {
  const bt = DATA.backtests[state.backtest];
  const m = bt.metrics;
  const page = el('div', 'page');

  const switcher = el('div', 'card');
  const sh = el('div', 'card-head');
  const tabs = el('div', 'tabs');
  DATA.backtests.forEach((b, i) => {
    const btn = el('button', 'tab', b.label);
    btn.setAttribute('aria-selected', String(i === state.backtest));
    btn.onclick = () => { state.backtest = i; render(); };
    tabs.appendChild(btn);
  });
  sh.appendChild(tabs);
  sh.appendChild(el('span', 'sub',
    `${bt.symbol} · ${bt.start} → ${bt.end} · ${bt.trading_days} trading days`));
  switcher.appendChild(sh);
  page.appendChild(switcher);

  const banner = el('div', 'banner');
  banner.innerHTML =
    `<span aria-hidden>▲</span><span><strong>Modelled prices — rules test, not a P&amp;L record.</strong> ${bt.caveat}</span>`;
  page.appendChild(banner);

  for (const w of bt.warnings || []) page.appendChild(el('div', 'err', w));

  const head = el('div', 'card');
  head.appendChild(el('div', 'card-head',
    `<div><h2>${bt.template_name} · ${bt.symbol}</h2>` +
    `<span class="sub">${bt.rules.target_dte} DTE entry every ${bt.rules.entry_every_days} days · ` +
    `${(bt.rules.profit_target_pct * 100).toFixed(0)}% profit target · ${bt.rules.stop_loss_pct}× stop · ` +
    `close at ${bt.rules.close_at_dte} DTE</span></div>`));
  const tiles = el('div', 'tiles');
  const tile = (k, v, n, cls = '') =>
    `<div class="tile"><div class="k">${k}</div><div class="v ${cls}">${v}</div><div class="n">${n}</div></div>`;
  tiles.innerHTML =
    tile('Total P/L', money(m.total_pl), `${m.trades} trades`, m.total_pl >= 0 ? 'pos' : 'neg') +
    tile('Win rate', pctS(m.win_rate, 0), `${m.wins}W / ${m.losses}L`) +
    tile('Profit factor', numS(m.profit_factor), 'gross win / gross loss') +
    tile('Expectancy', money(m.expectancy), 'per trade', m.expectancy >= 0 ? 'pos' : 'neg') +
    tile('Max drawdown', money(m.max_drawdown), 'on the equity curve', 'neg') +
    tile('Sharpe', numS(m.sharpe), 'annualized, daily equity') +
    tile('Avg hold', `${m.average_days_held}d`, `${pctS(m.exposure_pct, 0)} exposed`) +
    tile('Worst streak', m.max_consecutive_losses, 'consecutive losses');
  head.appendChild(tiles);
  page.appendChild(head);

  const eq = el('div', 'card');
  eq.appendChild(el('div', 'card-head',
    '<h2>Equity and drawdown</h2><span class="sub">realized plus open positions, marked daily</span>'));
  const eb = el('div', 'card-body flush');
  eb.appendChild(equityCurve(bt.equity_curve));
  const dd = el('div');
  dd.style.borderTop = '1px solid var(--border)';
  dd.appendChild(drawdownChart(bt.equity_curve));
  eb.appendChild(dd);
  eq.appendChild(eb);
  page.appendChild(eq);

  const dist = el('div', 'card');
  dist.appendChild(el('div', 'card-head',
    `<h2>Trade distribution</h2><span class="sub">largest win ${money(m.largest_win)} · largest loss ${money(m.largest_loss)}</span>`));
  const db = el('div', 'card-body flush');
  db.appendChild(histogram(bt.distribution));
  dist.appendChild(db);
  page.appendChild(dist);

  const led = el('div', 'card');
  led.appendChild(el('div', 'card-head',
    `<h2>Trade ledger</h2><span class="sub">${bt.trades.length} closed</span>`));
  const lb = el('div', 'card-body flush');
  const scroll = el('div', 'scroll');
  scroll.style.maxHeight = '380px';
  const t = el('table', 'table');
  t.innerHTML = '<thead><tr><th>Entry</th><th>Exit</th><th>Held</th><th>Spot in</th><th>Spot out</th>' +
    '<th>Basis</th><th>P/L</th><th>Worst</th><th style="text-align:left;padding-left:14px">Reason</th></tr></thead>';
  const tb = el('tbody');
  for (const tr of [...bt.trades].reverse()) {
    tb.appendChild(el('tr', null,
      `<td>${tr.a}</td><td>${tr.b}</td><td>${tr.h}d</td>` +
      `<td>${tr.si.toFixed(2)}</td><td>${tr.so.toFixed(2)}</td>` +
      `<td class="${tr.ba < 0 ? 'pos' : ''}">${tr.ba < 0 ? '+' + money(-tr.ba) : '-' + money(tr.ba)}</td>` +
      `<td class="${tr.pl >= 0 ? 'pos' : 'neg'}">${money(tr.pl)}</td>` +
      `<td class="neg tiny">${money(tr.mae)}</td>` +
      `<td class="tiny muted" style="text-align:left;padding-left:14px">${tr.why}</td>`));
  }
  t.appendChild(tb);
  scroll.appendChild(t);
  lb.appendChild(scroll);
  led.appendChild(lb);
  page.appendChild(led);

  return page;
}

const BW = 900, BPAD = { top: 14, right: 62, bottom: 26, left: 66 };

function equityCurve(points, height = 240) {
  const values = points.map((p) => p.e);
  let lo = Math.min(...values, 0), hi = Math.max(...values, 0);
  const pad = Math.max((hi - lo) * 0.1, 1);
  lo -= pad; hi += pad;
  const iw = BW - BPAD.left - BPAD.right, ih = height - BPAD.top - BPAD.bottom;
  const sx = (i) => BPAD.left + (i / (points.length - 1)) * iw;
  const sy = (v) => BPAD.top + (1 - (v - lo) / (hi - lo)) * ih;

  const wrap = el('div', 'chart-wrap');
  const svg = svgEl('svg', { viewBox: `0 0 ${BW} ${height}`, role: 'img',
    'aria-label': 'Backtest equity curve' });

  for (const t of ticks(lo, hi, 5)) {
    svg.appendChild(svgEl('line', { class: 'grid-line', x1: BPAD.left, x2: BW - BPAD.right, y1: sy(t), y2: sy(t) }));
    const tx = svgEl('text', { class: 'axis-label', x: BPAD.left - 8, y: sy(t) + 3.5, 'text-anchor': 'end' });
    tx.textContent = money(t);
    svg.appendChild(tx);
  }

  const line = points.map((p, i) => `${i ? 'L' : 'M'}${sx(i).toFixed(1)},${sy(p.e).toFixed(1)}`).join(' ');
  svg.appendChild(svgEl('path', {
    d: `${line} L${sx(points.length - 1).toFixed(1)},${sy(0).toFixed(1)} L${sx(0).toFixed(1)},${sy(0).toFixed(1)} Z`,
    fill: 'var(--series-1)', opacity: 0.12 }));
  svg.appendChild(svgEl('line', { x1: BPAD.left, x2: BW - BPAD.right, y1: sy(0), y2: sy(0),
    stroke: 'var(--axis)', 'stroke-width': 1.5 }));
  svg.appendChild(svgEl('path', { d: line, fill: 'none', stroke: 'var(--series-1)',
    'stroke-width': 2, 'stroke-linejoin': 'round' }));

  svg.appendChild(svgEl('line', { class: 'axis-line', x1: BPAD.left, x2: BW - BPAD.right,
    y1: height - BPAD.bottom, y2: height - BPAD.bottom }));
  const every = Math.max(1, Math.floor(points.length / 6));
  points.forEach((p, i) => {
    if (i % every) return;
    const tx = svgEl('text', { class: 'axis-label', x: sx(i), y: height - BPAD.bottom + 15, 'text-anchor': 'middle' });
    tx.textContent = p.d.slice(2, 7);
    svg.appendChild(tx);
  });

  const cross = svgEl('g', { 'pointer-events': 'none', style: 'display:none' });
  const cl = svgEl('line', { stroke: 'var(--text-secondary)', 'stroke-width': 1, y1: BPAD.top, y2: height - BPAD.bottom });
  const dot = svgEl('circle', { r: 4, fill: 'var(--series-1)', stroke: 'var(--surface-1)', 'stroke-width': 2 });
  cross.append(cl, dot);
  svg.appendChild(cross);

  const tip = el('div', 'tooltip');
  tip.style.display = 'none';
  wrap.append(svg, tip);

  svg.addEventListener('mousemove', (e) => {
    const r = svg.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * BW;
    const i = Math.round(((px - BPAD.left) / iw) * (points.length - 1));
    if (i < 0 || i >= points.length) return;
    const p = points[i];
    cross.style.display = '';
    cl.setAttribute('x1', sx(i)); cl.setAttribute('x2', sx(i));
    dot.setAttribute('cx', sx(i)); dot.setAttribute('cy', sy(p.e));
    tip.style.display = '';
    tip.style.top = '8px';
    if (i > points.length / 2) { tip.style.left = '16px'; tip.style.right = ''; }
    else { tip.style.right = '74px'; tip.style.left = ''; }
    tip.innerHTML = `<div class="tt-head">${p.d}</div>` +
      `<div class="tt-row"><span>Equity</span><strong class="${p.e >= 0 ? 'pos' : 'neg'}">${money(p.e)}</strong></div>` +
      `<div class="tt-row"><span>Realized</span><span>${money(p.r)}</span></div>` +
      `<div class="tt-row"><span>Open</span><span>${money(p.u)}</span></div>` +
      `<div class="tt-row"><span>Underlying</span><span>${p.s.toFixed(2)}</span></div>` +
      `<div class="tt-row"><span>Positions</span><span>${p.n}</span></div>`;
  });
  svg.addEventListener('mouseleave', () => { cross.style.display = 'none'; tip.style.display = 'none'; });
  return wrap;
}

function drawdownChart(points, height = 110) {
  let peak = 0;
  const series = points.map((p) => { peak = Math.max(peak, p.e); return p.e - peak; });
  const lo = Math.min(...series, -1);
  const iw = BW - BPAD.left - BPAD.right, ih = height - BPAD.top - 16;
  const sx = (i) => BPAD.left + (i / (series.length - 1)) * iw;
  const sy = (v) => BPAD.top + (v / lo) * ih;

  const wrap = el('div', 'chart-wrap');
  const svg = svgEl('svg', { viewBox: `0 0 ${BW} ${height}`, role: 'img',
    'aria-label': 'Drawdown from peak equity' });
  svg.appendChild(svgEl('line', { class: 'grid-line', x1: BPAD.left, x2: BW - BPAD.right, y1: sy(lo), y2: sy(lo) }));
  for (const [v, y] of [[lo, sy(lo)], [0, BPAD.top]]) {
    const t = svgEl('text', { class: 'axis-label', x: BPAD.left - 8, y: y + 3.5, 'text-anchor': 'end' });
    t.textContent = money(v);
    svg.appendChild(t);
  }
  const line = series.map((v, i) => `${i ? 'L' : 'M'}${sx(i).toFixed(1)},${sy(v).toFixed(1)}`).join(' ');
  svg.appendChild(svgEl('path', {
    d: `${line} L${sx(series.length - 1).toFixed(1)},${BPAD.top} L${sx(0).toFixed(1)},${BPAD.top} Z`,
    fill: 'var(--status-critical)', opacity: 0.28 }));
  svg.appendChild(svgEl('path', { d: line, fill: 'none', stroke: 'var(--status-critical)', 'stroke-width': 1.5 }));
  svg.appendChild(svgEl('line', { x1: BPAD.left, x2: BW - BPAD.right, y1: BPAD.top, y2: BPAD.top,
    stroke: 'var(--axis)', 'stroke-width': 1 }));
  wrap.appendChild(svg);
  return wrap;
}

function histogram(dist, height = 180) {
  const { edges, counts } = dist;
  const maxCount = Math.max(...counts, 1);
  const iw = BW - BPAD.left - BPAD.right, ih = height - BPAD.top - BPAD.bottom;
  const bw = iw / counts.length;

  // A managed position produces a genuine point mass -- every path that hits
  // the profit target exits at the same P/L -- so one bucket can hold most of
  // the distribution and a linear count axis flattens the losses to nothing.
  // Those losses are the part that decides survivability, so when one bucket
  // dwarfs the rest the count axis switches to a square root scale. Ticks keep
  // true counts and the legend says what the axis is doing: a rescaled axis
  // presented as linear would be a lie about magnitude.
  const nonZero = counts.filter((c) => c > 0).sort((a, b) => a - b);
  const typical = nonZero.length ? nonZero[Math.floor(nonZero.length / 2)] : 1;
  const compress = maxCount > typical * 20;
  const scaleC = (c) => (compress ? Math.sqrt(c) / Math.sqrt(maxCount) : c / maxCount);

  const wrap = el('div', 'chart-wrap');
  const svg = svgEl('svg', { viewBox: `0 0 ${BW} ${height}`, role: 'img',
    'aria-label': 'Distribution of trade profit and loss' });

  for (const t of ticks(0, maxCount, 4)) {
    const y = BPAD.top + ih - scaleC(t) * ih;
    svg.appendChild(svgEl('line', { class: 'grid-line', x1: BPAD.left, x2: BW - BPAD.right, y1: y, y2: y }));
    const tx = svgEl('text', { class: 'axis-label', x: BPAD.left - 8, y: y + 3.5, 'text-anchor': 'end' });
    tx.textContent = t.toFixed(0);
    svg.appendChild(tx);
  }

  const tip = el('div', 'tooltip');
  tip.style.display = 'none';

  counts.forEach((c, i) => {
    const h = scaleC(c) * ih;
    const mid = (edges[i] + edges[i + 1]) / 2;
    const rect = svgEl('rect', {
      x: BPAD.left + i * bw + 1, y: BPAD.top + ih - h,
      width: Math.max(bw - 2, 1), height: Math.max(h, c > 0 ? 2 : 0), rx: 2,
      fill: mid >= 0 ? 'var(--status-good)' : 'var(--status-critical)', opacity: 0.8 });
    rect.addEventListener('mouseenter', () => {
      tip.style.display = ''; tip.style.right = '16px'; tip.style.top = '8px';
      tip.innerHTML = `<div class="tt-head">${money(edges[i])} to ${money(edges[i + 1])}</div>` +
        `<div class="tt-row"><span>Trades</span><strong>${c}</strong></div>`;
    });
    svg.appendChild(rect);
  });

  svg.appendChild(svgEl('line', { class: 'axis-line', x1: BPAD.left, x2: BW - BPAD.right,
    y1: BPAD.top + ih, y2: BPAD.top + ih }));
  const every = Math.max(1, Math.floor(counts.length / 6));
  counts.forEach((_, i) => {
    if (i % every) return;
    const t = svgEl('text', { class: 'axis-label', x: BPAD.left + i * bw + bw / 2,
      y: height - BPAD.bottom + 15, 'text-anchor': 'middle' });
    t.textContent = money(edges[i]);
    svg.appendChild(t);
  });
  const ax = svgEl('text', { class: 'axis-label', x: (BPAD.left + BW - BPAD.right) / 2,
    y: height - 3, 'text-anchor': 'middle' });
  ax.textContent = 'profit / loss per trade';
  svg.appendChild(ax);

  wrap.append(svg, tip);
  wrap.onmouseleave = () => { tip.style.display = 'none'; };
  return wrap;
}


/* ================================================================ THEMES */
/*
 * Same four themes as the web app, driven by the same `data-theme` attribute
 * and the same validated tokens (tools/palette_check.py runs against this
 * stylesheet too).
 *
 * The choice is held in memory rather than localStorage on purpose: this file
 * is delivered as a standalone artifact and browser storage is not available
 * to it. The cost is that the theme resets on reload, which is the honest
 * trade for a single self-contained document.
 */

const THEMES = [
  ['dark-blue', 'Dark blue', 'Near-black with a blue cast. The default.'],
  ['light-black', 'Light black', 'Neutral dark grey, a step lighter. Easier in a bright room.'],
  ['eggshell', 'Eggshell', 'Warm off-white. Reads as paper rather than as a screen.'],
  ['glass', 'Liquid glass', 'Translucent panels over a fixed gradient.'],
];

function themeSwatch(id) {
  // Reads the theme's REAL tokens by probing a detached element carrying the
  // attribute. A hard-coded swatch is a copy of the palette, and a copy of the
  // palette is a swatch that goes stale the first time a colour changes.
  const probe = el('div');
  probe.setAttribute('data-theme', id);
  probe.style.cssText = 'position:absolute;visibility:hidden;pointer-events:none';
  document.body.appendChild(probe);
  const cs = getComputedStyle(probe);
  const cols = ['--surface-1', '--series-1', '--series-2', '--status-good', '--status-critical']
    .map((t) => cs.getPropertyValue(t).trim());
  probe.remove();

  const sw = el('span', 'theme-swatch');
  sw.setAttribute('data-theme', id);
  sw.innerHTML = cols.map((c) => `<span style="background:${c}"></span>`).join('');
  return sw;
}

function settingsControl() {
  const wrap = el('div', 'settings');
  const btn = el('button', 'btn ghost',
    '<svg width="14" height="14" viewBox="0 0 24 24" aria-hidden fill="none" ' +
    'stroke="currentColor" stroke-width="2" stroke-linecap="round">' +
    '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1.08-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9c.2.61.78 1.03 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>' +
    '<span style="margin-left:6px">Settings</span>');
  btn.style.display = 'inline-flex';
  btn.style.alignItems = 'center';
  btn.setAttribute('aria-haspopup', 'dialog');
  btn.setAttribute('aria-expanded', String(!!state.settingsOpen));
  btn.onclick = (e) => { e.stopPropagation(); state.settingsOpen = !state.settingsOpen; render(); };
  wrap.appendChild(btn);

  if (state.settingsOpen) {
    const pop = el('div', 'popover');
    pop.setAttribute('role', 'dialog');
    pop.setAttribute('aria-label', 'Settings');
    pop.onclick = (e) => e.stopPropagation();
    pop.appendChild(el('div', 'popover-head', 'Theme'));

    const grid = el('div', 'theme-grid');
    for (const [id, label, blurb] of THEMES) {
      const opt = el('button', 'theme-option');
      opt.setAttribute('aria-pressed', String(state.theme === id));
      opt.appendChild(themeSwatch(id));
      opt.appendChild(el('span', 'theme-meta',
        `<span class="theme-name">${label}</span><span class="theme-blurb">${blurb}</span>`));
      opt.onclick = () => {
        state.theme = id;
        document.documentElement.setAttribute('data-theme', id);
        render();
      };
      grid.appendChild(opt);
    }
    pop.appendChild(grid);
    pop.appendChild(el('div', 'popover-foot',
      'Every theme is validated for contrast and colour-vision separation by ' +
      '<code>tools/palette_check.py</code>. Profit and loss differ in lightness as ' +
      'well as hue, so the pair survives red-green colour blindness.'));
    wrap.appendChild(pop);
  }
  return wrap;
}


/* ========================================================== CANDLE CHART */
/*
 * The same chart the app draws from /api/tv/history. Here it reads the bundled
 * bars, which are the identical series the backtests and forecasts in this file
 * were computed from -- so the chart and the numbers cannot disagree.
 */

function candleChart(bars, symbol, height = 380) {
  const W = 1000, PAD = { top: 14, right: 62, bottom: 22, left: 34 }, VOL_H = 54;
  if (!bars || bars.length < 2) return el('div', 'skeleton');
  const lo = Math.min(...bars.map((b) => b.l)), hi = Math.max(...bars.map((b) => b.h));
  const pad = (hi - lo) * 0.06 || 1, yLo = lo - pad, yHi = hi + pad;
  const priceH = height - PAD.top - PAD.bottom - VOL_H;
  const step = (W - PAD.left - PAD.right) / bars.length;
  const bw = Math.max(1, Math.min(11, step * 0.68));
  const x = (i) => PAD.left + i * step + step / 2;
  const y = (v) => PAD.top + priceH - ((v - yLo) / (yHi - yLo)) * priceH;
  const maxVol = Math.max(...bars.map((b) => b.v), 1);
  const volTop = PAD.top + priceH + 12;

  const wrap = el('div', 'chart-wrap');
  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${height}`, role: 'img',
    'aria-label': `${symbol} daily candles with volume` });

  for (const t of ticks(yLo, yHi, 5)) {
    svg.appendChild(svgEl('line', { class: 'grid-line', x1: PAD.left, x2: W - PAD.right, y1: y(t), y2: y(t) }));
    const lab = svgEl('text', { class: 'axis-label', x: W - PAD.right + 6, y: y(t) + 3.5 });
    lab.textContent = numS(t); svg.appendChild(lab);
  }
  bars.forEach((b, i) => {
    const col = b.c >= b.o ? 'var(--status-good)' : 'var(--status-critical)';
    svg.appendChild(svgEl('line', { x1: x(i), x2: x(i), y1: y(b.h), y2: y(b.l), stroke: col, 'stroke-width': 1 }));
    svg.appendChild(svgEl('rect', { x: x(i) - bw / 2, y: Math.min(y(b.o), y(b.c)),
      width: bw, height: Math.max(1, Math.abs(y(b.c) - y(b.o))), fill: col }));
    const vh = (b.v / maxVol) * (VOL_H - 8);
    svg.appendChild(svgEl('rect', { x: x(i) - bw / 2, y: volTop + (VOL_H - 8) - vh,
      width: bw, height: vh, fill: col, opacity: 0.32 }));
  });
  svg.appendChild(svgEl('line', { class: 'axis-line', x1: PAD.left, x2: W - PAD.right,
    y1: PAD.top + priceH, y2: PAD.top + priceH }));
  const stride = Math.max(1, Math.floor(bars.length / 7));
  bars.forEach((b, i) => {
    if (i % stride) return;
    const lab = svgEl('text', { class: 'axis-label', x: x(i), y: height - 6, 'text-anchor': 'middle' });
    lab.textContent = b.d || ''; svg.appendChild(lab);
  });
  wrap.appendChild(svg);
  return wrap;
}

/*
 * TradingView embeds inside the standalone document.
 *
 * The rest of this file is a snapshot: everything it shows was computed at
 * build time and frozen. These widgets are the exception — they are fetched
 * live from TradingView's CDN when the file is opened, so this one panel shows
 * real market data even though the document itself is offline. That difference
 * is stated on the page, because a live chart sitting above generated numbers
 * is exactly the kind of thing someone would otherwise read across.
 *
 * Mirrors components/TradingView.tsx: same three states, same six-second
 * iframe poll, same reason — a blank rectangle is indistinguishable from a
 * broken page.
 */
function tvWidget(kind, config, height, label) {
  const wrap = el('div');
  wrap.style.cssText = `position:relative;height:${height}px`;
  const host = el('div');
  host.style.height = '100%';
  const overlay = el('div', 'tv-overlay');
  const short = height < 140;
  overlay.innerHTML = short
    ? `<span class="tiny muted"><span class="pulse-dot"></span> loading ${label}…</span>`
    : `<span class="tiny muted"><span class="pulse-dot"></span> loading ${label} from s3.tradingview.com…</span>`;
  wrap.appendChild(host);
  wrap.appendChild(overlay);

  const container = el('div', 'tradingview-widget-container');
  container.style.height = '100%';
  const inner = el('div', 'tradingview-widget-container__widget');
  inner.style.height = '100%';
  container.appendChild(inner);

  const theme = document.documentElement.getAttribute('data-theme') === 'eggshell'
    ? 'light' : 'dark';
  const script = document.createElement('script');
  script.src = 'https://s3.tradingview.com/external-embedding/' + kind;
  script.async = true;
  script.type = 'text/javascript';
  script.innerHTML = JSON.stringify(Object.assign({}, config, { theme, locale: 'en' }));

  const fail = () => {
    overlay.innerHTML = short
      ? `<span class="tiny muted">${label} did not load — <code>s3.tradingview.com</code> is unreachable.</span>`
      : `<div class="empty-state"><strong>${label} did not load.</strong><p>It is fetched from ` +
        `<code>s3.tradingview.com</code> when this file is opened, so it needs a context that ` +
        `allows third-party scripts. If you are reading this inside a chat preview pane, that ` +
        `is the cause — previews block external scripts. <b>Download this file and open it in ` +
        `your browser</b> and the live TradingView panels render. Otherwise: an ad blocker, a ` +
        `network policy, or being offline. Every other panel here is a build-time snapshot ` +
        `and keeps working.</p></div>`;
  };
  let settled = false;
  const poll = setInterval(() => {
    if (settled) return;
    if (inner.querySelector('iframe')) { settled = true; clearInterval(poll); overlay.remove(); }
  }, 250);
  setTimeout(() => {
    if (settled) return;
    settled = true; clearInterval(poll);
    if (inner.querySelector('iframe')) overlay.remove(); else fail();
  }, 6000);
  script.onerror = () => { settled = true; clearInterval(poll); fail(); };

  container.appendChild(script);
  host.appendChild(container);
  return wrap;
}


/* Plain ticker -> a TradingView symbol. The bundle carries US equities and two
 * ETFs, so the exchange is derivable here in a way it is not in general. */
const TV_EXCHANGE = {
  SPY: 'AMEX', QQQ: 'NASDAQ', NVDA: 'NASDAQ', AAPL: 'NASDAQ',
  TSLA: 'NASDAQ', AMD: 'NASDAQ', META: 'NASDAQ', COIN: 'NASDAQ',
};
function tvSymbol(s) { return `${TV_EXCHANGE[s] || 'NASDAQ'}:${s}`; }

const TV_TABS = [
  ['technicals', 'Technicals', 'embed-widget-technical-analysis.js',
   "TradingView's oscillator and moving-average gauge"],
  ['profile', 'Profile', 'embed-widget-symbol-profile.js',
   'the company behind the ticker'],
  ['financials', 'Financials', 'embed-widget-financials.js',
   'revenue, earnings and cash flow as reported'],
  ['news', 'News', 'embed-widget-timeline.js', 'TradingView headlines for this symbol'],
  ['calendar', 'Calendar', 'embed-widget-events.js', 'high-importance economic events'],
  ['heatmap', 'Heatmap', 'embed-widget-stock-heatmap.js',
   'S&P 500 by market cap, coloured by change'],
];

function renderCharting() {
  const page = el('div', 'page');
  const sym = state.symbol;
  const tv = tvSymbol(sym);
  const ks = (DATA.keystats || {})[sym];
  const sc = (DATA.scans || {})[sym];

  const banner = el('div', 'banner');
  banner.innerHTML = '<span aria-hidden>▲</span><span><strong>The TradingView panels below ' +
    'are live; everything else in this document is not.</strong> They are fetched from ' +
    'TradingView when you open this file, so they show real market data. Every other number ' +
    'here — chains, greeks, backtests, forecasts, and the statistics panel — was generated by ' +
    'the synthetic provider at build time. Reading the two against each other tells you ' +
    'nothing.</span>';
  page.appendChild(banner);

  /* --- terminal header --- */
  const th = el('div', 'term-head');
  th.innerHTML =
    `<div class="term-ticker"><h1>${sym}</h1><span class="badge">LIVE TV</span></div>` +
    '<div class="term-sub">market data feed</div>';
  const thRight = el('div', 'row');
  thRight.style.cssText = 'gap:6px;margin-left:auto';
  const scanBtn = el('button', 'btn', 'Series scan');
  if (state.scanOpen) scanBtn.setAttribute('aria-pressed', 'true');
  scanBtn.onclick = () => { state.scanOpen = !state.scanOpen; render(); };
  thRight.appendChild(scanBtn);
  th.appendChild(thRight);
  page.appendChild(th);

  /* --- terminal: chart | symbol | execution --- */
  const term = el('div', 'terminal');

  const chartCol = el('div', 'term-panel');
  chartCol.appendChild(tvWidget('embed-widget-advanced-chart.js', {
    autosize: true, symbol: tv, interval: 'D', timezone: 'Etc/UTC', style: '1',
    allow_symbol_change: true, hide_side_toolbar: false, hide_top_toolbar: false,
    hide_legend: false, withdateranges: true, details: true, hotlist: true,
    calendar: true, save_image: true, show_popup_button: true,
    popup_width: '1200', popup_height: '800',
    studies: ['STD;RSI', 'STD;MACD'],
    watchlist: Object.keys(TV_EXCHANGE).map(tvSymbol),
    backgroundColor: 'rgba(0,0,0,0)', gridColor: 'rgba(255,255,255,0.06)',
    support_host: 'https://www.tradingview.com',
  }, 560, 'The TradingView chart'));
  term.appendChild(chartCol);

  /* Symbol statistics + optional scan. Snapshotted at build time, so the badge
   * says snapshot rather than live — the running app refreshes these. */
  const symCol = el('div', 'term-panel');
  symCol.appendChild(el('div', 'term-panel-head',
    '<span>symbol</span><span class="chip" style="margin-left:auto">snapshot</span>'));
  if (ks) {
    const fmt = (n, d) => (n === null || n === undefined ? '—' : n.toFixed(d === undefined ? 2 : d));
    const big = (n) => {
      if (n === null || n === undefined) return '—';
      const a = Math.abs(n);
      if (a >= 1e12) return (n / 1e12).toFixed(2) + 'T';
      if (a >= 1e9) return (n / 1e9).toFixed(2) + 'B';
      if (a >= 1e6) return (n / 1e6).toFixed(2) + 'M';
      if (a >= 1e3) return (n / 1e3).toFixed(1) + 'K';
      return n.toFixed(2);
    };
    const bar = (lo, hi, at, label) => {
      const p = (lo === null || hi === null || hi <= lo)
        ? null : Math.min(1, Math.max(0, (at - lo) / (hi - lo)));
      return `<div class="range-row"><span class="range-end num">${fmt(lo)}</span>` +
        `<div class="range-track"><div class="range-fill" style="width:${(p || 0) * 100}%"></div>` +
        (p === null ? '' : `<span class="range-mark" style="left:${p * 100}%"></span>`) +
        `</div><span class="range-end num">${fmt(hi)}</span>` +
        `<span class="range-label">${label}</span></div>`;
    };
    const up = (ks.change || 0) >= 0;
    const panel = el('div', 'sym-panel');
    panel.innerHTML =
      `<div class="sym-ident"><div class="sym-name">${ks.name || ks.symbol}</div>` +
      `<div class="sym-meta">${ks.exchange || '—'}${ks.sector ? ' · ' + ks.sector : ''}</div></div>` +
      `<div class="sym-price"><span class="num">${fmt(ks.last)}</span><small>USD</small></div>` +
      `<div class="sym-change num ${up ? 'pos' : 'neg'}">${up ? '+' : ''}${fmt(ks.change)}` +
      ` &nbsp; ${up ? '+' : ''}${fmt(ks.change_pct)}%</div>` +
      bar(ks.day_low, ks.day_high, ks.last, 'Day') +
      bar(ks.range_52w.low, ks.range_52w.high, ks.last,
          ks.range_52w.complete ? '52wk' : ks.range_52w.sessions + 'd') +
      '<div class="sym-stats">' +
      `<div class="kv"><span>Bid / Ask</span><strong class="num">${fmt(ks.bid)} / ${fmt(ks.ask)}</strong></div>` +
      `<div class="kv"><span>Volume</span><strong class="num">${big(ks.volume)}</strong></div>` +
      `<div class="kv"><span>Avg volume (${ks.avg_volume_sessions}d)</span><strong class="num">${big(ks.avg_volume)}</strong></div>` +
      `<div class="kv"><span>Market cap</span><strong class="num">${big(ks.market_cap)}</strong></div>` +
      `<div class="kv"><span>Next earnings</span><strong>${ks.days_to_earnings !== null ? 'in ' + ks.days_to_earnings + ' days' : '—'}</strong></div>` +
      '</div>' +
      (ks.earnings_note ? `<p class="sym-note">${ks.earnings_note}</p>` : '');
    symCol.appendChild(panel);
  }

  if (state.scanOpen && sc) {
    const ICONS = {
      trend: 'M4 17l5-6 4 3 7-9', momentum: 'M12 3v18M6 9v9M18 6v12',
      volatility: 'M3 12h3l3-7 4 14 3-7h5', support: 'M4 18h16M8 14l4 4 4-4',
      resistance: 'M4 6h16M8 10l4-4 4 4',
    };
    const sheet = el('div', 'scan-sheet');
    const shead = el('div', 'scan-head', `<strong>Series scan · ${sym}</strong>`);
    const close = el('button', 'rail-icon-btn',
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>');
    close.onclick = () => { state.scanOpen = false; render(); };
    shead.appendChild(close);
    sheet.appendChild(shead);
    const list = el('div', 'scan-list');
    list.innerHTML = (sc.findings || []).map((f) =>
      '<div class="scan-item">' +
      `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" ` +
      `stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" class="scan-icon k-${f.kind}">` +
      `<path d="${ICONS[f.kind] || ICONS.trend}"/></svg>` +
      `<div><div class="scan-label">${f.label}</div>` +
      `<div class="scan-detail">${f.detail}</div></div></div>`).join('');
    sheet.appendChild(list);
    sheet.appendChild(el('p', 'scan-note', sc.note));
    symCol.appendChild(sheet);
  }
  term.appendChild(symCol);

  /* Execution: locked, with the real adapter states behind it. */
  const exec = el('aside', 'exec-panel');
  exec.appendChild(el('div', 'exec-head',
    '<span>EXECUTION</span><span class="exec-status"><i class="dot off"></i>OFFLINE</span>'));
  const lock = el('div', 'exec-lock');
  lock.innerHTML =
    '<span class="lock-icon"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="1.6"><rect x="4" y="10" width="16" height="10" rx="2"/>' +
    '<path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg></span>' +
    '<strong>Terminal locked</strong>' +
    '<p>No brokerage is connected, so nothing here can send an order. In this single ' +
    'file it could not in any case — there is no API behind it.</p>' +
    '<div class="broker-list">' +
    (DATA.brokers || []).map((b) =>
      '<div class="broker-row"><div class="broker-name">' +
      `<i class="dot ${b.connected ? 'on' : 'off'}"></i>${b.name}` +
      `<span class="chip">${b.trading_enabled ? 'orders enabled' : b.connected ? 'data only' : 'not connected'}</span>` +
      `</div><p>${b.notes}</p></div>`).join('') +
    '</div>';
  exec.appendChild(lock);
  term.appendChild(exec);

  page.appendChild(term);

  /* --- tabbed secondary panels; only the selected one is fetched --- */
  const tabId = state.tvTab || 'technicals';
  const meta = TV_TABS.find((t) => t[0] === tabId) || TV_TABS[0];
  const panel = el('div', 'card');
  const headEl = el('div', 'card-head',
    `<div><h2>${meta[1]}</h2><span class="sub">${meta[3]}</span></div>`);
  const tabs = el('div', 'tabs tabs-wrap');
  for (const [id, label] of TV_TABS) {
    const btn = el('button', 'tab', label);
    if (id === tabId) btn.setAttribute('aria-selected', 'true');
    btn.onclick = () => { state.tvTab = id; render(); };
    tabs.appendChild(btn);
  }
  headEl.appendChild(tabs);
  panel.appendChild(headEl);
  const pBody = el('div', 'card-body flush');
  const CFG = {
    technicals: { symbol: tv, interval: '1D', showIntervalTabs: true, isTransparent: true,
                  width: '100%', height: 460 },
    profile: { symbol: tv, isTransparent: true, width: '100%', height: 460 },
    financials: { symbol: tv, displayMode: 'regular', largeChartUrl: '',
                  isTransparent: true, width: '100%', height: 500 },
    news: { feedMode: 'symbol', symbol: tv, isTransparent: true,
            displayMode: 'regular', width: '100%', height: 500 },
    calendar: { isTransparent: true, width: '100%', height: 500,
                importanceFilter: '0,1', countryFilter: 'us,eu,gb,jp' },
    heatmap: { dataSource: 'SPX500', blockSize: 'market_cap_basic', blockColor: 'change',
               grouping: 'sector', symbolUrl: '', hasTopBar: false, isDataSetEnabled: false,
               isZoomEnabled: true, hasSymbolTooltip: true, isTransparent: true,
               width: '100%', height: 520 },
  };
  const H = { technicals: 460, profile: 460, financials: 500, news: 500, calendar: 500, heatmap: 520 };
  pBody.appendChild(tvWidget(meta[2], CFG[tabId], H[tabId], meta[1]));
  panel.appendChild(pBody);
  page.appendChild(panel);

  /* --- our data, kept clearly separate --- */
  const card = el('div', 'card');
  card.appendChild(el('div', 'card-head',
    `<div><h2>${sym} — this app's data</h2><span class="sub">daily candles and volume, ` +
    `the same series the backtests and forecasts here were computed from</span></div>` +
    `<span class="chip">static snapshot</span>`));
  const body = el('div', 'card-body flush');
  body.appendChild(candleChart(DATA.candles && DATA.candles[sym], sym));
  card.appendChild(body);
  page.appendChild(card);

  if (DATA.tv_status) page.appendChild(renderTvStatus(DATA.tv_status));
  return page;
}


/* ================================================================= AGENT */
/*
 * A replay of real agent turns captured at build time. The composer is inert —
 * the router lives in the API and a single HTML document cannot reach it — so
 * it says so rather than presenting an input box that silently swallows what
 * you type. Everything above it is genuine output from the real router,
 * including the tool calls.
 */

function renderAgent() {
  const A = DATA.agent || { capabilities: {}, turns: [] };
  const page = el('div', 'workspace is-reading');
  page.style.padding = '0 16px';

  const stream = el('div', 'agent-stream');

  const intro = el('div', 'agent-intro');
  intro.innerHTML =
    '<h1 class="type-heading-lg">What do you want to look at?</h1>' +
    '<p class="type-body">I run the same engines the rest of the workspace does and show you ' +
    'the real output — not a description of it. Every turn lists the tools that actually ran.</p>' +
    `<p class="type-caption agent-engine"><span class="pulse-dot"></span> ` +
    `${A.capabilities.engine || 'rules'} engine · no model key needed · cannot place live orders</p>`;
  stream.appendChild(intro);

  const cards = el('div', 'entry-cards');
  const CARDS = [
    ['Build a position', 'price it, see the greeks', 'var(--series-1)', 'M4 18h4V9H4zm6 0h4V4h-4zm6 0h4v-6h-4z'],
    ['Suggest a structure', 'ranked against the surface', '#a06cf0', 'M12 3v3m0 12v3M3 12h3m12 0h3M5.6 5.6l2.1 2.1m8.6 8.6 2.1 2.1m0-12.8-2.1 2.1m-8.6 8.6-2.1 2.1'],
    ['Test it on history', 'rules replay with the caveats', 'var(--accent-brand)', 'M3 3v18h18M7 15l4-5 3 3 5-7'],
    ['Check the models', 'and their measured skill', 'var(--status-warning)', 'M12 20v-6m0 0V4m0 10 4-4m-4 4-4-4'],
  ];
  cards.innerHTML = CARDS.map(([label, hint, color, icon]) =>
    '<div class="entry-card">' +
    `<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="${color}" ` +
    `stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="${icon}"/></svg>` +
    `<span class="entry-label">${label}</span><span class="entry-hint">${hint}</span></div>`).join('');
  intro.appendChild(cards);

  const md = (t) => t.split('\n').map((line) => {
    if (!line.trim()) return '<br>';
    return '<p>' + line.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>') + '</p>';
  }).join('');

  for (const { message, turn } of A.turns) {
    stream.appendChild(el('div', 'msg-user', message));
    const m = el('div', 'msg-agent');
    if (!turn.understood) m.appendChild(el('span', 'chip warnchip', 'not understood'));
    m.appendChild(el('div', 'msg-body', md(turn.reply)));
    if (turn.tool_calls && turn.tool_calls.length) {
      const tc = el('div', 'tool-calls');
      tc.innerHTML = turn.tool_calls.map((c) =>
        '<details class="code-block"><summary class="code-header">' +
        `<span>ran ${c.tool}</span><span class="type-caption">arguments</span></summary>` +
        `<pre class="type-code">${JSON.stringify(c.args, null, 2)}</pre></details>`).join('');
      m.appendChild(tc);
    }
    if (turn.actions && turn.actions.length) {
      const acts = el('div', 'agent-actions');
      acts.innerHTML = turn.actions.map((a) =>
        `<span class="chip">${a.type}${a.to ? ' → ' + a.to : ''}</span>`).join('');
      m.appendChild(acts);
    }
    stream.appendChild(m);
  }
  page.appendChild(stream);

  const wrap = el('div', 'composer-wrap');
  const box = el('div', 'composer-box');
  const input = el('input', 'composer-input');
  input.placeholder = 'Recorded transcript — the live composer needs the running API';
  input.disabled = true;
  box.appendChild(input);
  box.appendChild(el('button', 'composer-send',
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>'));
  box.querySelector('.composer-send').disabled = true;
  wrap.appendChild(box);
  const bar = el('div', 'composer-bar');
  bar.innerHTML =
    '<span class="composer-pill"><span class="pulse-dot"></span>Rules engine</span>' +
    '<span class="composer-pill">Replay only</span>' +
    '<span class="composer-foot type-caption">These four exchanges are real router output ' +
    'captured at build time. Run the repo to type your own.</span>';
  wrap.appendChild(bar);
  page.appendChild(wrap);
  return page;
}

/* ================================================================== SHELL */

function render() {
  const app = $('#app');
  app.innerHTML = '';

  const root = el('div', 'shell-root' + (state.railCollapsed ? ' is-collapsed' : ''));

  /* ---- rail ----
   * Mirrors apps/web Shell.tsx exactly: a 48px head that lines up with the top
   * bar on a shared divider, grouped links with small grey section labels,
   * 30px rows, and an account block at the bottom. Flat throughout — the
   * active item is a lighter row plus a 2px bar and nothing else. */
  const rail = el('aside', 'rail-nav');

  const head = el('div', 'header-brand');
  const title = el('button', 'rail-title',
    '<svg class="brand-mark" viewBox="0 0 24 24" aria-hidden>' +
    '<path d="M2 15c4-1 6-4 8-8 1.6 3.2 3.4 5 6 6l6-4-3 8c-3.4 2.6-9 3-13 1l-4-3z" fill="var(--accent-brand)"/></svg>' +
    '<span class="rail-label">MarketShark</span>' +
    '<svg class="rail-label rail-caret" width="10" height="10" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="3" aria-hidden><path d="m6 9 6 6 6-6"/></svg>');
  title.title = state.railCollapsed ? 'Expand sidebar' : 'Collapse sidebar';
  title.onclick = () => { state.railCollapsed = !state.railCollapsed; render(); };
  head.appendChild(title);

  const ICONS = {
    agent: 'M4 5h16v11H9l-5 4z',
    lab: 'M5 19h14M7 19V9m5 10V5m5 14v-7',
    chart: 'M4 20V4m0 16h16M8 16l4-6 3 3 5-7',
    dash: 'M4 13h5l2 6 3-14 2 8h4',
    res: 'M10.5 4a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13zM20 20l-4.9-4.9',
    bt: 'M4 4v16h16M8 15l3-4 3 2 4-6',
  };
  const GROUPS = [
    [null, [['agent', 'New chat']]],
    ['Workspace', [['lab', 'Strategy Lab'], ['chart', 'Charting'], ['dash', 'Dashboard']]],
    ['Analysis', [['res', 'Research'], ['bt', 'Backtest']]],
  ];
  const groups = el('nav', 'rail-groups');
  for (const [label, items] of GROUPS) {
    const g = el('div', 'rail-group');
    if (label) g.appendChild(el('div', 'rail-group-label rail-label', label));
    for (const [id, text] of items) {
      const a = el('a', 'rail-link',
        `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" ` +
        `stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden>` +
        `<path d="${ICONS[id]}"/></svg><span class="rail-label">${text}</span>`);
      a.href = '#';
      a.title = text;
      if (state.view === id) a.setAttribute('aria-current', 'page');
      a.onclick = (e) => { e.preventDefault(); state.view = id; render(); };
      g.appendChild(a);
    }
    groups.appendChild(g);
  }
  rail.appendChild(groups);

  const foot = el('div', 'rail-foot');
  foot.innerHTML =
    '<div class="rail-account"><span class="avatar" aria-hidden>MS</span>' +
    '<span class="rail-label account-name">Synthetic data' +
    '<small>bundled snapshot, no live feed</small></span></div>';
  rail.appendChild(foot);

  /* ---- main column ---- */
  const main = el('div', 'shell-main');
  /* The header is one band spanning both grid columns — brand cell over the
   * rail, bar over the content, no element boundary between them to show as a
   * seam. Built here, appended to the grid root below. */
  const header = el('header', 'app-header');
  header.appendChild(head);
  const bar = el('div', 'header-bar');

  // Symbol picker. The live app searches the API; a static document can only
  // offer what the bundle carries, so this lists exactly those and says so.
  const picker = el('div', 'symsearch');
  const trigger = el('button', 'symsearch-trigger',
    '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
    '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5" stroke-linecap="round"/></svg>' +
    `<span class="symsearch-value">${state.symbol}</span><kbd>${Object.keys(SYMBOLS).length}</kbd>`);
  trigger.onclick = (e) => { e.stopPropagation(); state.symOpen = !state.symOpen; render(); };
  picker.appendChild(trigger);
  if (state.symOpen) {
    const panel = el('div', 'symsearch-panel');
    panel.onclick = (e) => e.stopPropagation();
    const list = el('div', 'symsearch-list');
    for (const s of Object.keys(SYMBOLS)) {
      const row = el('button', 'symsearch-row',
        `<span class="sym">${s}</span><span class="desc">${SYMBOLS[s].name}</span>` +
        `<span class="quote">${SYMBOLS[s].spot.toFixed(2)}</span>`);
      row.setAttribute('aria-selected', String(s === state.symbol));
      row.onclick = () => { state.symbol = s; state.symOpen = false; resetParams(); render(); };
      list.appendChild(row);
    }
    panel.appendChild(list);
    panel.appendChild(el('div', 'symsearch-warn',
      'This is a static snapshot, so it carries these symbols only. The running app searches ' +
      'the live symbol index from the top bar with ⌘K.'));
    picker.appendChild(panel);
  }
  bar.appendChild(picker);

  const right = el('div', 'topbar-actions');
  if (state.view === 'lab') {
    const s = SYMBOLS[state.symbol];
    const q = el('div', 'row num');
    q.style.gap = '10px';
    q.innerHTML = `<strong style="font-size:17px">${s.spot.toFixed(2)}</strong>` +
                  '<span class="chip">synthetic</span>';
    right.appendChild(q);
  } else {
    right.appendChild(el('span', 'chip', 'snapshot from the running API'));
  }
  bar.appendChild(right);
  bar.appendChild(settingsControl());
  header.appendChild(bar);
  // Header, then rail, then content — DOM order matching reading order, so the
  // tab sequence goes brand -> search -> nav rather than nav -> brand.
  root.appendChild(header);
  root.appendChild(rail);

  if (state.view === 'agent') {
    main.appendChild(renderAgent());
    root.appendChild(main);
    app.appendChild(root);
    return;
  }

  const work = el('main', 'workspace');
  const banner = el('div', 'banner');
  banner.innerHTML = '<span aria-hidden>▲</span><span><strong>Synthetic data.</strong> ' +
    'Prices, chains and volatility are generated — internally consistent and deterministic, ' +
    'but not market data. The Strategy Lab math runs live in your browser and is verified ' +
    'against the Python engine to the cent at build time; every other view is a snapshot the ' +
    'Python engine computed at build time. The agent and live refresh need the running API — ' +
    'they are in the repo, not in this single file.</span>';
  work.appendChild(banner);

  work.appendChild(
    state.view === 'lab' ? renderLab()
      : state.view === 'dash' ? renderDashboard()
        : state.view === 'res' ? renderResearch()
          : state.view === 'bt' ? renderBacktest()
            : renderCharting(),
  );
  main.appendChild(work);
  root.appendChild(main);
  app.appendChild(root);
}

document.addEventListener('click', () => {
  if (state.settingsOpen) { state.settingsOpen = false; render(); }
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && state.settingsOpen) { state.settingsOpen = false; render(); }
});

document.documentElement.setAttribute('data-theme', state.theme);
resetParams();
render();
