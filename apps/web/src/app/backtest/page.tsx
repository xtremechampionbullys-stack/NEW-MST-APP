'use client'

import { useEffect, useState } from 'react'
import { Shell } from '@/components/Shell'
import { DrawdownChart, EquityCurve, Histogram } from '@/components/charts'
import { api } from '@/lib/api'
import { money, num, pct } from '@/lib/format'
import type { BacktestResult, BacktestRunSummary, TemplateMeta } from '@/lib/types'

const PRESETS = [
  { id: 'short_strangle', label: 'Short strangle', params: { delta: 0.16 } },
  { id: 'iron_condor', label: 'Iron condor', params: { delta: 0.16, width: 5 } },
  { id: 'bull_put_spread', label: 'Bull put spread', params: { delta: 0.25, width: 5 } },
  { id: 'short_put', label: 'Cash-secured put', params: { delta: 0.25 } },
  { id: 'covered_call', label: 'Covered call', params: { delta: 0.30 } },
  { id: 'wheel', label: 'The wheel', params: { delta: 0.25 } },
  { id: 'long_call', label: 'Long call', params: { delta: 0.35 } },
  { id: 'pmcc', label: 'Poor man’s covered call', params: {} },
  { id: 'call_calendar', label: 'Call calendar', params: {} },
]

export default function BacktestPage() {
  const [symbol, setSymbol] = useState('NVDA')
  const [templateId, setTemplateId] = useState('short_strangle')
  const [targetDte, setTargetDte] = useState(45)
  const [cadence, setCadence] = useState(7)
  const [profitTarget, setProfitTarget] = useState(0.5)
  const [stopLoss, setStopLoss] = useState(2.0)
  const [closeAtDte, setCloseAtDte] = useState(21)
  const [contracts, setContracts] = useState(1)
  const [maxConcurrent, setMaxConcurrent] = useState(1)
  const [skipEarnings, setSkipEarnings] = useState(false)
  const [vrp, setVrp] = useState(1.15)
  const [lookback, setLookback] = useState(730)

  const [templates, setTemplates] = useState<TemplateMeta[]>([])
  const [result, setResult] = useState<BacktestResult | null>(null)
  const [runs, setRuns] = useState<BacktestRunSummary[]>([])
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.templates().then((c) => setTemplates(c.templates)).catch(() => {})
    refreshRuns()
  }, [])

  const refreshRuns = () => {
    api.listBacktests().then((r) => setRuns(r.runs)).catch(() => {})
  }

  const run = async () => {
    setRunning(true)
    setError(null)
    try {
      const preset = PRESETS.find((p) => p.id === templateId)
      const res = await api.runBacktest({
        symbol,
        lookback_days: lookback,
        rules: {
          template_id: templateId,
          params: preset?.params ?? {},
          target_dte: targetDte,
          entry_every_days: cadence,
          contracts,
          max_concurrent: maxConcurrent,
          profit_target_pct: profitTarget > 0 ? profitTarget : null,
          stop_loss_pct: stopLoss > 0 ? stopLoss : null,
          close_at_dte: closeAtDte > 0 ? closeAtDte : null,
          skip_earnings: skipEarnings,
          variance_risk_premium: vrp,
        },
      })
      setResult(res)
      refreshRuns()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setRunning(false)
    }
  }

  const loadRun = async (id: string) => {
    try {
      const res = await api.getBacktest(id)
      setResult(res)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const m = result?.metrics

  return (
    <Shell symbol={symbol} onSymbol={setSymbol} actions={<><div className="row" style={{ gap: 6, marginLeft: 'auto' }}>
     <input
      className="input"
      style={{ width: 100, textTransform: 'uppercase' }}
      value={symbol}
      onChange={(e) => setSymbol(e.target.value.toUpperCase())}
      aria-label="Symbol"
     />
     <button className="btn primary" onClick={run} disabled={running}>
      {running ? 'Running…' : 'Run backtest'}
     </button>
    </div></>}>

      <div className="shell" style={{ gridTemplateColumns: '288px minmax(0, 1fr)' }}>
        <aside className="rail">
          <div className="card">
            <div className="card-head"><h2>Strategy</h2></div>
            <div className="card-body">
              <div className="field">
                <label htmlFor="tpl">Structure</label>
                <select id="tpl" className="select" value={templateId} onChange={(e) => setTemplateId(e.target.value)}>
                  {PRESETS.map((p) => (
                    <option key={p.id} value={p.id}>{p.label}</option>
                  ))}
                  <optgroup label="All templates">
                    {templates
                      .filter((t) => !PRESETS.some((p) => p.id === t.id))
                      .map((t) => (
                        <option key={t.id} value={t.id}>{t.name}</option>
                      ))}
                  </optgroup>
                </select>
              </div>

              <Slider label="Target DTE at entry" value={targetDte} min={7} max={365} step={1} onChange={setTargetDte} />
              <Slider label="Enter every N days" value={cadence} min={1} max={60} step={1} onChange={setCadence} />
              <Slider label="Contracts" value={contracts} min={1} max={20} step={1} onChange={setContracts} />
              <Slider label="Max concurrent" value={maxConcurrent} min={1} max={10} step={1} onChange={setMaxConcurrent} />
            </div>
          </div>

          <div className="card">
            <div className="card-head"><h2>Management</h2></div>
            <div className="card-body">
              <Slider
                label="Profit target"
                value={profitTarget}
                min={0}
                max={3}
                step={0.05}
                format={(v) => (v === 0 ? 'off' : `${(v * 100).toFixed(0)}% of basis`)}
                onChange={setProfitTarget}
                help="Fraction of the credit collected or debit paid. Zero disables it."
              />
              <Slider
                label="Stop loss"
                value={stopLoss}
                min={0}
                max={6}
                step={0.25}
                format={(v) => (v === 0 ? 'off' : `${v.toFixed(2)}× basis`)}
                onChange={setStopLoss}
              />
              <Slider
                label="Close at DTE"
                value={closeAtDte}
                min={0}
                max={90}
                step={1}
                format={(v) => (v === 0 ? 'off' : `${v} days left`)}
                onChange={setCloseAtDte}
                help="Must be well below the entry DTE, or every trade exits on day one."
              />
              <div className="field">
                <label className="row" style={{ gap: 8, cursor: 'pointer' }}>
                  <input type="checkbox" checked={skipEarnings} onChange={(e) => setSkipEarnings(e.target.checked)} />
                  <span>Skip entries that span earnings</span>
                </label>
              </div>
            </div>
          </div>

          <div className="card">
            <div className="card-head"><h2>Model assumptions</h2></div>
            <div className="card-body">
              <Slider
                label="Variance risk premium"
                value={vrp}
                min={0.8}
                max={2.0}
                step={0.05}
                format={(v) => `${v.toFixed(2)}× realized`}
                onChange={setVrp}
                help="How far implied volatility sits above realized when prices are modelled. This is the single most consequential assumption here — raise it and every short-premium result improves."
              />
              <Slider
                label="History"
                value={lookback}
                min={250}
                max={2000}
                step={50}
                format={(v) => `${v} days`}
                onChange={setLookback}
              />
            </div>
          </div>

          {runs.length > 0 && (
            <div className="card">
              <div className="card-head">
                <h2>Saved runs</h2>
                <span className="sub">{runs.length}</span>
              </div>
              <div style={{ maxHeight: 220, overflowY: 'auto' }}>
                {runs.map((r) => (
                  <button key={r.id} className="strat" onClick={() => loadRun(r.id)} style={{ width: '100%' }}>
                    <span className="name">
                      {r.symbol} · {r.template_id}
                    </span>
                    <span className="desc">
                      {r.metrics?.trades ?? 0} trades · {money(r.metrics?.total_pl, 0)} ·{' '}
                      {r.is_historical_pricing ? 'historical' : 'modelled'}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </aside>

        <main className="main">
          {error && <div className="err">{error}</div>}

          {result && (
            <div
              className="banner"
              style={{
                background: result.is_historical_pricing
                  ? 'color-mix(in oklab, var(--status-good) 12%, var(--surface-1))'
                  : undefined,
              }}
            >
              <span aria-hidden>{result.is_historical_pricing ? '✓' : '▲'}</span>
              <span>
                <strong>
                  {result.is_historical_pricing ? 'Historical quotes.' : 'Modelled prices — rules test, not a P&L record.'}
                </strong>{' '}
                {result.caveat}
              </span>
            </div>
          )}

          {result?.warnings?.map((w) => (
            <div className="err" key={w}>{w}</div>
          ))}

          {m && result && (
            <div className="card">
              <div className="card-head">
                <div>
                  <h2>{result.template_name} · {result.symbol}</h2>
                  <span className="sub">
                    {result.start} → {result.end} · {result.trading_days} trading days
                    {result.skipped_for_earnings > 0 && ` · ${result.skipped_for_earnings} entries skipped for earnings`}
                  </span>
                </div>
              </div>
              <div className="tiles">
                <div className="tile">
                  <div className="k">Total P/L</div>
                  <div className={`v ${m.total_pl >= 0 ? 'pos' : 'neg'}`}>{money(m.total_pl, 0)}</div>
                  <div className="n">{m.trades} trades</div>
                </div>
                <div className="tile">
                  <div className="k">Win rate</div>
                  <div className="v">{pct(m.win_rate, 0)}</div>
                  <div className="n">{m.wins}W / {m.losses}L</div>
                </div>
                <div className="tile">
                  <div className="k">Profit factor</div>
                  <div className="v">{m.profit_factor != null ? num(m.profit_factor, 2) : '—'}</div>
                  <div className="n">gross win / gross loss</div>
                </div>
                <div className="tile">
                  <div className="k">Expectancy</div>
                  <div className={`v ${(m.expectancy ?? 0) >= 0 ? 'pos' : 'neg'}`}>{money(m.expectancy, 0)}</div>
                  <div className="n">per trade</div>
                </div>
                <div className="tile">
                  <div className="k">Max drawdown</div>
                  <div className="v neg">{money(m.max_drawdown, 0)}</div>
                  <div className="n">on the equity curve</div>
                </div>
                <div className="tile">
                  <div className="k">Sharpe</div>
                  <div className="v">{m.sharpe != null ? num(m.sharpe, 2) : '—'}</div>
                  <div className="n">annualized, daily equity</div>
                </div>
                <div className="tile">
                  <div className="k">Avg hold</div>
                  <div className="v">{m.average_days_held != null ? `${m.average_days_held}d` : '—'}</div>
                  <div className="n">{m.exposure_pct != null ? `${pct(m.exposure_pct, 0)} exposed` : ''}</div>
                </div>
                <div className="tile">
                  <div className="k">Worst streak</div>
                  <div className="v">{m.max_consecutive_losses}</div>
                  <div className="n">consecutive losses</div>
                </div>
              </div>
            </div>
          )}

          {result && result.equity_curve.length > 1 && (
            <div className="card">
              <div className="card-head">
                <h2>Equity and drawdown</h2>
                <span className="sub">realized plus open positions, marked daily</span>
              </div>
              <div className="card-body flush">
                <EquityCurve points={result.equity_curve} />
                <div style={{ borderTop: '1px solid var(--border)' }}>
                  <DrawdownChart points={result.equity_curve} />
                </div>
              </div>
            </div>
          )}

          {result?.distribution && (
            <div className="card">
              <div className="card-head">
                <h2>Trade distribution</h2>
                <span className="sub">
                  largest win {money(m?.largest_win, 0)} · largest loss {money(m?.largest_loss, 0)}
                </span>
              </div>
              <div className="card-body flush">
                <Histogram edges={result.distribution.edges} counts={result.distribution.counts} />
              </div>
            </div>
          )}

          {result && result.trades.length > 0 && (
            <div className="card">
              <div className="card-head">
                <h2>Trade ledger</h2>
                <span className="sub">{result.trades.length} closed</span>
              </div>
              <div className="card-body flush">
                <div className="scroll" style={{ maxHeight: 420 }}>
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Entry</th><th>Exit</th><th>Held</th><th>Spot in</th><th>Spot out</th>
                        <th>Basis</th><th>P/L</th><th>Worst</th>
                        <th style={{ textAlign: 'left', paddingLeft: 14 }}>Reason</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.trades.slice().reverse().map((t, i) => (
                        <tr key={`${t.entry_date}-${i}`}>
                          <td>{t.entry_date}</td>
                          <td>{t.exit_date}</td>
                          <td>{t.days_held}d</td>
                          <td>{t.entry_spot.toFixed(2)}</td>
                          <td>{t.exit_spot.toFixed(2)}</td>
                          <td className={t.entry_basis < 0 ? 'pos' : ''}>
                            {t.entry_basis < 0 ? `+${money(-t.entry_basis, 0)}` : `-${money(t.entry_basis, 0)}`}
                          </td>
                          <td className={t.pl >= 0 ? 'pos' : 'neg'}>{money(t.pl, 0)}</td>
                          <td className="neg tiny">{money(t.max_adverse, 0)}</td>
                          <td className="tiny muted" style={{ textAlign: 'left', paddingLeft: 14 }}>
                            {t.exit_reason}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          )}

          {!result && !running && (
            <div className="card">
              <div className="card-body muted">
                Configure a rule set and run it. Results are stored, so you can compare runs
                afterwards — and every run records whether it was priced from historical quotes
                or from the model, because the two are not comparable.
              </div>
            </div>
          )}
        </main>
      </div>
    </Shell>
  )
}

function Slider({
  label, value, min, max, step, onChange, format, help,
}: {
  label: string
  value: number
  min: number
  max: number
  step: number
  onChange: (v: number) => void
  format?: (v: number) => string
  help?: string
}) {
  return (
    <div className="field">
      <label>
        <span>{label}</span>
        <span className="value">{format ? format(value) : value}</span>
      </label>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        aria-label={label}
      />
      {help && <div className="help">{help}</div>}
    </div>
  )
}
