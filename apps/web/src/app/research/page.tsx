'use client'

import { useCallback, useEffect, useState } from 'react'
import { Shell } from '@/components/Shell'
import { Histogram } from '@/components/charts'
import {
  CalibrationChart,
  FanChart,
  OutcomeRibbon,
  SkillReport,
  VolTermChart,
} from '@/components/research'
import { TvWidget } from '@/components/TradingView'
import { api } from '@/lib/api'
import { money, num, pct } from '@/lib/format'
import type {
  CalibrationResponse,
  CrossAssetResponse,
  DirectionResponse,
  DojoResponse,
  ForwardResponse,
  VolForecastResponse,
} from '@/lib/types'

type Tab = 'forecast' | 'forward' | 'dojo' | 'company'

const TABS: { id: Tab; label: string; blurb: string }[] = [
  {
    id: 'forecast',
    label: 'Forecast',
    blurb:
      'Volatility term projection, directional skill measurement, and how much of this name moves with the index.',
  },
  {
    id: 'forward',
    label: 'Forward test',
    blurb:
      'Monte Carlo projection of a live position with management rules applied along each path — and the calibration record of past predictions.',
  },
  {
    id: 'dojo',
    label: 'Dojo',
    blurb:
      'Automated rule search, selected on a training window and scored only on the window that follows.',
  },
  {
    id: 'company',
    label: 'Company',
    blurb:
      "TradingView's fundamentals, profile and technical gauge — real reported data, unlike everything else on this page, which is synthetic until credentials are configured.",
  },
]

const STRUCTURES = [
  { id: 'short_strangle', label: 'Short strangle' },
  { id: 'iron_condor', label: 'Iron condor' },
  { id: 'short_put', label: 'Cash-secured put' },
  { id: 'bull_put_spread', label: 'Bull put spread' },
  { id: 'covered_call', label: 'Covered call' },
  { id: 'long_call', label: 'Long call' },
  { id: 'long_straddle', label: 'Long straddle' },
  { id: 'calendar', label: 'Calendar' },
]

export default function ResearchPage() {
  const [tab, setTab] = useState<Tab>('forecast')
  const [symbol, setSymbol] = useState('NVDA')
  const [pending, setPending] = useState(symbol)
  const [error, setError] = useState<string | null>(null)

  const commit = () => {
    const s = pending.trim().toUpperCase()
    if (s) setSymbol(s)
  }

  return (
    <Shell symbol={symbol} onSymbol={(s) => { setSymbol(s); setPending(s) }} actions={<><div className="row" style={{ gap: 6, marginLeft: 'auto' }}>
     <input
      className="input"
      style={{ width: 104, textTransform: 'uppercase' }}
      value={pending}
      onChange={(e) => setPending(e.target.value.toUpperCase())}
      onKeyDown={(e) => e.key === 'Enter' && commit()}
      onBlur={commit}
      aria-label="Symbol"
     />
    </div></>}>

      <div className="page">
        <div className="card">
          <div className="card-head" style={{ gap: 12 }}>
            <div className="tabs" role="tablist">
              {TABS.map((t) => (
                <button
                  key={t.id}
                  className="tab"
                  role="tab"
                  aria-selected={tab === t.id}
                  onClick={() => setTab(t.id)}
                >
                  {t.label}
                </button>
              ))}
            </div>
            <span className="sub" style={{ maxWidth: 640, textAlign: 'right' }}>
              {TABS.find((t) => t.id === tab)?.blurb}
            </span>
          </div>
        </div>

        {error && <div className="err">{error}</div>}

        {tab === 'forecast' && <ForecastTab symbol={symbol} onError={setError} />}
        {tab === 'forward' && <ForwardTab symbol={symbol} onError={setError} />}
        {tab === 'dojo' && <DojoTab symbol={symbol} onError={setError} />}
        {tab === 'company' && <CompanyTab symbol={symbol} />}
      </div>
    </Shell>
  )
}

/* =============================================================== company */

/*
 * The one tab on this page carrying real reported figures.
 *
 * Everything else in Research runs on the synthetic provider unless market-data
 * credentials are set, so mixing TradingView's actual fundamentals into those
 * panels would produce a screen where some numbers are real and nothing says
 * which. Kept as its own tab, with the distinction stated, for that reason.
 *
 * The symbol needs an exchange prefix for TradingView to resolve it. There is
 * no reliable way to derive one, so the guess is stated as a guess and the user
 * can override it.
 */
function CompanyTab({ symbol }: { symbol: string }) {
  const [exchange, setExchange] = useState('NASDAQ')
  const tv = `${exchange}:${symbol.toUpperCase()}`
  return (
    <>
      <div className="banner">
        <span aria-hidden>▲</span>
        <span>
          <strong>These three panels are real, and the rest of Research is not.</strong>{' '}
          They are TradingView&apos;s reported fundamentals for <code>{tv}</code>. Every
          forecast, forward test and dojo result elsewhere on this page is computed from the
          synthetic provider. Comparing them tells you nothing.
        </span>
      </div>

      <div className="card">
        <div className="card-head">
          <div>
            <h2>Symbol</h2>
            <span className="sub">
              TradingView needs an exchange prefix; this app stores plain tickers, so the
              prefix is a guess — change it if the panels come back empty
            </span>
          </div>
          <div className="tabs">
            {['NASDAQ', 'NYSE', 'AMEX'].map((x) => (
              <button key={x} className="tab" aria-selected={exchange === x}
                onClick={() => setExchange(x)}>{x}</button>
            ))}
          </div>
        </div>
        <div className="card-body flush">
          <TvWidget kind="symbol-info" height={200} label="Symbol info"
            config={{ symbol: tv, isTransparent: true, width: '100%' }} />
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <h2>Fundamentals</h2>
          <span className="sub">income statement, balance sheet and cash flow as reported</span>
        </div>
        <div className="card-body flush">
          <TvWidget kind="financials" height={560} label="Fundamental data"
            config={{ symbol: tv, displayMode: 'regular', largeChartUrl: '',
                      isTransparent: true, width: '100%', height: 560 }} />
        </div>
      </div>

      <div className="grid-2">
        <div className="card">
          <div className="card-head">
            <h2>Profile</h2>
            <span className="sub">sector, employees, what the business actually is</span>
          </div>
          <div className="card-body flush">
            <TvWidget kind="symbol-profile" height={420} label="Company profile"
              config={{ symbol: tv, isTransparent: true, width: '100%', height: 420 }} />
          </div>
        </div>
        <div className="card">
          <div className="card-head">
            <h2>Technical gauge</h2>
            <span className="sub">TradingView&apos;s oscillator and moving-average summary</span>
          </div>
          <div className="card-body flush">
            <TvWidget kind="technical-analysis" height={420} label="Technical analysis"
              config={{ symbol: tv, interval: '1D', showIntervalTabs: true,
                        isTransparent: true, width: '100%', height: 420 }} />
          </div>
        </div>
      </div>
    </>
  )
}

/* ============================================================== forecast */

function ForecastTab({ symbol, onError }: { symbol: string; onError: (e: string | null) => void }) {
  const [vol, setVol] = useState<VolForecastResponse | null>(null)
  const [dir, setDir] = useState<DirectionResponse | null>(null)
  const [cross, setCross] = useState<CrossAssetResponse | null>(null)
  const [horizon, setHorizon] = useState(21)
  const [benchmark, setBenchmark] = useState('SPY')
  const [move, setMove] = useState(0.05)
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    onError(null)
    try {
      const [v, d, c] = await Promise.all([
        api.volForecast(symbol),
        api.direction(symbol, horizon),
        api.crossAsset(symbol, benchmark, move),
      ])
      setVol(v)
      setDir(d)
      setCross(c)
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [symbol, horizon, benchmark, move, onError])

  useEffect(() => {
    load()
  }, [load])

  const har = vol?.har ?? vol?.har_short_horizon ?? null

  return (
    <>
      <div className="card">
        <div className="card-head">
          <div>
            <h2>Volatility term projection · {symbol}</h2>
            <span className="sub">
              {vol
                ? `${vol.bars} daily bars through ${vol.as_of}`
                : loading
                  ? 'loading…'
                  : ''}
            </span>
          </div>
        </div>
        {vol?.projections?.length ? (
          <>
            <div className="card-body flush">
              <VolTermChart projections={vol.projections} impliedNow={vol.implied_now} />
            </div>
            <div className="card-body flush">
              <table className="table">
                <thead>
                  <tr>
                    <th>Horizon</th>
                    <th>Date</th>
                    <th>Projected</th>
                    <th>Diffusive</th>
                    <th>Events</th>
                    <th>Implied now</th>
                    <th>Implied − projected</th>
                  </tr>
                </thead>
                <tbody>
                  {vol.projections.map((p) => (
                    <tr key={p.days}>
                      <td>{p.days}d</td>
                      <td>{p.date}</td>
                      <td>{pct(p.projected_vol, 1)}</td>
                      <td>{pct(p.diffusive_vol, 1)}</td>
                      <td>{p.events_in_window || '—'}</td>
                      <td>{p.implied_now != null ? pct(p.implied_now, 1) : '—'}</td>
                      <td className={(p.implied_minus_projected ?? 0) >= 0 ? 'pos' : 'neg'}>
                        {p.implied_minus_projected != null ? pct(p.implied_minus_projected, 1) : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="card-body muted tiny">{vol.note}</div>
          </>
        ) : (
          <div className="card-body skeleton" style={{ height: 180 }} />
        )}
      </div>

      <div className="grid-2">
        <div className="card">
          <div className="card-head">
            <h2>Volatility models</h2>
          </div>
          <div className="card-body">
            {har ? (
              <>
                <div className="kv">
                  <span>HAR {har.horizon_days}-day forecast</span>
                  <strong>{pct(har.forecast_vol, 1)}</strong>
                </div>
                <div className="kv">
                  <span>Out-of-sample R²</span>
                  <strong className={(har.out_of_sample_r2 ?? 0) > 0.05 ? 'pos' : ''}>
                    {har.out_of_sample_r2 != null ? num(har.out_of_sample_r2, 3) : '—'}
                  </strong>
                </div>
                <div className="kv">
                  <span>In-sample R²</span>
                  <strong>{har.in_sample_r2 != null ? num(har.in_sample_r2, 3) : '—'}</strong>
                </div>
                <div className="kv">
                  <span>Components (d / w / m)</span>
                  <strong>
                    {pct(har.daily_component, 0)} / {pct(har.weekly_component, 0)} /{' '}
                    {pct(har.monthly_component, 0)}
                  </strong>
                </div>
                <p className="tiny muted" style={{ marginTop: 8 }}>{har.note}</p>
              </>
            ) : (
              <p className="muted">Not enough history to fit the HAR model.</p>
            )}

            {vol?.garch && (
              <>
                <div className="rule" />
                <div className="kv">
                  <span>GARCH persistence</span>
                  <strong>{num(vol.garch.persistence, 3)}</strong>
                </div>
                <div className="kv">
                  <span>Shock half-life</span>
                  <strong>
                    {vol.garch.half_life_days != null ? `${num(vol.garch.half_life_days, 1)} days` : '—'}
                  </strong>
                </div>
                <div className="kv">
                  <span>Long-run level</span>
                  <strong>{pct(vol.garch.long_run_vol, 1)}</strong>
                </div>
                <div className="kv">
                  <span>Current level</span>
                  <strong>{pct(vol.garch.current_vol, 1)}</strong>
                </div>
                {!vol.garch.converged && (
                  <p className="tiny neg" style={{ marginTop: 8 }}>
                    The optimizer did not converge cleanly. Treat these parameters as indicative.
                  </p>
                )}
              </>
            )}
          </div>
        </div>

        <div className="card">
          <div className="card-head">
            <div>
              <h2>Directional model</h2>
              <span className="sub">{horizon}-day horizon, walk-forward validated</span>
            </div>
            <select
              className="select"
              style={{ width: 110 }}
              value={horizon}
              onChange={(e) => setHorizon(Number(e.target.value))}
              aria-label="Prediction horizon"
            >
              {[5, 10, 21, 42, 63].map((h) => (
                <option key={h} value={h}>{h} days</option>
              ))}
            </select>
          </div>
          {dir ? (
            <>
              <div className="tiles">
                <div className="tile">
                  <div className="k">Model says</div>
                  <div className="v">{pct(dir.probability_up, 1)}</div>
                  <div className="n">probability of a higher close</div>
                </div>
                <div className="tile">
                  <div className="k">Trustworthy</div>
                  <div className={`v ${dir.trustworthy ? 'pos' : 'neg'}`}>
                    {dir.trustworthy ? 'Yes' : 'No'}
                  </div>
                  <div className="n">by its own out-of-sample score</div>
                </div>
              </div>
              <SkillReport d={dir} />
              {dir.folds.length > 0 && (
                <div className="card-body flush">
                  <table className="table">
                    <thead>
                      <tr><th>Fold</th><th>Train</th><th>Test</th><th>Accuracy</th><th>Base</th><th>Edge</th><th>AUC</th></tr>
                    </thead>
                    <tbody>
                      {dir.folds.map((f) => (
                        <tr key={f.fold}>
                          <td>{f.fold}</td>
                          <td>{f.train_size}</td>
                          <td>{f.test_size}</td>
                          <td>{pct(f.accuracy, 1)}</td>
                          <td className="muted">{pct(f.base_rate, 1)}</td>
                          <td className={f.edge >= 0 ? 'pos' : 'neg'}>{pct(f.edge, 1)}</td>
                          <td>{num(f.auc, 3)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          ) : (
            <div className="card-body skeleton" style={{ height: 220 }} />
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <div>
            <h2>Index and sector propagation</h2>
            <span className="sub">how much of {symbol} is the market, and how much is the company</span>
          </div>
          <div className="row" style={{ gap: 6 }}>
            <select
              className="select"
              style={{ width: 100 }}
              value={benchmark}
              onChange={(e) => setBenchmark(e.target.value)}
              aria-label="Benchmark"
            >
              {['SPY', 'QQQ', 'IWM', 'DIA'].map((b) => (
                <option key={b} value={b}>{b}</option>
              ))}
            </select>
            <select
              className="select"
              style={{ width: 110 }}
              value={move}
              onChange={(e) => setMove(Number(e.target.value))}
              aria-label="Index move scenario"
            >
              {[-0.1, -0.05, -0.02, 0.02, 0.05, 0.1].map((m) => (
                <option key={m} value={m}>{m > 0 ? '+' : ''}{(m * 100).toFixed(0)}%</option>
              ))}
            </select>
          </div>
        </div>
        {cross ? (
          <>
            <div className="tiles">
              <div className="tile">
                <div className="k">Beta to {cross.benchmark.benchmark}</div>
                <div className="v">{num(cross.benchmark.beta, 2)}</div>
                <div className="n">{cross.benchmark.observations} observations</div>
              </div>
              <div className="tile">
                <div className="k">R²</div>
                <div className="v">{num(cross.benchmark.r_squared, 2)}</div>
                <div className="n">variance explained by the index</div>
              </div>
              <div className="tile">
                <div className="k">Total volatility</div>
                <div className="v">{pct(cross.benchmark.total_vol, 0)}</div>
                <div className="n">annualized</div>
              </div>
              <div className="tile">
                <div className="k">Systematic</div>
                <div className="v">{pct(cross.benchmark.systematic_vol, 0)}</div>
                <div className="n">hedgeable with the index</div>
              </div>
              <div className="tile">
                <div className="k">Idiosyncratic</div>
                <div className="v">{pct(cross.benchmark.idiosyncratic_vol, 0)}</div>
                <div className="n">the part that is this company</div>
              </div>
            </div>
            <div className="card-body">
              <p style={{ margin: 0 }}>
                A <strong>{move > 0 ? '+' : ''}{(move * 100).toFixed(0)}%</strong> move in{' '}
                {cross.benchmark.benchmark} implies{' '}
                <strong className={cross.propagation.expected_stock_move >= 0 ? 'pos' : 'neg'}>
                  {pct(cross.propagation.expected_stock_move, 1)}
                </strong>{' '}
                in {symbol}, with a 68% range of{' '}
                {pct(cross.propagation.range_68[0], 1)} to {pct(cross.propagation.range_68[1], 1)} and a
                95% range of {pct(cross.propagation.range_95[0], 1)} to{' '}
                {pct(cross.propagation.range_95[1], 1)}.
              </p>
              <p className="tiny muted" style={{ marginTop: 8 }}>{cross.propagation.note}</p>
            </div>
          </>
        ) : (
          <div className="card-body skeleton" style={{ height: 140 }} />
        )}
      </div>
    </>
  )
}

/* =============================================================== forward */

function ForwardTab({ symbol, onError }: { symbol: string; onError: (e: string | null) => void }) {
  const [template, setTemplate] = useState('short_strangle')
  const [paths, setPaths] = useState(4000)
  const [profitTarget, setProfitTarget] = useState(0.5)
  const [stopLoss, setStopLoss] = useState(2)
  const [closeAtDte, setCloseAtDte] = useState(21)
  const [drift, setDrift] = useState(0)
  const [tailDf, setTailDf] = useState(4)
  const [useForecastVol, setUseForecastVol] = useState(true)
  const [res, setRes] = useState<ForwardResponse | null>(null)
  const [cal, setCal] = useState<CalibrationResponse | null>(null)
  const [running, setRunning] = useState(false)

  const run = async () => {
    setRunning(true)
    onError(null)
    try {
      const r = await api.forwardSimulate({
        symbol,
        template,
        paths,
        annual_drift: drift,
        tail_df: tailDf,
        profit_target_pct: profitTarget > 0 ? profitTarget : null,
        stop_loss_pct: stopLoss > 0 ? stopLoss : null,
        close_at_dte: closeAtDte > 0 ? closeAtDte : null,
        use_forecast_vol: useForecastVol,
      })
      setRes(r)
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e))
    } finally {
      setRunning(false)
    }
  }

  useEffect(() => {
    api.calibration().then(setCal).catch(() => {})
  }, [res])

  const o = res?.outcomes
  const p = res?.path_dependent

  return (
    <>
      <div className="card">
        <div className="card-head">
          <div>
            <h2>Forward test · {symbol}</h2>
            <span className="sub">
              {res
                ? `${res.paths.toLocaleString()} paths over ${res.horizon_days} days at ${pct(res.base_vol, 1)} vol`
                : 'configure and run'}
            </span>
          </div>
          <button className="btn primary" onClick={run} disabled={running}>
            {running ? 'Simulating…' : 'Run simulation'}
          </button>
        </div>
        <div className="card-body">
          <div className="controls">
            <label className="field" style={{ minWidth: 170 }}>
              <span>Structure</span>
              <select className="select" value={template} onChange={(e) => setTemplate(e.target.value)}>
                {STRUCTURES.map((s) => (
                  <option key={s.id} value={s.id}>{s.label}</option>
                ))}
              </select>
            </label>
            <NumField label="Paths" value={paths} step={500} min={500} max={40000} onChange={setPaths} />
            <NumField
              label="Profit target"
              value={profitTarget}
              step={0.05}
              min={0}
              max={3}
              onChange={setProfitTarget}
              hint="× basis, 0 = off"
            />
            <NumField label="Stop loss" value={stopLoss} step={0.25} min={0} max={6} onChange={setStopLoss}
              hint="× basis, 0 = off" />
            <NumField label="Close at DTE" value={closeAtDte} step={1} min={0} max={90} onChange={setCloseAtDte}
              hint="0 = hold to expiry" />
            <NumField label="Annual drift" value={drift} step={0.01} min={-0.3} max={0.3} onChange={setDrift}
              hint="0 = risk-neutral" />
            <NumField label="Tail df" value={tailDf} step={0.5} min={2.5} max={30} onChange={setTailDf}
              hint="lower = fatter tails" />
            <label className="field" style={{ justifyContent: 'flex-end' }}>
              <span className="row" style={{ gap: 8, cursor: 'pointer' }}>
                <input
                  type="checkbox"
                  checked={useForecastVol}
                  onChange={(e) => setUseForecastVol(e.target.checked)}
                />
                <span>Use forecast vol when it has skill</span>
              </span>
            </label>
          </div>
        </div>
      </div>

      {res && (
        <div className="banner">
          <span aria-hidden>▲</span>
          <span>
            <strong>Volatility input:</strong> {res.volatility_source}
          </span>
        </div>
      )}

      {res && o && p && (
        <>
          <div className="card">
            <div className="card-head">
              <h2>Outcome distribution</h2>
              <span className="sub">entry basis {money(res.entry_basis, 0)}</span>
            </div>
            <div className="tiles">
              <div className="tile">
                <div className="k">Win rate</div>
                <div className="v">{pct(o.win_rate, 1)}</div>
                <div className="n">across all paths</div>
              </div>
              <div className="tile">
                <div className="k">Mean P/L</div>
                <div className={`v ${o.mean_pl >= 0 ? 'pos' : 'neg'}`}>{money(o.mean_pl, 0)}</div>
                <div className="n">median {money(o.median_pl, 0)}</div>
              </div>
              <div className="tile">
                <div className="k">5th percentile</div>
                <div className="v neg">{money(o.p05, 0)}</div>
                <div className="n">1 in 20 does worse</div>
              </div>
              <div className="tile">
                <div className="k">Worst path</div>
                <div className="v neg">{money(o.worst, 0)}</div>
                <div className="n">of {res.paths.toLocaleString()} simulated</div>
              </div>
              <div className="tile">
                <div className="k">Avg hold</div>
                <div className="v">{num(p.mean_days_held, 1)}d</div>
                <div className="n">median {num(p.median_days_held, 0)}d</div>
              </div>
              <div className="tile">
                <div className="k">Drawdown on winners</div>
                <div className="v neg">
                  {p.mean_max_adverse_on_winners != null ? money(p.mean_max_adverse_on_winners, 0) : '—'}
                </div>
                <div className="n">how far winners go underwater first</div>
              </div>
            </div>
            <OutcomeRibbon
              target={p.hit_profit_target}
              stop={p.hit_stop_loss}
              timed={p.closed_on_time}
            />
          </div>

          <div className="grid-2">
            <div className="card">
              <div className="card-head">
                <h2>Simulated price paths</h2>
                <span className="sub">percentile bands</span>
              </div>
              <div className="card-body flush">
                <FanChart
                  days={res.fan_chart.days}
                  bands={res.fan_chart.bands}
                  spot={res.fan_chart.bands['50']?.[0]}
                />
              </div>
            </div>

            <div className="card">
              <div className="card-head">
                <h2>P/L at exit</h2>
                <span className="sub">
                  expected win {money(o.expected_win, 0)} · expected loss {money(o.expected_loss, 0)}
                </span>
              </div>
              <div className="card-body flush">
                <Histogram
                  edges={res.pl_histogram.edges}
                  counts={res.pl_histogram.counts}
                  below={res.pl_histogram.below}
                  above={res.pl_histogram.above}
                  min={res.pl_histogram.min}
                  max={res.pl_histogram.max}
                />
              </div>
            </div>
          </div>

          <div className="card">
            <div className="card-body tiny muted">{res.note}</div>
          </div>
        </>
      )}

      <div className="card">
        <div className="card-head">
          <div>
            <h2>Prediction calibration</h2>
            <span className="sub">
              {cal
                ? `${cal.closed_paper_trades} closed paper trades · ${cal.scored} scored` +
                  (cal.unscorable ? ` · ${cal.unscorable} logged without a prediction` : '')
                : ''}
            </span>
          </div>
        </div>
        {cal?.calibration.available ? (
          <>
            <div className="card-body flush">
              <CalibrationChart report={cal.calibration} />
            </div>
            <div className="card-body flush">
              <table className="table">
                <thead>
                  <tr><th>Predicted band</th><th>Trades</th><th>Predicted</th><th>Realized</th><th>Gap</th></tr>
                </thead>
                <tbody>
                  {cal.calibration.buckets?.map((b) => (
                    <tr key={b.bucket}>
                      <td>{b.bucket}</td>
                      <td>{b.trades}</td>
                      <td>{pct(b.predicted_win_rate, 1)}</td>
                      <td>{pct(b.realized_win_rate, 1)}</td>
                      <td className={Math.abs(b.gap) > 0.1 ? 'neg' : 'pos'}>{pct(b.gap, 1)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="card-body tiny muted">{cal.calibration.note}</div>
          </>
        ) : (
          <div className="card-body muted">
            {cal?.calibration.reason ??
              'No scored paper trades yet. Open positions from the Strategy Lab and close them to build a calibration record — until then the probability engine is unvalidated on your own trades.'}
          </div>
        )}
      </div>
    </>
  )
}

/* ================================================================== dojo */

function DojoTab({ symbol, onError }: { symbol: string; onError: (e: string | null) => void }) {
  const [template, setTemplate] = useState('short_strangle')
  const [variants, setVariants] = useState(30)
  const [trainFraction, setTrainFraction] = useState(0.6)
  const [lookback, setLookback] = useState(1200)
  const [seed, setSeed] = useState(42)
  const [space, setSpace] = useState<{ combinations: number } | null>(null)
  const [res, setRes] = useState<DojoResponse | null>(null)
  const [running, setRunning] = useState(false)

  useEffect(() => {
    api.dojoSearchSpace().then(setSpace).catch(() => {})
  }, [])

  const run = async () => {
    setRunning(true)
    onError(null)
    try {
      const r = await api.runDojo({
        symbol,
        template_id: template,
        variants,
        train_fraction: trainFraction,
        lookback_days: lookback,
        seed,
      })
      setRes(r)
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e))
    } finally {
      setRunning(false)
    }
  }

  const v = res?.validation

  return (
    <>
      <div className="card">
        <div className="card-head">
          <div>
            <h2>Dojo · {symbol}</h2>
            <span className="sub">
              {space ? `${space.combinations.toLocaleString()} rule sets in the space` : ''}
            </span>
          </div>
          <button className="btn primary" onClick={run} disabled={running}>
            {running ? 'Searching…' : 'Run search'}
          </button>
        </div>
        <div className="card-body">
          <div className="controls">
            <label className="field" style={{ minWidth: 170 }}>
              <span>Structure</span>
              <select className="select" value={template} onChange={(e) => setTemplate(e.target.value)}>
                {STRUCTURES.map((s) => (
                  <option key={s.id} value={s.id}>{s.label}</option>
                ))}
              </select>
            </label>
            <NumField label="Variants" value={variants} step={5} min={5} max={120} onChange={setVariants}
              hint="more tried = higher bar to believe the winner" />
            <NumField label="Train fraction" value={trainFraction} step={0.05} min={0.4} max={0.8}
              onChange={setTrainFraction} />
            <NumField label="History (days)" value={lookback} step={100} min={500} max={3000}
              onChange={setLookback} />
            <NumField label="Seed" value={seed} step={1} min={1} max={9999} onChange={setSeed} />
          </div>
        </div>
      </div>

      {res && v && (
        <>
          <div className={`banner ${v.selection_beat_random ? '' : 'warn'}`}>
            <span aria-hidden>{v.selection_beat_random ? '✓' : '▲'}</span>
            <span>{res.verdict}</span>
          </div>

          <div className="card">
            <div className="card-head">
              <div>
                <h2>Validation</h2>
                <span className="sub">
                  train {res.train_window.start} → {res.train_window.end} · test {res.test_window.start} →{' '}
                  {res.test_window.end}
                </span>
              </div>
            </div>
            <div className="tiles">
              <div className="tile">
                <div className="k">Out-of-sample percentile</div>
                <div className={`v ${v.selection_beat_random ? 'pos' : 'neg'}`}>
                  {num(v.out_of_sample_percentile, 0)}
                </div>
                <div className="n">where the training winner landed on test</div>
              </div>
              <div className="tile">
                <div className="k">Beat random pick</div>
                <div className={`v ${v.selection_beat_random ? 'pos' : 'neg'}`}>
                  {v.selection_beat_random ? 'Yes' : 'No'}
                </div>
                <div className="n">the decisive test</div>
              </div>
              <div className="tile">
                <div className="k">Beat the baseline</div>
                <div className={`v ${v.selection_beat_baseline ? 'pos' : 'neg'}`}>
                  {v.selection_beat_baseline ? 'Yes' : 'No'}
                </div>
                <div className="n">vs the rules you started with</div>
              </div>
              <div className="tile">
                <div className="k">Degradation</div>
                <div className="v">{num(v.degradation, 2)}</div>
                <div className="n">train score − test score</div>
              </div>
              <div className="tile">
                <div className="k">Variants tried</div>
                <div className="v">{res.variants_tried}</div>
                <div className="n">of {res.search_space_size.toLocaleString()} possible</div>
              </div>
            </div>
          </div>

          <div className="grid-2">
            <div className="card">
              <div className="card-head">
                <h2>Selected rule set</h2>
              </div>
              <div className="card-body">
                {Object.entries(res.selected.variant).map(([k, val]) => (
                  <div className="kv" key={k}>
                    <span>{VARIANT_LABELS[k] ?? k.replace(/_/g, ' ')}</span>
                    <strong>{formatVariant(k, val as number)}</strong>
                  </div>
                ))}
              </div>
            </div>

            <div className="card">
              <div className="card-head">
                <h2>In sample vs out of sample</h2>
                <span className="sub">the same rule set, two windows</span>
              </div>
              <div className="card-body flush">
                <table className="table">
                  <thead>
                    <tr><th>Metric</th><th>Train</th><th>Test</th><th>Baseline (test)</th></tr>
                  </thead>
                  <tbody>
                    {(
                      [
                        ['Trades', 'trades', 0],
                        ['Win rate', 'win_rate', 1],
                        ['Total P/L', 'total_pl', 0],
                        ['Max drawdown', 'max_drawdown', 0],
                        ['Sharpe', 'sharpe', 2],
                        ['Profit factor', 'profit_factor', 2],
                      ] as const
                    ).map(([label, key, dp]) => {
                      const fmt = (x: unknown) => {
                        if (x == null) return '—'
                        const n = Number(x)
                        if (key === 'win_rate') return pct(n, dp)
                        if (key === 'total_pl' || key === 'max_drawdown') return money(n, dp)
                        return num(n, dp)
                      }
                      const tr = res.selected.train_metrics as unknown as Record<string, number | null>
                      const te = res.selected.test_metrics as unknown as Record<string, number | null>
                      const bl = res.baseline.test_metrics as unknown as Record<string, number | null> | null
                      return (
                        <tr key={key}>
                          <td>{label}</td>
                          <td>{fmt(tr?.[key])}</td>
                          <td>{fmt(te?.[key])}</td>
                          <td className="muted">{fmt(bl?.[key])}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <h2>Leaderboard</h2>
              <span className="sub">ranked by TRAINING score — the test column is what actually happened next</span>
            </div>
            <div className="card-body flush">
              <div className="scroll" style={{ maxHeight: 420 }}>
                <table className="table">
                  <thead>
                    <tr>
                      <th>#</th><th>Target</th><th>Stop</th><th>Close DTE</th><th>Entry DTE</th>
                      <th>Delta</th><th>Cadence</th>
                      <th>Train P/L</th><th>Train score</th>
                      <th>Test P/L</th><th>Test trades</th><th>Test score</th>
                    </tr>
                  </thead>
                  <tbody>
                    {res.leaderboard.map((r, i) => (
                      <tr key={i} style={i === 0 ? { background: 'var(--surface-2)' } : undefined}>
                        <td>{i + 1}</td>
                        <td>{r.profit_target_pct ? pct(r.profit_target_pct, 0) : 'off'}</td>
                        <td>{r.stop_loss_pct ? `${num(r.stop_loss_pct, 2)}×` : 'off'}</td>
                        <td>{r.close_at_dte || 'off'}</td>
                        <td>{r.target_dte}</td>
                        <td>{num(r.delta, 2)}</td>
                        <td>{r.entry_every_days}d</td>
                        <td className={r.train_pl >= 0 ? 'pos' : 'neg'}>{money(r.train_pl, 0)}</td>
                        <td>{num(r.train_score, 2)}</td>
                        <td className={r.test_pl >= 0 ? 'pos' : 'neg'}>{money(r.test_pl, 0)}</td>
                        <td>{r.test_trades}</td>
                        <td>{num(r.test_score, 2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
            <div className="card-body tiny muted">{res.caveat}</div>
          </div>
        </>
      )}

      {!res && !running && (
        <div className="card">
          <div className="card-body muted">
            The dojo proposes rule variations, ranks them on a training window, and reports the
            winner&apos;s performance on the window that follows. Read the out-of-sample percentile
            first: if the training winner lands mid-pack on the test window, the search found noise,
            and the leaderboard below it is a list of coincidences.
          </div>
        </div>
      )}
    </>
  )
}

/* ------------------------------------------------------------ small bits */

const VARIANT_LABELS: Record<string, string> = {
  profit_target_pct: 'Profit target',
  stop_loss_pct: 'Stop loss',
  close_at_dte: 'Close at DTE',
  target_dte: 'Entry DTE',
  delta: 'Short delta',
  entry_every_days: 'Entry cadence',
}

function formatVariant(key: string, value: number): string {
  // 0 is the "disabled" arm of the search, not a stop at zero or an instant
  // exit. Printing it as "0.00" would read as an aggressive setting rather
  // than an absent one.
  if (key === 'profit_target_pct') return value ? pct(value, 0) : 'off'
  if (key === 'stop_loss_pct') return value ? `${num(value, 2)}× basis` : 'off'
  if (key === 'close_at_dte') return value ? `${value} days left` : 'off'
  if (key === 'target_dte') return `${value} DTE`
  if (key === 'entry_every_days') return `every ${value}d`
  return num(value, 2)
}

function NumField({
  label, value, step, min, max, onChange, hint,
}: {
  label: string
  value: number
  step: number
  min: number
  max: number
  onChange: (v: number) => void
  hint?: string
}) {
  return (
    <label className="field" style={{ minWidth: 130 }}>
      <span>{label}</span>
      <input
        className="input"
        type="number"
        value={value}
        step={step}
        min={min}
        max={max}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      {hint && <span className="help">{hint}</span>}
    </label>
  )
}
