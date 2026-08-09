/* ==========================================================================
   Research view: forecast, forward test, dojo.

   Unlike the Strategy Lab, nothing here is recomputed in the browser. These
   are snapshots produced by the Python engine at build time.

   That is a deliberate line, not a shortcut. The Lab's pricing math is a few
   hundred lines that can be verified against Python to the cent, and the build
   does verify it. HAR-RV, GARCH, walk-forward validation and a path-dependent
   Monte Carlo are thousands of lines of numerics; a second implementation of
   those would drift from the first, and a reader would have no way to tell
   which of the two was wrong. A snapshot cannot drift, because it is not a
   second implementation -- it is the answer the real engine gave.
   ========================================================================== */

const RESEARCH_SYMBOLS = Object.keys(DATA.research || {});

function researchTile(k, v, n, cls = '') {
  return `<div class="tile"><div class="k">${k}</div>` +
    `<div class="v ${cls}">${v}</div><div class="n">${n}</div></div>`;
}

function kvRow(label, value, cls = '') {
  return `<div class="kv"><span>${label}</span><strong class="${cls}">${value}</strong></div>`;
}

/* --------------------------------------------------------------- forecast */

function volTermChart(projections, impliedNow) {
  const W = 900, H = 230, P = { t: 16, r: 20, b: 28, l: 60 };
  const xs = projections.map((p) => p.days);
  const all = projections.flatMap((p) => [p.projected_vol, p.diffusive_vol])
    .concat(impliedNow != null ? [impliedNow] : []);
  const lo = Math.min(...all) * 0.9, hi = Math.max(...all) * 1.08;
  const iw = W - P.l - P.r, ih = H - P.t - P.b;
  const x = (d) => P.l + ((d - xs[0]) / (xs[xs.length - 1] - xs[0] || 1)) * iw;
  const y = (v) => P.t + ih - ((v - lo) / (hi - lo || 1)) * ih;

  const wrap = el('div', 'chart-wrap');
  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img',
    'aria-label': 'Projected volatility by horizon, split into diffusive and event components' });

  for (const t of ticks(lo, hi, 5)) {
    svg.appendChild(svgEl('line', { class: 'grid-line', x1: P.l, x2: W - P.r, y1: y(t), y2: y(t) }));
    const lab = svgEl('text', { class: 'axis-label', x: P.l - 8, y: y(t) + 3, 'text-anchor': 'end' });
    lab.textContent = (t * 100).toFixed(0) + '%';
    svg.appendChild(lab);
  }

  // The shaded gap between the two lines IS the earnings contribution. A gap
  // reads more directly than a stacked segment when the question is "how much
  // of this projection is the announcement".
  const up = projections.map((p, i) => `${i ? 'L' : 'M'}${x(p.days)},${y(p.projected_vol)}`).join(' ');
  const down = projections.map((p) => `L${x(p.days)},${y(p.diffusive_vol)}`).reverse().join(' ');
  svg.appendChild(svgEl('path', { d: `${up} ${down} Z`, fill: 'var(--series-2)', opacity: 0.16 }));

  if (impliedNow != null) {
    svg.appendChild(svgEl('line', { x1: P.l, x2: W - P.r, y1: y(impliedNow), y2: y(impliedNow),
      stroke: 'var(--series-4)', 'stroke-width': 1.5, 'stroke-dasharray': '5 4' }));
    const t = svgEl('text', { class: 'axis-label', x: W - P.r - 2, y: y(impliedNow) - 5,
      'text-anchor': 'end', fill: 'var(--series-4)' });
    t.textContent = `implied now ${(impliedNow * 100).toFixed(0)}%`;
    svg.appendChild(t);
  }

  svg.appendChild(svgEl('path', { d: up, fill: 'none', stroke: 'var(--series-1)', 'stroke-width': 2 }));
  svg.appendChild(svgEl('path', {
    d: projections.map((p, i) => `${i ? 'L' : 'M'}${x(p.days)},${y(p.diffusive_vol)}`).join(' '),
    fill: 'none', stroke: 'var(--series-3)', 'stroke-width': 2, 'stroke-dasharray': '6 3' }));

  for (const p of projections) {
    svg.appendChild(svgEl('circle', { cx: x(p.days), cy: y(p.projected_vol), r: 3, fill: 'var(--series-1)' }));
    const lab = svgEl('text', { class: 'axis-label', x: x(p.days), y: H - P.b + 16, 'text-anchor': 'middle' });
    lab.textContent = p.days + 'd';
    svg.appendChild(lab);
  }
  svg.appendChild(svgEl('line', { class: 'axis-line', x1: P.l, x2: W - P.r, y1: H - P.b, y2: H - P.b }));

  wrap.appendChild(svg);
  const leg = el('div', 'legend');
  leg.innerHTML =
    '<span class="item"><span class="line" style="background:var(--series-1)"></span> projected total</span>' +
    '<span class="item"><span class="line" style="background:var(--series-3)"></span> diffusive only</span>' +
    '<span class="item muted">shaded gap: earnings contribution</span>';
  wrap.appendChild(leg);
  return wrap;
}

function skillBar(os) {
  const lo = Math.min(0.35, os.accuracy - 0.05, os.base_rate - 0.05);
  const hi = Math.max(0.65, os.accuracy + 0.05, os.base_rate + 0.05);
  const place = (v) => `${((v - lo) / (hi - lo)) * 100}%`;
  const wrap = el('div', 'skill');
  wrap.innerHTML =
    `<div class="skill-track">` +
    `<div class="skill-fill" style="width:${place(os.accuracy)}"></div>` +
    `<div class="skill-band" style="left:${place(os.base_rate - 2 * os.standard_error)};` +
    `width:${((4 * os.standard_error) / (hi - lo)) * 100}%"></div>` +
    `<div class="skill-mark" style="left:${place(os.base_rate)}"></div>` +
    `</div>` +
    `<div class="skill-legend">` +
    `<span><span class="swatch" style="background:var(--series-1)"></span>` +
    `out-of-sample accuracy <strong>${pctS(os.accuracy)}</strong></span>` +
    `<span><span class="swatch" style="background:var(--text-secondary)"></span>` +
    `base rate <strong>${pctS(os.base_rate)}</strong></span>` +
    `<span class="muted">shaded: ±2 standard errors around the base rate</span>` +
    `</div>`;
  return wrap;
}

function renderForecast(r) {
  const page = el('div', 'page');
  const vol = r.volatility, dir = r.direction, cross = r.cross_asset;

  const vcard = el('div', 'card');
  vcard.appendChild(el('div', 'card-head',
    `<div><h2>Volatility term projection · ${vol.symbol}</h2>` +
    `<span class="sub">${vol.bars} daily bars through ${vol.as_of}</span></div>`));
  const vb = el('div', 'card-body flush');
  vb.appendChild(volTermChart(vol.projections, vol.implied_now));
  vcard.appendChild(vb);

  const tb = el('div', 'card-body flush');
  tb.innerHTML =
    '<table class="table"><thead><tr><th>Horizon</th><th>Date</th><th>Projected</th>' +
    '<th>Diffusive</th><th>Events</th><th>Implied now</th><th>Implied − projected</th></tr></thead><tbody>' +
    vol.projections.map((p) =>
      `<tr><td>${p.days}d</td><td>${p.date}</td><td>${pctS(p.projected_vol)}</td>` +
      `<td>${pctS(p.diffusive_vol)}</td><td>${p.events_in_window || '—'}</td>` +
      `<td>${p.implied_now != null ? pctS(p.implied_now) : '—'}</td>` +
      `<td class="${(p.implied_minus_projected ?? 0) >= 0 ? 'pos' : 'neg'}">` +
      `${p.implied_minus_projected != null ? pctS(p.implied_minus_projected) : '—'}</td></tr>`).join('') +
    '</tbody></table>';
  vcard.appendChild(tb);
  vcard.appendChild(el('div', 'card-body muted tiny', vol.note));
  page.appendChild(vcard);

  const grid = el('div', 'grid-2');

  const mcard = el('div', 'card');
  mcard.appendChild(el('div', 'card-head', '<h2>Volatility models</h2>'));
  const har = vol.har || vol.har_short_horizon;
  const mb = el('div', 'card-body');
  if (har) {
    mb.innerHTML =
      kvRow(`HAR ${har.horizon_days}-day forecast`, pctS(har.forecast_vol)) +
      kvRow('Out-of-sample R²', numS(har.out_of_sample_r2, 3),
        (har.out_of_sample_r2 ?? 0) > 0.05 ? 'pos' : '') +
      kvRow('In-sample R²', numS(har.in_sample_r2, 3)) +
      `<p class="tiny muted" style="margin-top:8px">${har.note}</p>`;
  }
  if (vol.garch) {
    mb.innerHTML += '<div class="rule"></div>' +
      kvRow('GARCH persistence', numS(vol.garch.persistence, 3)) +
      kvRow('Shock half-life', vol.garch.half_life_days != null
        ? `${numS(vol.garch.half_life_days, 1)} days` : '—') +
      kvRow('Long-run level', pctS(vol.garch.long_run_vol)) +
      kvRow('Current level', pctS(vol.garch.current_vol));
  }
  mcard.appendChild(mb);
  grid.appendChild(mcard);

  const dcard = el('div', 'card');
  dcard.appendChild(el('div', 'card-head',
    `<div><h2>Directional model</h2><span class="sub">${dir.horizon_days}-day horizon, ` +
    `walk-forward validated</span></div>`));
  const dt = el('div', 'tiles');
  dt.innerHTML =
    researchTile('Model says', pctS(dir.probability_up), 'probability of a higher close') +
    researchTile('Trustworthy', dir.trustworthy ? 'Yes' : 'No',
      'by its own out-of-sample score', dir.trustworthy ? 'pos' : 'neg');
  dcard.appendChild(dt);
  const db = el('div', 'card-body');
  db.appendChild(skillBar(dir.out_of_sample));
  const verdict = el('p', `verdict ${dir.trustworthy ? 'ok' : 'null'}`, dir.verdict);
  db.appendChild(verdict);
  const dtiles = el('div', 'tiles');
  dtiles.style.marginTop = '12px';
  dtiles.innerHTML =
    researchTile('Edge', pctS(dir.out_of_sample.edge), 'accuracy − base rate',
      dir.out_of_sample.edge > 0 ? 'pos' : 'neg') +
    researchTile('AUC', numS(dir.out_of_sample.auc, 3), '0.5 = no ranking ability') +
    researchTile('Brier', numS(dir.out_of_sample.brier, 4),
      `vs ${numS(dir.out_of_sample.brier_base_rate, 4)} for the base rate`) +
    researchTile('Samples', dir.out_of_sample.samples, 'out-of-sample predictions');
  db.appendChild(dtiles);
  dcard.appendChild(db);
  grid.appendChild(dcard);
  page.appendChild(grid);

  const b = cross.benchmark, p = cross.propagation;
  const ccard = el('div', 'card');
  ccard.appendChild(el('div', 'card-head',
    `<div><h2>Index propagation</h2><span class="sub">how much of ${b.symbol} is the market, ` +
    `and how much is the company</span></div>`));
  const ct = el('div', 'tiles');
  ct.innerHTML =
    researchTile(`Beta to ${b.benchmark}`, numS(b.beta), `${b.observations} observations`) +
    researchTile('R²', numS(b.r_squared), 'variance explained by the index') +
    researchTile('Total volatility', pctS(b.total_vol, 0), 'annualized') +
    researchTile('Systematic', pctS(b.systematic_vol, 0), 'hedgeable with the index') +
    researchTile('Idiosyncratic', pctS(b.idiosyncratic_vol, 0), 'the part that is this company');
  ccard.appendChild(ct);
  ccard.appendChild(el('div', 'card-body',
    `<p style="margin:0">A <strong>${pctS(p.benchmark_move, 0)}</strong> move in ${b.benchmark} implies ` +
    `<strong class="${p.expected_stock_move >= 0 ? 'pos' : 'neg'}">${pctS(p.expected_stock_move)}</strong> ` +
    `in ${b.symbol}, with a 68% range of ${pctS(p.range_68[0])} to ${pctS(p.range_68[1])}.</p>` +
    `<p class="tiny muted" style="margin-top:8px">${p.note}</p>`));
  page.appendChild(ccard);
  return page;
}

/* ---------------------------------------------------------------- forward */

function fanChart(days, bands) {
  const W = 900, H = 260, P = { t: 16, r: 20, b: 28, l: 60 };
  const all = Object.values(bands).flat();
  const lo = Math.min(...all), hi = Math.max(...all);
  const pad = (hi - lo) * 0.06 || 1;
  const yLo = lo - pad, yHi = hi + pad;
  const iw = W - P.l - P.r, ih = H - P.t - P.b;
  const x = (d) => P.l + ((d - days[0]) / (days[days.length - 1] - days[0] || 1)) * iw;
  const y = (v) => P.t + ih - ((v - yLo) / (yHi - yLo || 1)) * ih;

  const wrap = el('div', 'chart-wrap');
  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img',
    'aria-label': 'Simulated price paths as percentile bands' });
  for (const t of ticks(yLo, yHi, 5)) {
    svg.appendChild(svgEl('line', { class: 'grid-line', x1: P.l, x2: W - P.r, y1: y(t), y2: y(t) }));
    const lab = svgEl('text', { class: 'axis-label', x: P.l - 8, y: y(t) + 3, 'text-anchor': 'end' });
    lab.textContent = t.toFixed(0);
    svg.appendChild(lab);
  }
  const area = (lower, upper) =>
    upper.map((v, i) => `${i ? 'L' : 'M'}${x(days[i])},${y(v)}`).join(' ') + ' ' +
    lower.map((v, i) => `L${x(days[i])},${y(v)}`).reverse().join(' ') + ' Z';

  // Sequential ramp, not categorical slots: these bands are an ordered
  // quantity and an ordered colour scale says so.
  svg.appendChild(svgEl('path', { d: area(bands['5'], bands['95']), fill: 'var(--seq-600)', opacity: 0.30 }));
  svg.appendChild(svgEl('path', { d: area(bands['25'], bands['75']), fill: 'var(--seq-450)', opacity: 0.45 }));
  svg.appendChild(svgEl('path', {
    d: bands['50'].map((v, i) => `${i ? 'L' : 'M'}${x(days[i])},${y(v)}`).join(' '),
    fill: 'none', stroke: 'var(--seq-250)', 'stroke-width': 2 }));

  svg.appendChild(svgEl('line', { class: 'axis-line', x1: P.l, x2: W - P.r, y1: H - P.b, y2: H - P.b }));
  const step = Math.max(1, Math.floor(days.length / 6));
  days.forEach((d, i) => {
    if (i % step) return;
    const lab = svgEl('text', { class: 'axis-label', x: x(d), y: H - P.b + 16, 'text-anchor': 'middle' });
    lab.textContent = d + 'd';
    svg.appendChild(lab);
  });
  wrap.appendChild(svg);
  const leg = el('div', 'legend');
  leg.innerHTML =
    '<span class="item"><span class="line" style="background:var(--seq-250)"></span> median</span>' +
    '<span class="item"><span class="swatch" style="background:var(--seq-450)"></span> 25–75th</span>' +
    '<span class="item"><span class="swatch" style="background:var(--seq-600)"></span> 5–95th</span>';
  wrap.appendChild(leg);
  return wrap;
}

function outcomeRibbon(pd) {
  const segs = [
    ['Profit target', pd.hit_profit_target, 'var(--status-good)'],
    ['Stop loss', pd.hit_stop_loss, 'var(--status-critical)'],
    ['Held to exit', pd.closed_on_time, 'var(--seq-450)'],
  ];
  const wrap = el('div', 'card-body');
  wrap.innerHTML =
    `<div class="ribbon" role="img" aria-label="${segs.map(([k, v]) => `${k} ${pctS(v)}`).join(', ')}">` +
    segs.map(([k, v, c]) => `<div style="width:${v * 100}%;background:${c}" title="${k}"></div>`).join('') +
    '</div><div class="legend" style="padding:10px 0 0">' +
    segs.map(([k, v, c]) =>
      `<span class="item"><span class="swatch" style="background:${c}"></span>${k} <strong>${pctS(v)}</strong></span>`
    ).join('') + '</div>';
  return wrap;
}

function renderForward(r) {
  const f = r.forward, o = f.outcomes, pd = f.path_dependent;
  const page = el('div', 'page');

  const banner = el('div', 'banner');
  banner.innerHTML = `<span aria-hidden>▲</span><span><strong>Volatility input:</strong> ${f.volatility_source}</span>`;
  page.appendChild(banner);

  const card = el('div', 'card');
  card.appendChild(el('div', 'card-head',
    `<div><h2>Forward test · ${f.symbol} short strangle</h2>` +
    `<span class="sub">${f.paths.toLocaleString()} paths over ${f.horizon_days} days at ` +
    `${pctS(f.base_vol)} vol · 50% profit target · 2× stop · close at 21 DTE</span></div>`));
  const tiles = el('div', 'tiles');
  tiles.innerHTML =
    researchTile('Win rate', pctS(o.win_rate), 'across all paths') +
    researchTile('Mean P/L', money(o.mean_pl), `median ${money(o.median_pl)}`,
      o.mean_pl >= 0 ? 'pos' : 'neg') +
    researchTile('5th percentile', money(o.p05), '1 in 20 does worse', 'neg') +
    researchTile('Worst path', money(o.worst), `of ${f.paths.toLocaleString()} simulated`, 'neg') +
    researchTile('Avg hold', `${numS(pd.mean_days_held, 1)}d`, `median ${numS(pd.median_days_held, 0)}d`) +
    researchTile('Drawdown on winners', money(pd.mean_max_adverse_on_winners),
      'how far winners go underwater first', 'neg');
  card.appendChild(tiles);
  card.appendChild(outcomeRibbon(pd));
  page.appendChild(card);

  const grid = el('div', 'grid-2');
  const pc = el('div', 'card');
  pc.appendChild(el('div', 'card-head', '<h2>Simulated price paths</h2><span class="sub">percentile bands</span>'));
  const pb = el('div', 'card-body flush');
  pb.appendChild(fanChart(f.fan_chart.days, f.fan_chart.bands));
  pc.appendChild(pb);
  grid.appendChild(pc);

  const hc = el('div', 'card');
  hc.appendChild(el('div', 'card-head',
    `<h2>P/L at exit</h2><span class="sub">expected win ${money(o.expected_win)} · ` +
    `expected loss ${money(o.expected_loss)}</span>`));
  const hb = el('div', 'card-body flush');
  hb.appendChild(histogram(f.pl_histogram));
  if (f.pl_histogram.below || f.pl_histogram.above) {
    const note = el('div', 'legend');
    note.style.paddingBottom = '10px';
    note.innerHTML =
      (f.pl_histogram.below ? `<span class="item neg">${f.pl_histogram.below} beyond ` +
        `${money(f.pl_histogram.edges[0])} — worst ${money(f.pl_histogram.min)}</span>` : '') +
      (f.pl_histogram.above ? `<span class="item pos">${f.pl_histogram.above} beyond ` +
        `${money(f.pl_histogram.edges[f.pl_histogram.edges.length - 1])} — best ` +
        `${money(f.pl_histogram.max)}</span>` : '') +
      '<span class="item muted">off the chart so the rest is legible, not to hide them</span>';
    hb.appendChild(note);
  }
  hc.appendChild(hb);
  grid.appendChild(hc);
  page.appendChild(grid);

  page.appendChild(el('div', 'card', `<div class="card-body tiny muted">${f.note}</div>`));
  return page;
}

/* ------------------------------------------------------------------- dojo */

function renderDojo(r) {
  const d = r.dojo, v = d.validation, s = d.selected;
  const page = el('div', 'page');

  const banner = el('div', `banner ${v.selection_beat_random ? '' : 'warn'}`);
  banner.innerHTML = `<span aria-hidden>${v.selection_beat_random ? '✓' : '▲'}</span><span>${d.verdict}</span>`;
  page.appendChild(banner);

  const card = el('div', 'card');
  card.appendChild(el('div', 'card-head',
    `<div><h2>Validation · ${d.symbol}</h2><span class="sub">` +
    `train ${d.train_window.start} → ${d.train_window.end} · ` +
    `test ${d.test_window.start} → ${d.test_window.end}</span></div>`));
  const tiles = el('div', 'tiles');
  tiles.innerHTML =
    researchTile('Out-of-sample percentile', numS(v.out_of_sample_percentile, 0),
      'where the training winner landed on test', v.selection_beat_random ? 'pos' : 'neg') +
    researchTile('Beat random pick', v.selection_beat_random ? 'Yes' : 'No',
      'the decisive test', v.selection_beat_random ? 'pos' : 'neg') +
    researchTile('Beat the baseline', v.selection_beat_baseline ? 'Yes' : 'No',
      'vs the rules you started with', v.selection_beat_baseline ? 'pos' : 'neg') +
    researchTile('Degradation', numS(v.degradation), 'train score − test score') +
    researchTile('Variants tried', d.variants_tried, `of ${d.search_space_size.toLocaleString()} possible`);
  card.appendChild(tiles);
  page.appendChild(card);

  const grid = el('div', 'grid-2');
  const sel = el('div', 'card');
  sel.appendChild(el('div', 'card-head', '<h2>Selected rule set</h2>'));
  const labels = {
    profit_target_pct: ['Profit target', (x) => (x ? pctS(x, 0) : 'off')],
    stop_loss_pct: ['Stop loss', (x) => (x ? `${numS(x)}× basis` : 'off')],
    close_at_dte: ['Close at DTE', (x) => (x ? `${x} days left` : 'off')],
    target_dte: ['Entry DTE', (x) => `${x} DTE`],
    delta: ['Short delta', (x) => numS(x)],
    entry_every_days: ['Entry cadence', (x) => `every ${x}d`],
  };
  sel.appendChild(el('div', 'card-body',
    Object.entries(s.variant).map(([k, val]) => {
      const [label, fmt] = labels[k] || [k.replace(/_/g, ' '), (x) => numS(x)];
      return kvRow(label, fmt(val));
    }).join('')));
  grid.appendChild(sel);

  const cmp = el('div', 'card');
  cmp.appendChild(el('div', 'card-head',
    '<h2>In sample vs out of sample</h2><span class="sub">the same rule set, two windows</span>'));
  const rows = [
    ['Trades', 'trades', (x) => (x == null ? '—' : x)],
    ['Win rate', 'win_rate', (x) => pctS(x)],
    ['Total P/L', 'total_pl', (x) => money(x)],
    ['Max drawdown', 'max_drawdown', (x) => money(x)],
    ['Sharpe', 'sharpe', (x) => numS(x)],
    ['Profit factor', 'profit_factor', (x) => numS(x)],
  ];
  cmp.appendChild(el('div', 'card-body flush',
    '<table class="table"><thead><tr><th>Metric</th><th>Train</th><th>Test</th>' +
    '<th>Baseline (test)</th></tr></thead><tbody>' +
    rows.map(([label, key, fmt]) =>
      `<tr><td>${label}</td><td>${fmt(s.train_metrics[key])}</td>` +
      `<td>${fmt(s.test_metrics[key])}</td>` +
      `<td class="muted">${d.baseline.test_metrics ? fmt(d.baseline.test_metrics[key]) : '—'}</td></tr>`
    ).join('') + '</tbody></table>'));
  grid.appendChild(cmp);
  page.appendChild(grid);

  const lb = el('div', 'card');
  lb.appendChild(el('div', 'card-head',
    '<h2>Leaderboard</h2><span class="sub">ranked by TRAINING score — the test column is what ' +
    'actually happened next</span>'));
  const scroll = el('div', 'scroll');
  scroll.style.maxHeight = '420px';
  scroll.innerHTML =
    '<table class="table"><thead><tr><th>#</th><th>Target</th><th>Stop</th><th>Close DTE</th>' +
    '<th>Entry DTE</th><th>Delta</th><th>Cadence</th><th>Train P/L</th><th>Train score</th>' +
    '<th>Test P/L</th><th>Test trades</th><th>Test score</th></tr></thead><tbody>' +
    d.leaderboard.map((row, i) =>
      `<tr${i === 0 ? ' style="background:var(--surface-2)"' : ''}><td>${i + 1}</td>` +
      `<td>${row.profit_target_pct ? pctS(row.profit_target_pct, 0) : 'off'}</td>` +
      `<td>${row.stop_loss_pct ? numS(row.stop_loss_pct) + '×' : 'off'}</td>` +
      `<td>${row.close_at_dte || 'off'}</td><td>${row.target_dte}</td>` +
      `<td>${numS(row.delta)}</td><td>${row.entry_every_days}d</td>` +
      `<td class="${row.train_pl >= 0 ? 'pos' : 'neg'}">${money(row.train_pl)}</td>` +
      `<td>${numS(row.train_score)}</td>` +
      `<td class="${row.test_pl >= 0 ? 'pos' : 'neg'}">${money(row.test_pl)}</td>` +
      `<td>${row.test_trades}</td><td>${numS(row.test_score)}</td></tr>`).join('') +
    '</tbody></table>';
  const lbBody = el('div', 'card-body flush');
  lbBody.appendChild(scroll);
  lb.appendChild(lbBody);
  lb.appendChild(el('div', 'card-body tiny muted', d.caveat));
  page.appendChild(lb);
  return page;
}

/* ------------------------------------------------------------------ shell */

function renderResearch() {
  const sym = state.researchSymbol || RESEARCH_SYMBOLS[0];
  const r = DATA.research[sym];
  const page = el('div', 'page');

  const head = el('div', 'card');
  const hb = el('div', 'card-head');
  hb.style.gap = '12px';
  const tabs = el('div', 'tabs');
  for (const [id, label] of [['forecast', 'Forecast'], ['forward', 'Forward test'], ['dojo', 'Dojo']]) {
    const b = el('button', 'tab', label);
    b.setAttribute('aria-selected', String((state.researchTab || 'forecast') === id));
    b.onclick = () => { state.researchTab = id; render(); };
    tabs.appendChild(b);
  }
  hb.appendChild(tabs);

  const syms = el('div', 'row');
  syms.style.cssText = 'gap:4px;margin-left:auto';
  for (const s of RESEARCH_SYMBOLS) {
    const b = el('button', 'btn ghost tiny', s);
    b.style.padding = '3px 8px';
    if (s === sym) b.setAttribute('aria-pressed', 'true');
    b.onclick = () => { state.researchSymbol = s; render(); };
    syms.appendChild(b);
  }
  hb.appendChild(syms);
  head.appendChild(hb);
  page.appendChild(head);

  const tab = state.researchTab || 'forecast';
  page.appendChild(
    tab === 'forecast' ? renderForecast(r)
      : tab === 'forward' ? renderForward(r)
        : renderDojo(r));
  return page;
}
