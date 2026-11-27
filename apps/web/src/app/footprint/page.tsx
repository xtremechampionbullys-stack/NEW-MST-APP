'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { Shell, useUrlSymbol } from '@/components/Shell'
import { FootprintChart, type FpBar, type FpSignal, type FpTrade } from '@/components/FootprintChart'
import { money, num, pct } from '@/lib/format'

/*
 * Order-flow footprint and the POC-defence strategy.
 *
 * The page is laid out in the order a result should be read, which is not the
 * order it is tempting to read it: what data this is, then the verdict, then
 * the numbers, then the chart. A win rate seen before "this tape is synthetic"
 * or "random entries win 32% with these exits" is a number without its
 * meaning, so those come first and are not dismissible.
 *
 * Responses are typed here rather than in lib/types.ts because every shape is
 * pinned by tests/test_footprint.py against the live endpoint.
 */

type Params = {
  volume_mult: number; volume_lookback: number; poc_zone: number; require_delta: boolean
  min_stack: number; trend_filter: boolean; allow_long: boolean; allow_short: boolean
  entry_ttl_bars: number; fill_through: boolean; stop_buffer_ticks: number
  min_risk_ticks: number; target1_r: number; target2_r: number; scale_out: number
  breakeven_after_t1: boolean; max_hold_bars: number; slippage_ticks: number
  commission_per_unit: number; point_value: number; starting_equity: number
  risk_per_trade_pct: number; max_leverage: number
}

const DEFAULTS: Params = {
  volume_mult: 1.5, volume_lookback: 20, poc_zone: 0.35, require_delta: true,
  min_stack: 0, trend_filter: false, allow_long: true, allow_short: true,
  entry_ttl_bars: 3, fill_through: true, stop_buffer_ticks: 2, min_risk_ticks: 4,
  target1_r: 1, target2_r: 2, scale_out: 0.5, breakeven_after_t1: true,
  max_hold_bars: 20, slippage_ticks: 1, commission_per_unit: 0.005, point_value: 1,
  starting_equity: 25000, risk_per_trade_pct: 0.5, max_leverage: 4,
}

type Meta = {
  symbol: string; source: string; synthetic: boolean; aggressor: string; trades: number
  bars: number; row_size: number; tick_size: number; from: number; to: number; note: string
}

type Summary = {
  trades: number; win_rate: number | null; win_rate_ci: [number, number] | null
  expectancy_r: number | null; avg_win_r: number | null; avg_loss_r: number | null
  profit_factor: number | null; roi_pct: number; net_pnl: number; max_drawdown_pct: number
  avg_hold_bars: number | null; costs: number; fill_rate: number | null; t_stat: number | null
  exits: Record<string, number>; signals: number; filled: number; expired: number
  skipped_busy: number; control_percentile: number | null
  by_side: Record<string, { trades: number; win_rate: number; expectancy_r: number }>
}

type Control = {
  runs: number; expectancy_r_mean: number; expectancy_r_p05: number; expectancy_r_p95: number
  win_rate_mean: number; win_rate_p05: number; win_rate_p95: number
}

type RunResult = {
  meta: Meta; bars: FpBar[]
  window: { start: number; end: number; bars: FpBar[] }
  summary: Summary; control: Control | null
  verdict: { level: string; notes: string[] }
  signals: FpSignal[]; trades: FpTrade[]
  equity_curve: { t: number | null; equity: number }[]
}

type Slim = { trades: number; win_rate: number | null; expectancy_r: number | null; profit_factor: number | null; roi_pct: number; max_drawdown_pct: number }
type SweepRow = { variant: Record<string, number>; train: Slim; test: Slim }
type SweepResult = {
  variants_tried: number; train_bars: number; test_bars: number; min_trades: number
  best: SweepRow | null
  selection: { test_percentile: number; median_test_expectancy_r: number; degradation_r: number } | null
  table: SweepRow[]; synthetic: boolean
}

type Upload = { tape_id: string; trades: number; aggressor: string; tick_size: number; label: string }

const VERDICT: Record<string, { label: string; tone: 'warn' | 'bad' | 'good' | 'neutral' }> = {
  synthetic: { label: 'Synthetic tape — mechanics check only', tone: 'warn' },
  insufficient: { label: 'Too few trades to judge', tone: 'warn' },
  negative: { label: 'Loses after costs', tone: 'bad' },
  no_edge_over_random: { label: 'No better than random entries', tone: 'bad' },
  inconclusive: { label: 'Positive, but within noise', tone: 'warn' },
  candidate: { label: 'Candidate — needs out-of-sample confirmation', tone: 'good' },
}

const EXIT_LABEL: Record<string, string> = {
  target2: 'Target 2', stop: 'Stop', breakeven: 'Breakeven', time: 'Time stop', session_end: 'Session end',
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json' }, cache: 'no-store' })
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try { const b = await res.json(); if (b?.detail) detail = typeof b.detail === 'string' ? b.detail : JSON.stringify(b.detail) } catch { /* not json */ }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

const WINDOW = 30
const r2 = (v: number | null | undefined) => (v == null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}R`)

export default function FootprintPage() {
  const [symbol, setSymbol] = useUrlSymbol('SPY')
  const [sessions, setSessions] = useState(3)
  const [p, setP] = useState<Params>(DEFAULTS)
  const [upload, setUpload] = useState<Upload | null>(null)
  const [result, setResult] = useState<RunResult | null>(null)
  const [win, setWin] = useState<{ start: number; end: number; bars: FpBar[] } | null>(null)
  const [focus, setFocus] = useState<number | null>(null)
  const [sweep, setSweep] = useState<SweepResult | null>(null)
  const [busy, setBusy] = useState<'' | 'run' | 'sweep' | 'upload' | 'page'>('')
  const [error, setError] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const set = <K extends keyof Params>(k: K, v: Params[K]) => setP((old) => ({ ...old, [k]: v }))

  const body = useCallback(() => ({
    symbol, sessions, tape_id: upload?.tape_id ?? null,
    // With scaling off there is one target; keep the pair valid for the API.
    params: { ...p, target1_r: p.scale_out > 0 ? Math.min(p.target1_r, p.target2_r - 0.25) : p.target1_r },
  }), [symbol, sessions, upload, p])

  const run = useCallback(async () => {
    setBusy('run'); setError(null)
    try {
      const res = await call<RunResult>('/api/footprint/run', {
        method: 'POST', body: JSON.stringify({ ...body(), window: WINDOW }),
      })
      setResult(res); setWin(res.window); setFocus(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally { setBusy('') }
  }, [body])

  // First paint: run once with defaults so the page is never an empty form.
  const ran = useRef(false)
  useEffect(() => { if (!ran.current) { ran.current = true; run() } }, [run])

  const page = async (end: number, focusBar: number | null = null) => {
    if (!result) return
    const clamped = Math.max(WINDOW, Math.min(end, result.meta.bars))
    setBusy('page')
    try {
      const q = new URLSearchParams({
        symbol, sessions: String(sessions), window: String(WINDOW), end: String(clamped),
        row_size: String(result.meta.row_size),
      })
      if (upload) q.set('tape_id', upload.tape_id)
      const res = await call<{ bars: FpBar[]; start: number; end: number }>(`/api/footprint/bars?${q}`)
      setWin({ start: res.start, end: res.end, bars: res.bars }); setFocus(focusBar)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally { setBusy('') }
  }

  const runSweep = async () => {
    setBusy('sweep'); setError(null)
    try {
      setSweep(await call<SweepResult>('/api/footprint/sweep', {
        method: 'POST', body: JSON.stringify({ ...body(), sessions: Math.max(sessions, 2) }),
      }))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally { setBusy('') }
  }

  const onFile = async (file: File | undefined) => {
    if (!file) return
    setBusy('upload'); setError(null)
    try {
      const csv = await file.text()
      setUpload(await call<Upload>('/api/footprint/upload', {
        method: 'POST', body: JSON.stringify({ csv, label: file.name.slice(0, 80) }),
      }))
      setSweep(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally { setBusy(''); if (fileRef.current) fileRef.current.value = '' }
  }

  const applyVariant = (v: Record<string, number>) => setP((old) => ({ ...old, ...v }))

  const s = result?.summary
  const v = result ? VERDICT[result.verdict.level] ?? { label: result.verdict.level, tone: 'neutral' as const } : null

  return (
    <Shell symbol={symbol} onSymbol={(x) => { setSymbol(x); setUpload(null) }} actions={
      <div className="row">
        <button className="btn" onClick={runSweep} disabled={!!busy}>{busy === 'sweep' ? 'Sweeping…' : 'Walk-forward sweep'}</button>
        <button className="btn primary" onClick={run} disabled={!!busy}>{busy === 'run' ? 'Running…' : 'Run'}</button>
      </div>
    }>
      <div className="shell" style={{ gridTemplateColumns: '288px minmax(0, 1fr)' }}>
        <aside className="rail fp-rail">
          <div className="card">
            <div className="card-head"><h2>Trade tape</h2></div>
            <div className="card-body">
              {upload ? (
                <div className="field">
                  <label><span>Uploaded</span><span className="value">{upload.trades.toLocaleString()} trades</span></label>
                  <div className="help">{upload.label} · tick {upload.tick_size} · side: {upload.aggressor === 'exchange' ? 'from the file' : 'inferred (tick rule)'}</div>
                  <button className="btn ghost" onClick={() => setUpload(null)}>Use {symbol} from the data provider</button>
                </div>
              ) : (
                <Slider label="Sessions" value={sessions} min={1} max={10} step={1} onChange={setSessions}
                  format={(x) => `${x} day${x === 1 ? '' : 's'}`} />
              )}
              <input ref={fileRef} type="file" accept=".csv,text/csv,text/plain" hidden onChange={(e) => onFile(e.target.files?.[0])} />
              <button className="btn" style={{ width: '100%' }} onClick={() => fileRef.current?.click()} disabled={!!busy}>
                {busy === 'upload' ? 'Reading…' : 'Upload real trades (CSV)'}
              </button>
              <div className="field" style={{ margin: '8px 0 0' }}>
                <div className="help">Columns: time, price, size and optionally side. Headerless Binance aggTrades files work as they are.</div>
              </div>
            </div>
          </div>

          <div className="card">
            <div className="card-head"><h2>Setup</h2><span className="sub">what makes a bar a signal</span></div>
            <div className="card-body">
              <Slider label="Bar volume" value={p.volume_mult} min={1} max={4} step={0.25} onChange={(x) => set('volume_mult', x)}
                format={(x) => `≥ ${x.toFixed(2)}× median`} help={`Against the median of the previous ${p.volume_lookback} bars.`} />
              <Slider label="POC location" value={p.poc_zone} min={0.1} max={0.5} step={0.05} onChange={(x) => set('poc_zone', x)}
                format={(x) => `outer ${(x * 100).toFixed(0)}% of range`}
                help="Long: the POC sits in the bottom of the bar and price closed above it. Short: the mirror." />
              <Slider label="Stacked imbalances" value={p.min_stack} min={0} max={4} step={1} onChange={(x) => set('min_stack', x)}
                format={(x) => (x === 0 ? 'not required' : `≥ ${x} rows`)} />
              <Check label="Delta must agree with direction" checked={p.require_delta} onChange={(x) => set('require_delta', x)} />
              <Check label="Trade only with session VWAP" checked={p.trend_filter} onChange={(x) => set('trend_filter', x)} />
              <div className="row">
                <Check label="Longs" checked={p.allow_long} onChange={(x) => set('allow_long', x)} />
                <Check label="Shorts" checked={p.allow_short} onChange={(x) => set('allow_short', x)} />
              </div>
            </div>
          </div>

          <div className="card">
            <div className="card-head"><h2>Entry and exits</h2></div>
            <div className="card-body">
              <Slider label="Limit at POC rests for" value={p.entry_ttl_bars} min={1} max={10} step={1} onChange={(x) => set('entry_ttl_bars', x)}
                format={(x) => `${x} bar${x === 1 ? '' : 's'}`} />
              <Check label="Fill only when price trades through" checked={p.fill_through} onChange={(x) => set('fill_through', x)}
                help="Off assumes you are first in the queue at every price. Leave on unless you can show otherwise." />
              <Slider label="Stop beyond bar extreme" value={p.stop_buffer_ticks} min={0} max={10} step={1} onChange={(x) => set('stop_buffer_ticks', x)}
                format={(x) => `${x} ticks`} />
              <Slider label="Scale out at target 1" value={p.scale_out} min={0} max={0.75} step={0.25} onChange={(x) => set('scale_out', x)}
                format={(x) => (x === 0 ? 'off — all out at target 2' : `${(x * 100).toFixed(0)}% of position`)}
                help="Raises the win rate by construction. Whether it raises expectancy is what the control row tells you." />
              {p.scale_out > 0 && (
                <Slider label="Target 1" value={p.target1_r} min={0.5} max={3} step={0.25} onChange={(x) => set('target1_r', x)} format={(x) => `${x.toFixed(2)}R`} />
              )}
              <Slider label={p.scale_out > 0 ? 'Target 2' : 'Target'} value={p.target2_r} min={0.75} max={6} step={0.25} onChange={(x) => set('target2_r', x)} format={(x) => `${x.toFixed(2)}R`} />
              {p.scale_out > 0 && (
                <Check label="Stop to breakeven after target 1" checked={p.breakeven_after_t1} onChange={(x) => set('breakeven_after_t1', x)} />
              )}
              <Slider label="Time stop" value={p.max_hold_bars} min={2} max={120} step={1} onChange={(x) => set('max_hold_bars', x)} format={(x) => `${x} bars`} />
            </div>
          </div>

          <div className="card">
            <div className="card-head"><h2>Costs and sizing</h2></div>
            <div className="card-body">
              <Slider label="Slippage on stops and market exits" value={p.slippage_ticks} min={0} max={5} step={0.5} onChange={(x) => set('slippage_ticks', x)} format={(x) => `${x} ticks`} />
              <Slider label="Commission" value={p.commission_per_unit} min={0} max={0.02} step={0.001} onChange={(x) => set('commission_per_unit', x)} format={(x) => `$${x.toFixed(3)} / unit / side`} />
              <Slider label="Risk per trade" value={p.risk_per_trade_pct} min={0.1} max={3} step={0.1} onChange={(x) => set('risk_per_trade_pct', x)} format={(x) => `${x.toFixed(1)}% of equity`} />
              <Slider label="Account" value={p.starting_equity} min={5000} max={250000} step={5000} onChange={(x) => set('starting_equity', x)} format={(x) => money(x, 0)} />
            </div>
          </div>
        </aside>

        <main className="main">
          {error && <div className="err">{error}</div>}
          {!result && !error && <div className="skeleton" style={{ height: 420 }} />}

          {result && v && s && (<>
            <div className={`fp-verdict is-${v.tone}`}>
              <div className="fp-verdict-head">
                <strong>{v.label}</strong>
                <span>{result.meta.symbol} · {result.meta.source} · {result.meta.trades.toLocaleString()} trades · {result.meta.bars.toLocaleString()} one-minute bars · rows of {result.meta.row_size}</span>
              </div>
              <ul>
                {result.verdict.notes.map((n) => <li key={n}>{n}</li>)}
                {!result.meta.synthetic && <li>{result.meta.note}</li>}
              </ul>
            </div>

            <div className="card">
              <div className="tiles">
                <Tile k="Trades" v={String(s.trades)} n={`${s.signals} signals · ${s.fill_rate == null ? '—' : pct(s.fill_rate, 0)} filled`} />
                <Tile k="Win rate" v={pct(s.win_rate, 0)}
                  n={s.win_rate_ci ? `95% CI ${pct(s.win_rate_ci[0], 0)}–${pct(s.win_rate_ci[1], 0)}` : 'no trades'} />
                <Tile k="Expectancy" v={r2(s.expectancy_r)} tone={s.expectancy_r}
                  n={`win ${r2(s.avg_win_r)} · loss ${r2(s.avg_loss_r)}`} />
                <Tile k="Profit factor" v={num(s.profit_factor, 2)} n="gross win / gross loss" />
                <Tile k="ROI" v={`${s.roi_pct >= 0 ? '+' : ''}${s.roi_pct.toFixed(2)}%`} tone={s.roi_pct}
                  n={`${money(s.net_pnl, 0)} · costs ${money(s.costs, 0)}`} />
                <Tile k="Max drawdown" v={`${s.max_drawdown_pct.toFixed(2)}%`} n="closed-trade equity" />
                <Tile k="vs random entries" v={s.control_percentile == null ? '—' : `${s.control_percentile.toFixed(0)}th pct`}
                  n={result.control ? `random: ${r2(result.control.expectancy_r_mean)}, wins ${pct(result.control.win_rate_mean, 0)}` : 'control not run'} />
              </div>
              {result.control && s.expectancy_r != null && (
                <ControlBar value={s.expectancy_r} c={result.control} />
              )}
            </div>

            <div className="card">
              <div className="card-head">
                <div>
                  <h2>Footprint · 1 minute</h2>
                  <span className="sub">
                    {win ? `bars ${win.start + 1}–${win.end} of ${result.meta.bars} · ${new Date((win.bars[0]?.t ?? 0) * 1000).toISOString().slice(0, 10)}` : ''}
                  </span>
                </div>
                <div className="row">
                  <button className="btn" disabled={!!busy || !win || win.start === 0} onClick={() => win && page(win.end - WINDOW + 5)} aria-label="Earlier bars">◀ Earlier</button>
                  <button className="btn" disabled={!!busy || !win || win.end >= result.meta.bars} onClick={() => win && page(win.end + WINDOW - 5)} aria-label="Later bars">Later ▶</button>
                  <button className="btn" disabled={!!busy || !win || win.end >= result.meta.bars} onClick={() => page(result.meta.bars)}>Latest</button>
                </div>
              </div>
              <div className="card-body">
                {win && (
                  <FootprintChart bars={win.bars} start={win.start} rowSize={result.meta.row_size}
                    signals={result.signals} trades={result.trades} focusBar={focus} />
                )}
              </div>
            </div>

            {result.equity_curve.length > 2 && (
              <div className="card">
                <div className="card-head"><h2>Equity by closed trade</h2><span className="sub">after costs · starts at {money(p.starting_equity, 0)}</span></div>
                <div className="card-body"><Equity points={result.equity_curve.map((e) => e.equity)} /></div>
              </div>
            )}

            <div className="card">
              <div className="card-head">
                <h2>Trades</h2>
                <span className="sub">
                  {Object.entries(s.exits).map(([k, n]) => `${EXIT_LABEL[k] ?? k} ${n}`).join(' · ') || 'none'} · click a row to see it on the chart
                </span>
              </div>
              <div style={{ maxHeight: 340, overflowY: 'auto' }}>
                <table className="table">
                  <thead><tr><th>Entry (UTC)</th><th>Side</th><th>Entry</th><th>Stop</th><th>Exit</th><th>Qty</th><th>Bars</th><th>Out</th><th>P/L</th><th>R</th></tr></thead>
                  <tbody>
                    {result.trades.map((t) => (
                      <tr key={`${t.signal_bar}-${t.entry_t}`} style={{ cursor: 'pointer' }}
                        onClick={() => page(Math.min(t.signal_bar + WINDOW - 8, result.meta.bars), t.signal_bar)}>
                        <td>{new Date(t.entry_t * 1000).toISOString().slice(5, 19).replace('T', ' ')}</td>
                        <td>{t.side > 0 ? 'Long' : 'Short'}</td>
                        <td>{t.entry.toFixed(2)}</td>
                        <td>{t.stop.toFixed(2)}</td>
                        <td>{t.exit.toFixed(2)}</td>
                        <td>{t.qty}</td>
                        <td>{t.hold_bars}</td>
                        <td>{EXIT_LABEL[t.reason] ?? t.reason}</td>
                        <td className={t.pnl >= 0 ? 'pos' : 'neg'}>{money(t.pnl, 0)}</td>
                        <td className={t.r >= 0 ? 'pos' : 'neg'}>{r2(t.r)}</td>
                      </tr>
                    ))}
                    {result.trades.length === 0 && <tr><td colSpan={10} style={{ textAlign: 'left' }} className="muted">No signal filled. Loosen the setup or let the limit rest longer.</td></tr>}
                  </tbody>
                </table>
              </div>
            </div>
          </>)}

          {sweep && (
            <div className="card">
              <div className="card-head">
                <div>
                  <h2>Walk-forward sweep</h2>
                  <span className="sub">{sweep.variants_tried} variants · chosen on the first {sweep.train_bars} bars · scored on the last {sweep.test_bars}</span>
                </div>
              </div>
              <div className="card-body">
                {sweep.synthetic && <p className="fp-note">Synthetic tape: every variant is fitting noise here. This shows how the sweep reports, not which settings work.</p>}
                {sweep.best && sweep.selection ? (
                  <p className="fp-note">
                    The best variant in training made <b>{r2(sweep.best.train.expectancy_r)}</b> per trade there and <b>{r2(sweep.best.test.expectancy_r)}</b> on
                    bars it was not chosen on — a gap of {sweep.selection.degradation_r.toFixed(2)}R. Out of sample it ranks at
                    the <b>{sweep.selection.test_percentile.toFixed(0)}th percentile</b> of all variants (50 means choosing did no better than picking at random).
                  </p>
                ) : (
                  <p className="fp-note">No variant reached {sweep.min_trades} training trades. Use more sessions.</p>
                )}
                <div style={{ maxHeight: 300, overflowY: 'auto' }}>
                  <table className="table">
                    <thead><tr><th>Vol ×</th><th>POC zone</th><th>Target</th><th>Scale</th><th>TTL</th><th>Train n</th><th>Train R</th><th>Train win</th><th>Test n</th><th>Test R</th><th>Test win</th><th /></tr></thead>
                    <tbody>
                      {sweep.table.slice(0, 15).map((row, i) => (
                        <tr key={i}>
                          <td>{row.variant.volume_mult}</td>
                          <td>{(row.variant.poc_zone * 100).toFixed(0)}%</td>
                          <td>{row.variant.target2_r}R</td>
                          <td>{row.variant.scale_out ? `${row.variant.scale_out * 100}%` : 'off'}</td>
                          <td>{row.variant.entry_ttl_bars}</td>
                          <td>{row.train.trades}</td>
                          <td className={(row.train.expectancy_r ?? 0) >= 0 ? 'pos' : 'neg'}>{r2(row.train.expectancy_r)}</td>
                          <td>{pct(row.train.win_rate, 0)}</td>
                          <td>{row.test.trades}</td>
                          <td className={(row.test.expectancy_r ?? 0) >= 0 ? 'pos' : 'neg'}>{r2(row.test.expectancy_r)}</td>
                          <td>{pct(row.test.win_rate, 0)}</td>
                          <td><button className="btn ghost" onClick={() => applyVariant(row.variant)}>Load</button></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          )}
        </main>
      </div>
    </Shell>
  )
}

function Tile({ k, v, n, tone }: { k: string; v: string; n: string; tone?: number | null }) {
  const cls = tone == null ? '' : tone >= 0 ? ' pos' : ' neg'
  return <div className="tile"><div className="k">{k}</div><div className={`v${cls}`}>{v}</div><div className="n">{n}</div></div>
}

function Check({ label, checked, onChange, help }: { label: string; checked: boolean; onChange: (v: boolean) => void; help?: string }) {
  return (
    <div className="field">
      <label className="row" style={{ gap: 8, cursor: 'pointer', justifyContent: 'flex-start' }}>
        <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
        <span>{label}</span>
      </label>
      {help && <div className="help">{help}</div>}
    </div>
  )
}

function Slider({ label, value, min, max, step, onChange, format, help }: {
  label: string; value: number; min: number; max: number; step: number
  onChange: (v: number) => void; format?: (v: number) => string; help?: string
}) {
  return (
    <div className="field">
      <label><span>{label}</span><span className="value">{format ? format(value) : value}</span></label>
      <input type="range" min={min} max={max} step={step} value={value} onChange={(e) => onChange(Number(e.target.value))} aria-label={label} />
      {help && <div className="help">{help}</div>}
    </div>
  )
}

/** Where the strategy's expectancy falls against the random-entry control's 5–95% band. */
function ControlBar({ value, c }: { value: number; c: Control }) {
  const lo = Math.min(c.expectancy_r_p05, value, 0) - 0.1
  const hi = Math.max(c.expectancy_r_p95, value, 0) + 0.1
  const x = (v: number) => `${((v - lo) / (hi - lo)) * 100}%`
  return (
    <div className="fp-control">
      <div className="fp-control-label">Expectancy against {c.runs} random-entry runs using the same orders, exits and costs</div>
      <div className="fp-control-track">
        <i className="fp-control-band" style={{ left: x(c.expectancy_r_p05), width: `calc(${x(c.expectancy_r_p95)} - ${x(c.expectancy_r_p05)})` }} />
        <i className="fp-control-zero" style={{ left: x(0) }} />
        <i className="fp-control-mark" style={{ left: x(value) }} />
      </div>
      <div className="fp-control-keys">
        <span><i className="fp-key fp-key-band" /> random entries, 5th–95th percentile ({r2(c.expectancy_r_p05)} to {r2(c.expectancy_r_p95)})</span>
        <span><i className="fp-key fp-key-mark" /> this strategy ({r2(value)})</span>
        <span><i className="fp-key fp-key-zero" /> breakeven</span>
      </div>
    </div>
  )
}

function Equity({ points }: { points: number[] }) {
  const W = 900, H = 170, L = 62, R = 12, T = 10, B = 20
  const lo = Math.min(...points), hi = Math.max(...points)
  const pad = (hi - lo) * 0.1 || 1
  const y = (v: number) => T + (H - T - B) * (1 - (v - (lo - pad)) / (hi - lo + 2 * pad))
  const x = (i: number) => L + ((W - L - R) * i) / (points.length - 1)
  const d = points.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join('')
  const end = points[points.length - 1]
  return (
    <div className="chart-wrap">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Equity after each of ${points.length - 1} trades, ending at ${money(end, 0)}`}>
        {[lo, points[0], hi].filter((v, i, a) => a.indexOf(v) === i).map((v) => (
          <g key={v}>
            <line className="grid-line" x1={L} x2={W - R} y1={y(v)} y2={y(v)} />
            <text className="axis-label" x={L - 6} y={y(v) + 3.5} textAnchor="end">{money(v, 0)}</text>
          </g>
        ))}
        <path d={d} fill="none" stroke="var(--series-1)" strokeWidth={2} strokeLinejoin="round" />
        <circle cx={x(points.length - 1)} cy={y(end)} r={3.5} fill="var(--series-1)" />
        <text className="axis-label" x={L} y={H - 5}>trade 1</text>
        <text className="axis-label" x={W - R} y={H - 5} textAnchor="end">trade {points.length - 1}</text>
      </svg>
    </div>
  )
}
