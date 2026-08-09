/* ==========================================================================
   UI. Three views over the engine: Strategy Lab (live), Dashboard, Backtest.
   ========================================================================== */

const SYMBOLS = DATA.symbols;
const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};

/* ------------------------------------------------------------ formatting */

function money(v, dp = 0) {
  if (v == null || !Number.isFinite(v)) return '—';
  const s = Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp });
  return (v < 0 ? '-$' : '$') + s;
}
const pctS = (v, dp = 1) => (v == null || !Number.isFinite(v) ? '—' : (v * 100).toFixed(dp) + '%');
const numS = (v, dp = 2) => (v == null || !Number.isFinite(v) ? '—' : v.toFixed(dp));
const fmtStrike = (k) => (k % 1 === 0 ? String(k) : k.toFixed(1));

function ticks(lo, hi, target = 6) {
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi <= lo) return [lo];
  const raw = (hi - lo) / target;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm >= 5 ? 5 : norm >= 2 ? 2 : 1) * mag;
  const out = [];
  for (let t = Math.ceil(lo / step) * step; t <= hi + step * 1e-9; t += step) {
    out.push(Math.abs(t) < step * 1e-9 ? 0 : t);
  }
  return out;
}

const svgEl = (t, attrs = {}) => {
  const n = document.createElementNS('http://www.w3.org/2000/svg', t);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
};

/* ---------------------------------------------------------------- state */

const state = {
  view: 'agent',
  symbol: 'NVDA',
  template: 'iron_condor',
  params: {},
  chartTab: 'payoff',
  backtest: 0,
  researchTab: 'forecast',
  researchSymbol: null,
  theme: 'dark-blue',
  settingsOpen: false,
  railCollapsed: false,
  symOpen: false,
};

function resetParams() {
  const meta = DATA.templates[state.template];
  state.params = {};
  for (const p of meta.params) state.params[p.key] = p.default;
}

/* ------------------------------------------------------- earnings context */

function earningsContext(pos) {
  const s = SYMBOLS[state.symbol];
  if (!s.nextEarnings) return null;
  const days = Math.round(
    (new Date(s.nextEarnings + 'T00:00:00') - startOfToday()) / 86400000,
  );
  const inWindow = days >= 0 && days <= pos.maxDte;
  // Event move isolated from the term structure, exactly as the API does it:
  // sigma_jump is a one-off return sd, and E|move| = sqrt(2/pi) * sigma_jump.
  return {
    symbol: state.symbol, date: s.nextEarnings, daysAway: days, inWindow,
    impliedMove: MAD * s.jumpVol, eventVol: s.jumpVol, session: s.session,
  };
}

/* ============================================================ STRATEGY LAB */

function renderLab() {
  const root = el('div', 'shell');

  /* ---- left rail ---- */
  const left = el('aside', 'rail');

  const picker = el('div', 'card');
  picker.appendChild(el('div', 'card-head',
    `<h2>Strategies</h2><span class="sub">${Object.keys(DATA.templates).length}</span>`));
  const list = el('div');
  list.style.maxHeight = '430px';
  list.style.overflowY = 'auto';
  for (const cat of DATA.categories) {
    const items = Object.values(DATA.templates).filter((t) => t.category === cat.id);
    if (!items.length) continue;
    const group = el('div', 'cat-group');
    group.appendChild(el('div', 'cat-head', `<span>${cat.label}</span><span>${items.length}</span>`));
    for (const t of items) {
      const b = el('button', 'strat',
        `<span class="name">${t.name}${t.risk_defined ? '' :
          ' <span class="tiny" style="color:var(--status-serious)">⚠ undefined risk</span>'}</span>` +
        `<span class="desc">${t.summary}</span>`);
      if (t.id === state.template) b.setAttribute('aria-current', 'true');
      b.onclick = () => { state.template = t.id; resetParams(); render(); };
      group.appendChild(b);
    }
    list.appendChild(group);
  }
  picker.appendChild(list);
  left.appendChild(picker);

  const meta = DATA.templates[state.template];
  const paramsCard = el('div', 'card');
  paramsCard.appendChild(el('div', 'card-head',
    `<h2>Parameters</h2><span class="sub">${meta.name}</span>`));
  const body = el('div', 'card-body');
  for (const p of meta.params) {
    const v = state.params[p.key];
    const field = el('div', 'field');
    if (p.type === 'select' && p.options) {
      field.appendChild(el('label', null, p.label));
      const sel = el('select', 'select');
      for (const o of p.options) {
        const opt = el('option', null, o.label);
        opt.value = o.value;
        if (String(v) === o.value) opt.selected = true;
        sel.appendChild(opt);
      }
      sel.onchange = (e) => { state.params[p.key] = e.target.value; render(); };
      field.appendChild(sel);
    } else {
      const isDelta = p.type === 'delta';
      field.appendChild(el('label', null,
        `<span>${p.label}</span><span class="value">${isDelta ? Number(v).toFixed(2) : v}</span>`));
      const input = el('input');
      input.type = 'range';
      input.min = p.min ?? 0; input.max = p.max ?? 100;
      input.step = p.step ?? 1; input.value = v;
      input.setAttribute('aria-label', p.label);
      input.oninput = (e) => { state.params[p.key] = Number(e.target.value); render(); };
      field.appendChild(input);
    }
    if (p.help) field.appendChild(el('div', 'help', p.help));
    body.appendChild(field);
  }
  paramsCard.appendChild(body);
  left.appendChild(paramsCard);

  /* ---- compute ---- */
  const ctx = makeContext(state.symbol);
  const pos = buildPosition(state.template, ctx, state.params);
  const a = analyze(pos);
  const eCtx = earningsContext(pos);
  const insights = evaluateInsights(pos, a, eCtx);

  /* ---- main ---- */
  const main = el('main', 'main');

  const head = el('div', 'card');
  const hh = el('div', 'card-head');
  hh.innerHTML =
    `<div><h2>${meta.name}</h2><span class="sub">${state.symbol} · ${a.contracts} option legs · ` +
    `${a.isCredit ? 'credit' : 'debit'} ${money(Math.abs(a.netDebit))}</span></div>` +
    `<div class="row" style="gap:6px">` +
    (a.riskDefined ? '' : '<span class="chip" style="color:var(--status-serious)">⚠ undefined risk</span>') +
    (eCtx && eCtx.inWindow
      ? `<span class="chip" style="color:var(--status-warning)">▲ earnings in ${eCtx.daysAway}d · ${pctS(eCtx.impliedMove, 1)} implied</span>`
      : '') +
    `<span class="chip">live in-browser math</span></div>`;
  head.appendChild(hh);
  head.appendChild(metricTiles(a));
  main.appendChild(head);

  const chartCard = el('div', 'card');
  const ch = el('div', 'card-head');
  const tabs = el('div', 'tabs');
  for (const [id, label] of [['payoff', 'Risk graph'], ['surface', 'P/L surface'], ['chain', 'Option chain']]) {
    const b = el('button', 'tab', label);
    b.setAttribute('role', 'tab');
    b.setAttribute('aria-selected', String(state.chartTab === id));
    b.onclick = () => { state.chartTab = id; render(); };
    tabs.appendChild(b);
  }
  ch.appendChild(tabs);
  ch.appendChild(el('span', 'sub',
    state.chartTab === 'payoff' ? 'profit and loss against price, at expiry and along the way'
      : state.chartTab === 'surface' ? 'every price and every day between now and expiry'
      : 'calls left, puts right, around the money'));
  chartCard.appendChild(ch);
  const cb = el('div', 'card-body flush');
  cb.appendChild(
    state.chartTab === 'payoff' ? payoffChart(pos, a)
      : state.chartTab === 'surface' ? plSurface(pos)
      : chainGrid(ctx, pos),
  );
  chartCard.appendChild(cb);
  main.appendChild(chartCard);

  const legsCard = el('div', 'card');
  legsCard.appendChild(el('div', 'card-head',
    '<h2>Legs</h2><span class="sub">black-scholes-merton</span>'));
  legsCard.appendChild(legTable(pos));
  main.appendChild(legsCard);

  /* ---- right rail ---- */
  const right = el('aside', 'rail rail-right');

  const ins = el('div', 'card');
  ins.appendChild(el('div', 'card-head',
    `<h2>What the model flags</h2><span class="sub">${insights.length}</span>`));
  const ib = el('div');
  const ICON = { critical: ['✕', 'critical'], warning: ['!', 'serious'],
                 caution: ['▲', 'warning'], info: ['✓', 'good'] };
  for (const i of insights) {
    const [icon, tok] = ICON[i.severity];
    ib.appendChild(el('div', 'insight',
      `<span class="icon" style="color:var(--status-${tok})" aria-hidden>${icon}</span>` +
      `<div><div class="t">${i.title} <span class="tiny muted">· ${i.severity}</span></div>` +
      `<div class="d">${i.detail}</div></div>`));
  }
  if (!insights.length) ib.appendChild(el('div', 'card-body muted tiny', 'No flags raised.'));
  ins.appendChild(ib);
  right.appendChild(ins);

  const gk = el('div', 'card');
  gk.appendChild(el('div', 'card-head', '<h2>Net greeks</h2>'));
  gk.appendChild(greeksTable(a));
  right.appendChild(gk);

  const vs = el('div', 'card');
  vs.appendChild(el('div', 'card-head',
    '<h2>Volatility sensitivity</h2><span class="sub">parallel IV shift</span>'));
  vs.appendChild(volSensTable(a));
  right.appendChild(vs);

  const ex = el('div', 'card');
  ex.appendChild(el('div', 'card-head', '<h2>How this strategy works</h2>'));
  ex.appendChild(el('div', 'card-body explain',
    `<p style="color:var(--text-primary);font-size:13px">${meta.summary}</p>` +
    `<h3>How it works</h3><p>${meta.mechanics}</p>` +
    `<h3>When to use it</h3><p>${meta.when_to_use}</p>` +
    `<h3>What can go wrong</h3><p>${meta.risk_notes}</p>` +
    `<h3>Managing it</h3><p>${meta.management}</p>` +
    `<div class="row" style="flex-wrap:wrap;gap:6px;margin-top:14px">` +
    meta.tags.map((t) => `<span class="chip">${t}</span>`).join('') + `</div>`));
  right.appendChild(ex);

  root.append(left, main, right);
  return root;
}

function metricTiles(a) {
  const t = el('div', 'tiles');
  const tile = (k, v, n, cls = '') =>
    `<div class="tile"><div class="k">${k}</div><div class="v ${cls}">${v}</div><div class="n">${n}</div></div>`;
  t.innerHTML =
    tile(a.isCredit ? 'Net credit' : 'Net debit', money(Math.abs(a.netDebit)),
         `${a.contracts} option legs · ${money(a.commission, 2)} fees`) +
    tile('Max profit', a.maxProfitUnbounded ? 'Unlimited' : money(a.maxProfit),
         a.riskReward ? `${numS(a.riskReward)}:1 reward/risk` : 'no capped reward',
         a.maxProfitUnbounded || (a.maxProfit ?? 0) > 0 ? 'pos' : '') +
    tile('Max loss', a.maxLossUnbounded ? 'Unlimited' : money(a.maxLoss),
         a.riskDefined ? 'risk defined' : 'undefined risk', 'neg') +
    tile('Prob. of profit', pctS(a.pop, 1), 'market-implied') +
    tile('Expected value', (a.expectedValue >= 0 ? '+' : '') + money(a.expectedValue),
         'against the implied distribution', a.expectedValue >= 0 ? 'pos' : 'neg') +
    tile('Breakeven' + (a.breakevens.length === 1 ? '' : 's'),
         a.breakevens.length ? a.breakevens.map((b) => b.toFixed(2)).join(' / ') : '—',
         `spot ${a.spot.toFixed(2)}`) +
    tile('Capital', money(a.capital),
         a.returnOnCapital ? `${pctS(a.returnOnCapital, 1)} max return` : 'Reg-T estimate') +
    tile('Expected move',
         `${a.expectedMove.low.toFixed(1)} – ${a.expectedMove.high.toFixed(1)}`,
         `±${pctS(a.expectedMovePct, 1)} · ${Math.round(a.dte)} DTE · IV ${pctS(a.blendedIv, 1)}`);
  // Breakevens can be long; shrink that one tile's value text.
  const bTile = t.children[5];
  if (a.breakevens.length > 1) bTile.querySelector('.v').style.fontSize = '15px';
  t.children[7].querySelector('.v').style.fontSize = '15px';
  return t;
}

/* ------------------------------------------------------------ payoff chart */

const CURVE_RAMP = ['var(--seq-250)', 'var(--seq-350)', 'var(--seq-450)', 'var(--seq-550)'];

function payoffChart(pos, a) {
  const W = 900, H = 420, PAD = { top: 18, right: 74, bottom: 34, left: 62 };
  const prices = pos.priceGrid(161);
  const expiry = prices.map((p) => pos.plAtExpiry(p));

  const dte = pos.minDte || 30;
  const offsets = [0, +(dte / 3).toFixed(1), +((2 * dte) / 3).toFixed(1)];
  const curves = offsets.map((d) => ({
    label: d === 0 ? 'Today (T+0)' : `T+${d}`,
    pl: prices.map((p) => pos.plAt(p, d)),
  }));

  const all = [...expiry, ...curves.flatMap((c) => c.pl)];
  let lo = Math.min(...all), hi = Math.max(...all);
  const pad = Math.max((hi - lo) * 0.12, 1);
  lo = Math.min(lo - pad, 0); hi = Math.max(hi + pad, 0);

  const xMin = prices[0], xMax = prices[prices.length - 1];
  const iw = W - PAD.left - PAD.right, ih = H - PAD.top - PAD.bottom;
  const sx = (p) => PAD.left + ((p - xMin) / (xMax - xMin)) * iw;
  const sy = (v) => PAD.top + (1 - (v - lo) / (hi - lo)) * ih;
  const path = (vals) => vals.map((v, i) => `${i ? 'L' : 'M'}${sx(prices[i]).toFixed(1)},${sy(v).toFixed(1)}`).join(' ');

  const wrap = el('div', 'chart-wrap');

  const legend = el('div', 'legend');
  legend.innerHTML =
    `<span class="item"><span class="line" style="background:var(--series-2)"></span>At expiration</span>` +
    curves.map((c, i) =>
      `<span class="item"><span class="line" style="background:${CURVE_RAMP[i]}"></span>${c.label}</span>`).join('') +
    `<span class="item muted"><span class="line" style="background:var(--status-good);opacity:.55"></span>profit` +
    `<span class="line" style="background:var(--status-critical);opacity:.55;margin-left:8px"></span>loss</span>`;
  wrap.appendChild(legend);

  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img',
    'aria-label': `Profit and loss versus ${state.symbol} price at expiration` });

  for (const t of ticks(lo, hi, 6)) {
    svg.appendChild(svgEl('line', { class: 'grid-line', x1: PAD.left, x2: W - PAD.right, y1: sy(t), y2: sy(t) }));
    const tx = svgEl('text', { class: 'axis-label', x: PAD.left - 8, y: sy(t) + 3.5, 'text-anchor': 'end' });
    tx.textContent = money(t);
    svg.appendChild(tx);
  }

  // Polarity shading: split the expiry curve at each sign change.
  const zeroY = sy(0);
  let run = [];
  const flush = () => {
    if (run.length > 1) {
      const d = `M${sx(prices[run[0].i]).toFixed(1)},${zeroY.toFixed(1)} ` +
        run.map((r) => `L${sx(prices[r.i]).toFixed(1)},${sy(expiry[r.i]).toFixed(1)}`).join(' ') +
        ` L${sx(prices[run[run.length - 1].i]).toFixed(1)},${zeroY.toFixed(1)} Z`;
      svg.appendChild(svgEl('path', { d,
        fill: run[0].sign > 0 ? 'var(--status-good)' : 'var(--status-critical)', opacity: 0.14 }));
    }
    run = [];
  };
  for (let i = 0; i < expiry.length; i++) {
    const sign = expiry[i] >= 0 ? 1 : -1;
    if (run.length && run[run.length - 1].sign !== sign) flush();
    run.push({ i, sign });
  }
  flush();

  svg.appendChild(svgEl('line', { x1: PAD.left, x2: W - PAD.right, y1: zeroY, y2: zeroY,
    stroke: 'var(--axis)', 'stroke-width': 1.5 }));

  for (const l of pos.legs) {
    if (!l.strike || l.strike < xMin || l.strike > xMax) continue;
    svg.appendChild(svgEl('line', { x1: sx(l.strike), x2: sx(l.strike),
      y1: H - PAD.bottom, y2: H - PAD.bottom - 7, stroke: 'var(--text-muted)', 'stroke-width': 1.5 }));
  }

  if (pos.spot >= xMin && pos.spot <= xMax) {
    svg.appendChild(svgEl('line', { x1: sx(pos.spot), x2: sx(pos.spot), y1: PAD.top,
      y2: H - PAD.bottom, stroke: 'var(--text-muted)', 'stroke-width': 1, opacity: 0.7 }));
    const t = svgEl('text', { class: 'axis-label', x: sx(pos.spot) + 5, y: PAD.top + 10 });
    t.textContent = `spot ${pos.spot.toFixed(2)}`;
    svg.appendChild(t);
  }

  curves.forEach((c, i) => svg.appendChild(svgEl('path', { d: path(c.pl), fill: 'none',
    stroke: CURVE_RAMP[i], 'stroke-width': 2, 'stroke-linejoin': 'round' })));
  svg.appendChild(svgEl('path', { d: path(expiry), fill: 'none', stroke: 'var(--series-2)',
    'stroke-width': 2, 'stroke-linejoin': 'round' }));

  for (const b of a.breakevens) {
    if (b < xMin || b > xMax) continue;
    svg.appendChild(svgEl('circle', { cx: sx(b), cy: zeroY, r: 4.5, fill: 'var(--series-2)',
      stroke: 'var(--surface-1)', 'stroke-width': 2 }));
    const t = svgEl('text', { class: 'axis-label', x: sx(b), y: zeroY - 9,
      'text-anchor': 'middle', fill: 'var(--text-secondary)' });
    t.textContent = b.toFixed(2);
    svg.appendChild(t);
  }

  const lbl = (txt, y, fill) => {
    const t = svgEl('text', { class: 'axis-label', x: W - PAD.right + 6, y: y + 3.5, fill });
    t.textContent = txt;
    svg.appendChild(t);
  };
  lbl('expiry', sy(expiry[expiry.length - 1]), 'var(--series-2)');
  lbl('today', sy(curves[0].pl[curves[0].pl.length - 1]), CURVE_RAMP[0]);

  svg.appendChild(svgEl('line', { class: 'axis-line', x1: PAD.left, x2: W - PAD.right,
    y1: H - PAD.bottom, y2: H - PAD.bottom }));
  for (const t of ticks(xMin, xMax, 7)) {
    const tx = svgEl('text', { class: 'axis-label', x: sx(t), y: H - PAD.bottom + 15, 'text-anchor': 'middle' });
    tx.textContent = t.toFixed(t < 20 ? 1 : 0);
    svg.appendChild(tx);
  }
  const ax = svgEl('text', { class: 'axis-label', x: (PAD.left + W - PAD.right) / 2, y: H - 3, 'text-anchor': 'middle' });
  ax.textContent = `${state.symbol} price at expiration`;
  svg.appendChild(ax);

  // Crosshair + tooltip
  const cross = svgEl('g', { 'pointer-events': 'none', style: 'display:none' });
  const cl = svgEl('line', { stroke: 'var(--text-secondary)', 'stroke-width': 1, y1: PAD.top, y2: H - PAD.bottom });
  const dot = svgEl('circle', { r: 4, fill: 'var(--series-2)', stroke: 'var(--surface-1)', 'stroke-width': 2 });
  cross.append(cl, dot);
  svg.appendChild(cross);

  const tip = el('div', 'tooltip');
  tip.style.display = 'none';
  wrap.append(svg, tip);

  svg.addEventListener('mousemove', (e) => {
    const r = svg.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    if (px < PAD.left || px > W - PAD.right) return;
    const value = xMin + ((px - PAD.left) / iw) * (xMax - xMin);
    let i = 0, best = Infinity;
    prices.forEach((p, j) => { const d = Math.abs(p - value); if (d < best) { best = d; i = j; } });
    cross.style.display = '';
    cl.setAttribute('x1', sx(prices[i])); cl.setAttribute('x2', sx(prices[i]));
    dot.setAttribute('cx', sx(prices[i])); dot.setAttribute('cy', sy(expiry[i]));
    tip.style.display = '';
    const leftPct = ((e.clientX - r.left) / r.width) * 100;
    tip.style.left = leftPct > 62 ? `${leftPct - 20}%` : `${leftPct + 3}%`;
    tip.style.transform = leftPct > 62 ? 'translateX(-100%)' : '';
    tip.style.top = '10px';
    tip.innerHTML =
      `<div class="tt-head">${prices[i].toFixed(2)}</div>` +
      `<div class="tt-row"><span>At expiration</span><strong class="${expiry[i] >= 0 ? 'pos' : 'neg'}">${money(expiry[i])}</strong></div>` +
      curves.map((c) => `<div class="tt-row"><span>${c.label}</span><span class="${c.pl[i] >= 0 ? 'pos' : 'neg'}">${money(c.pl[i])}</span></div>`).join('') +
      `<div class="tt-row" style="margin-top:4px;opacity:.75"><span>vs spot</span><span>${(((prices[i] - pos.spot) / pos.spot) * 100).toFixed(1)}%</span></div>`;
  });
  svg.addEventListener('mouseleave', () => { cross.style.display = 'none'; tip.style.display = 'none'; });

  return wrap;
}

/* ------------------------------------------------------------- P/L surface */

function plSurface(pos) {
  const prices = pos.priceGrid(41);
  const horizon = pos.minDte || 30;
  const days = Array.from({ length: 21 }, (_, i) => +((i * horizon) / 20).toFixed(1));
  const grid = days.map((d) => prices.map((p) => pos.plAt(p, d)));
  const flat = grid.flat();
  const maxPos = Math.max(...flat, 1e-9), maxNeg = Math.min(...flat, -1e-9);

  const NEG = ['var(--div-neg-1)', 'var(--div-neg-2)', 'var(--div-neg-3)'];
  const POS = ['var(--div-pos-1)', 'var(--div-pos-2)', 'var(--div-pos-3)'];
  const color = (v) => {
    if (Math.abs(v) < Math.max(maxPos, -maxNeg) * 0.02) return 'var(--div-mid)';
    const t = v > 0 ? Math.min(v / maxPos, 1) : Math.min(v / maxNeg, 1);
    const ramp = v > 0 ? POS : NEG;
    return ramp[t > 0.66 ? 2 : t > 0.33 ? 1 : 0];
  };

  const wrap = el('div', 'chart-wrap');
  const legend = el('div', 'legend');
  legend.innerHTML =
    '<span class="item muted">Loss</span>' +
    [...NEG].reverse().map((c) => `<span class="line" style="background:${c};width:18px;height:10px;border-radius:2px"></span>`).join('') +
    '<span class="line" style="background:var(--div-mid);width:18px;height:10px;border-radius:2px"></span>' +
    POS.map((c) => `<span class="line" style="background:${c};width:18px;height:10px;border-radius:2px"></span>`).join('') +
    '<span class="item muted">Profit</span>' +
    `<span class="item muted" style="margin-left:auto">rows: days held · columns: ${state.symbol} price</span>`;
  wrap.appendChild(legend);

  const holder = el('div');
  holder.style.cssText = 'padding:4px 14px 14px;overflow-x:auto';
  const tbl = el('table', 'num');
  tbl.style.cssText = 'border-collapse:separate;border-spacing:2px;font-size:10px';
  const colStep = Math.max(1, Math.ceil(prices.length / 9));
  const thead = el('thead');
  const hr = el('tr');
  hr.appendChild(el('th', null, 'day')).style.cssText =
    'padding:0 6px 0 0;text-align:right;color:var(--text-muted);font-weight:500;font-size:9.5px';
  prices.forEach((p, c) => {
    const th = el('th', null, c % colStep === 0 ? p.toFixed(0) : '');
    th.style.cssText = 'padding:0;font-weight:500;color:var(--text-muted);font-size:9.5px;min-width:22px';
    hr.appendChild(th);
  });
  thead.appendChild(hr); tbl.appendChild(thead);

  const tip = el('div', 'tooltip');
  tip.style.display = 'none';
  const tbody = el('tbody');
  const rowStep = Math.max(1, Math.ceil(days.length / 8));
  grid.forEach((row, r) => {
    const tr = el('tr');
    const lab = el('td', null, r % rowStep === 0 ? days[r].toFixed(0) : '');
    lab.style.cssText = 'padding:0 6px 0 0;text-align:right;color:var(--text-muted);font-size:9.5px';
    tr.appendChild(lab);
    row.forEach((v, c) => {
      const td = el('td');
      td.style.cssText = `background:${color(v)};width:22px;height:15px;border-radius:2px;padding:0;cursor:crosshair`;
      td.onmouseenter = () => {
        tip.style.display = '';
        tip.style.right = '18px'; tip.style.top = '34px';
        tip.innerHTML = `<div class="tt-head">${prices[c].toFixed(2)}</div>` +
          `<div class="tt-row"><span>Day</span><span>${days[r].toFixed(0)}</span></div>` +
          `<div class="tt-row"><span>P/L</span><strong class="${v >= 0 ? 'pos' : 'neg'}">${money(v)}</strong></div>`;
      };
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  tbl.appendChild(tbody);
  holder.appendChild(tbl);
  wrap.append(holder, tip);
  wrap.onmouseleave = () => { tip.style.display = 'none'; };
  return wrap;
}

/* -------------------------------------------------------------- chain grid */

function chainGrid(ctx, pos) {
  const dte = Math.round(pos.minDte || 45);
  const strikes = [];
  const lo = ctx.roundStrike(ctx.spot * 0.88), hi = ctx.roundStrike(ctx.spot * 1.12);
  for (let k = lo; k <= hi + 1e-9; k += ctx.inc) strikes.push(+k.toFixed(4));

  const wrap = el('div');
  wrap.appendChild(el('div', 'row',
    `<span class="chip">${dte} DTE</span><span class="tiny muted">theoretical mid, priced off the same surface the risk graph uses</span>`)
  ).style.cssText = 'padding:10px 14px;gap:12px';

  const scroll = el('div', 'scroll');
  const t = el('table', 'table');
  t.innerHTML =
    '<thead><tr><th colspan="3" style="text-align:center;color:var(--text-secondary)">Calls</th>' +
    '<th style="text-align:center">Strike</th>' +
    '<th colspan="3" style="text-align:center;color:var(--text-secondary)">Puts</th></tr>' +
    '<tr><th>IV</th><th>Δ</th><th>Mid</th><th></th><th>Mid</th><th>Δ</th><th>IV</th></tr></thead>';
  const tb = el('tbody');
  const T = yearsFromDte(dte);
  const used = new Set(pos.legs.filter((l) => l.strike).map((l) => l.strike));

  for (const k of strikes) {
    const ivc = ctx.ivFor(k, dte), ivp = ctx.ivFor(k, dte);
    const gc = bsGreeks(ctx.spot, k, T, ctx.rate, 0, ivc, 'call');
    const gp = bsGreeks(ctx.spot, k, T, ctx.rate, 0, ivp, 'put');
    const atm = Math.abs(k - ctx.spot) < ctx.inc / 2;
    const inUse = used.has(k);
    const tint = 'color-mix(in oklab, var(--series-1) 8%, transparent)';
    const tr = el('tr');
    if (inUse) tr.style.background = 'color-mix(in oklab, var(--series-2) 12%, transparent)';
    tr.innerHTML =
      `<td style="background:${k < ctx.spot ? tint : 'none'}">${pctS(ivc, 1)}</td>` +
      `<td style="background:${k < ctx.spot ? tint : 'none'}">${numS(gc.delta)}</td>` +
      `<td style="background:${k < ctx.spot ? tint : 'none'}">${numS(gc.price)}</td>` +
      `<td style="text-align:center;font-weight:${atm ? 650 : 500};background:${atm ? 'var(--surface-3)' : 'var(--surface-2)'}">${fmtStrike(k)}</td>` +
      `<td style="background:${k > ctx.spot ? tint : 'none'}">${numS(gp.price)}</td>` +
      `<td style="background:${k > ctx.spot ? tint : 'none'}">${numS(gp.delta)}</td>` +
      `<td style="background:${k > ctx.spot ? tint : 'none'}">${pctS(ivp, 1)}</td>`;
    tb.appendChild(tr);
  }
  t.appendChild(tb);
  scroll.appendChild(t);
  wrap.appendChild(scroll);
  return wrap;
}

/* ------------------------------------------------------------- side tables */

function legTable(pos) {
  const wrap = el('div', 'card-body flush');
  const t = el('table', 'table');
  t.innerHTML = '<thead><tr><th>Leg</th><th>Qty</th><th>Entry</th><th>Theo</th><th>IV</th><th>Δ</th><th>Θ</th><th>Vega</th></tr></thead>';
  const tb = el('tbody');
  for (const r of pos.legRows()) {
    tb.appendChild(el('tr', null,
      `<td><span class="swatch" style="background:${r.quantity > 0 ? 'var(--series-1)' : 'var(--series-2)'}"></span>${r.label}</td>` +
      `<td class="${r.quantity > 0 ? 'pos' : 'neg'}">${r.quantity > 0 ? '+' : ''}${r.quantity}</td>` +
      `<td>${numS(r.entryPrice)}</td><td>${numS(r.theo)}</td>` +
      `<td>${r.iv == null ? '—' : pctS(r.iv, 1)}</td>` +
      `<td>${numS(r.delta, 1)}</td><td>${numS(r.theta)}</td><td>${numS(r.vega)}</td>`));
  }
  t.appendChild(tb);
  wrap.appendChild(t);
  return wrap;
}

const GREEK_HELP = {
  Delta: 'Share-equivalent exposure. +100 behaves like 100 long shares.',
  Gamma: 'How fast delta changes. Positive = position improves as it moves.',
  Theta: 'Dollars per day from time decay. Positive means time pays you.',
  Vega: 'Dollars per 1 point of implied volatility.',
  Rho: 'Dollars per 1 point of interest rates.',
};

function greeksTable(a) {
  const g = a.netGreeks;
  const t = el('table', 'table');
  t.innerHTML = '<thead><tr><th>Greek</th><th>Net</th><th style="text-align:left;padding-left:16px">What it means here</th></tr></thead>';
  const tb = el('tbody');
  for (const [label, v, dp] of [['Delta', g.delta, 0], ['Gamma', g.gamma, 3],
                                ['Theta', g.theta, 2], ['Vega', g.vega, 2], ['Rho', g.rho, 2]]) {
    tb.appendChild(el('tr', null,
      `<td>${label}</td><td class="${v >= 0 ? 'pos' : 'neg'}">${numS(v, dp)}</td>` +
      `<td class="tiny muted" style="text-align:left;padding-left:16px">${GREEK_HELP[label]}</td>`));
  }
  tb.appendChild(el('tr', null,
    `<td>Dollar delta</td><td class="${g.dollarDelta >= 0 ? 'pos' : 'neg'}">${money(g.dollarDelta)}</td>` +
    `<td class="tiny muted" style="text-align:left;padding-left:16px">Notional directional exposure at the current price.</td>`));
  t.appendChild(tb);
  return t;
}

function volSensTable(a) {
  const rows = a.volSensitivity;
  const max = Math.max(...rows.map((r) => Math.abs(r.deltaPl)), 1);
  const t = el('table', 'table');
  t.innerHTML = '<thead><tr><th>IV shift</th><th>P/L now</th><th>Change</th><th style="width:110px"></th></tr></thead>';
  const tb = el('tbody');
  for (const r of rows) {
    const w = (Math.abs(r.deltaPl) / max) * 100;
    tb.appendChild(el('tr', null,
      `<td>${r.shift > 0 ? '+' : ''}${(r.shift * 100).toFixed(0)} pts</td>` +
      `<td>${money(r.pl)}</td>` +
      `<td class="${r.deltaPl >= 0 ? 'pos' : 'neg'}">${r.deltaPl >= 0 ? '+' : ''}${money(Math.abs(r.deltaPl))}</td>` +
      `<td><div style="display:flex;justify-content:${r.deltaPl >= 0 ? 'flex-start' : 'flex-end'};height:8px">` +
      `<div style="width:${w}%;background:${r.deltaPl >= 0 ? 'var(--status-good)' : 'var(--status-critical)'};border-radius:2px;opacity:.8"></div></div></td>`));
  }
  t.appendChild(tb);
  return t;
}
