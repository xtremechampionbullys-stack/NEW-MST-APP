'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { Shell } from '@/components/Shell'
import { ImpactFeed, SegmentBoard } from '@/components/MarketBoard'
import { LiveBadge, useLive } from '@/components/live'
import { Sparkline } from '@/components/charts'
import { TvPanel } from '@/components/TradingView'
import { api } from '@/lib/api'
import { pct } from '@/lib/format'
import type { NewsFeedResponse, SegmentsResponse, WatchlistResponse } from '@/lib/types'

const DEFAULT = 'SPY,QQQ,NVDA,AAPL,TSLA,AMD,META,COIN'

export default function Dashboard() {
  const [symbols, setSymbols] = useState(DEFAULT)
  const [input, setInput] = useState(DEFAULT)
  const [data, setData] = useState<WatchlistResponse | null>(null)
  const [risk, setRisk] = useState<Record<string, unknown> | null>(null)
  const [board, setBoard] = useState<SegmentsResponse | null>(null)
  const [news, setNews] = useState<NewsFeedResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const tickers = symbols.split(',').map((s) => s.trim()).filter(Boolean)

  // One live channel per panel rather than one for the page. A watchlist that
  // refreshes every 15 seconds and a dojo-scale board that does not need to are
  // different problems, and a single interval forces the slowest cadence on
  // everything or the fastest on the provider.
  const live = useLive(() => api.watchlist(tickers), [symbols], 15000)
  const boardLive = useLive(() => api.segments(), [], 30000)
  const newsLive = useLive(() => api.watchlistNews(tickers, 40), [symbols], 60000)

  useEffect(() => {
    if (live.data) setData(live.data)
    if (live.error) setError(live.error)
    else if (live.data) setError(null)
    setLoading(live.loading && !live.data)
  }, [live.data, live.error, live.loading])

  useEffect(() => {
    if (boardLive.data) setBoard(boardLive.data)
  }, [boardLive.data])

  useEffect(() => {
    if (newsLive.data) setNews(newsLive.data)
  }, [newsLive.data])

  useEffect(() => {
    api.portfolioRisk().then((r) => setRisk(r as Record<string, unknown>)).catch(() => {})
  }, [])

  const summary = data?.summary

  return (
    <Shell actions={<><form
     className="row"
     style={{ gap: 6, marginLeft: 'auto' }}
     onSubmit={(e) => {
      e.preventDefault()
      setSymbols(input)
     }}
    >
     <input
      className="input"
      style={{ width: 340, textTransform: 'uppercase' }}
      value={input}
      onChange={(e) => setInput(e.target.value)}
      aria-label="Watchlist symbols"
     />
     <button className="btn" type="submit">Update</button>
    </form></>}>

      {(data?.rows ?? []).filter((r) => r.iv_rank_method === 'cross-sectional').length >
        (data?.rows.length ?? 0) / 2 && (
        <div className="banner">
          <span aria-hidden>▲</span>
          <span>
            <strong>IV ranks are estimates.</strong> Most names here have too little stored
            history to rank against, so the figures come from the spread of volatilities quoted
            across each chain today — a narrower measure that pulls ranks toward the middle. Run{' '}
            <code>POST /api/volatility/snapshot</code> once a day to build the real thing.
          </span>
        </div>
      )}

      {summary?.synthetic && (
        <div className="banner">
          <span aria-hidden>▲</span>
          <span>
            <strong>Synthetic data.</strong> Every figure below is generated, not market data.
          </span>
        </div>
      )}

      <div className="page">
        {error && <div className="err">{error}</div>}

        <div className="card">
          <div className="card-head">
            <h2>Watchlist</h2>
            <span className="sub">
              {summary ? `${summary.tracked} tracked` : ''}
              {summary?.errors ? ` · ${summary.errors} unavailable` : ''}
              {loading ? ' · loading…' : ''}
            </span>
            <LiveBadge state={live} />
          </div>

          <div className="tiles">
            <div className="tile">
              <div className="k">Names tracked</div>
              <div className="v">{summary?.tracked ?? '—'}</div>
              <div className="n">{summary?.errors ? `${summary.errors} failed` : 'all responding'}</div>
            </div>
            <div className="tile">
              <div className="k">Average IV rank</div>
              <div className="v">{summary?.average_iv_rank != null ? pct(summary.average_iv_rank, 0) : '—'}</div>
              <div className="n">across the list</div>
            </div>
            <div className="tile">
              <div className="k">Avg variance premium</div>
              <div className={`v ${(summary?.average_variance_premium ?? 0) >= 0 ? 'pos' : 'neg'}`}>
                {summary?.average_variance_premium != null
                  ? `${summary.average_variance_premium >= 0 ? '+' : ''}${(summary.average_variance_premium * 100).toFixed(1)}`
                  : '—'}
              </div>
              <div className="n">implied − realized, points</div>
            </div>
            <div className="tile">
              <div className="k">Reporting soon</div>
              <div className="v">{data?.earnings_soon.filter((e) => e.days_away <= 14).length ?? '—'}</div>
              <div className="n">within 14 days</div>
            </div>
          </div>

          <div className="scroll" style={{ maxHeight: 520 }}>
            <table className="table">
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Last</th>
                  <th>Change</th>
                  <th style={{ textAlign: 'center' }}>Trend</th>
                  <th>IV rank</th>
                  <th>ATM IV</th>
                  <th>Realized 20d</th>
                  <th>Variance prem.</th>
                  <th>Earnings</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {(data?.rows ?? []).map((r) => {
                  if (r.error) {
                    return (
                      <tr key={r.symbol}>
                        <td>{r.symbol}</td>
                        <td colSpan={9} className="tiny muted" style={{ textAlign: 'left' }}>
                          {r.error}
                        </td>
                      </tr>
                    )
                  }
                  const up = (r.change_pct ?? 0) >= 0
                  const rank = r.iv_rank ?? null
                  return (
                    <tr key={r.symbol}>
                      <td style={{ fontWeight: 600 }}>{r.symbol}</td>
                      <td>{r.last?.toFixed(2)}</td>
                      <td className={up ? 'pos' : 'neg'}>
                        {up ? '+' : ''}{r.change_pct?.toFixed(2)}%
                      </td>
                      <td style={{ textAlign: 'center' }}>
                        <Sparkline values={r.sparkline ?? []} positive={up} />
                      </td>
                      <td>
                        {rank != null ? (
                          <div style={{ display: 'flex', alignItems: 'center', gap: 6, justifyContent: 'flex-end' }}>
                            <span>{pct(rank, 0)}</span>
                            <div className="meter" style={{ width: 44 }}>
                              <div
                                style={{
                                  width: `${Math.min(rank, 1) * 100}%`,
                                  background: rank > 0.5 ? 'var(--series-2)' : 'var(--series-1)',
                                }}
                              />
                            </div>
                          </div>
                        ) : '—'}
                      </td>
                      <td>{pct(r.atm_iv_30d, 1)}</td>
                      <td>{pct(r.realized_vol_20d, 1)}</td>
                      <td className={(r.variance_risk_premium ?? 0) >= 0 ? 'pos' : 'neg'}>
                        {r.variance_risk_premium != null
                          ? `${r.variance_risk_premium >= 0 ? '+' : ''}${(r.variance_risk_premium * 100).toFixed(1)}`
                          : '—'}
                      </td>
                      <td>
                        {r.earnings ? (
                          <span
                            className="tiny"
                            style={{ color: r.earnings.days_away <= 7 ? 'var(--status-warning)' : undefined }}
                            title={`${r.earnings.date} · ${r.earnings.confirmed ? 'confirmed' : 'projected'}`}
                          >
                            {r.earnings.days_away}d
                          </span>
                        ) : (
                          <span className="tiny muted">—</span>
                        )}
                      </td>
                      <td>
                        <Link className="btn ghost tiny" href={`/?symbol=${r.symbol}`} style={{ padding: '2px 8px' }}>
                          open
                        </Link>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>

        <div className="grid-2">
          <div className="card">
            <div className="card-head">
              <h2>Short-premium shortlist</h2>
              <span className="sub">IV rank ≥ 45% and options above realized</span>
            </div>
            {data?.premium_candidates.length ? (
              <table className="table">
                <thead>
                  <tr>
                    <th>Symbol</th><th>IV rank</th><th>ATM IV</th><th>Var. premium</th><th>Earnings</th>
                  </tr>
                </thead>
                <tbody>
                  {data.premium_candidates.map((c) => (
                    <tr key={c.symbol}>
                      <td style={{ fontWeight: 600 }}>{c.symbol}</td>
                      <td>{pct(c.iv_rank, 0)}</td>
                      <td>{pct(c.atm_iv_30d, 1)}</td>
                      <td className="pos">+{((c.variance_risk_premium ?? 0) * 100).toFixed(1)}</td>
                      <td className="tiny muted">{c.earnings_days != null ? `${c.earnings_days}d` : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="card-body muted tiny">
                Nothing on this list currently has implied volatility both elevated in its own
                range and above what the stock has recently delivered. That is a normal reading,
                not an error — it means there is no obvious premium to sell today.
              </div>
            )}
          </div>

          <div className="card">
            <div className="card-head">
              <h2>Earnings calendar</h2>
              <span className="sub">next report per name</span>
            </div>
            {data?.earnings_soon.length ? (
              <table className="table">
                <thead>
                  <tr><th>Symbol</th><th>Date</th><th>In</th><th>Session</th><th>Status</th></tr>
                </thead>
                <tbody>
                  {data.earnings_soon.map((e) => (
                    <tr key={e.symbol}>
                      <td style={{ fontWeight: 600 }}>{e.symbol}</td>
                      <td>{e.date}</td>
                      <td className={e.days_away <= 7 ? 'neg' : ''}>{e.days_away}d</td>
                      <td className="tiny muted">
                        {e.session === 'bmo' ? 'before open' : e.session === 'amc' ? 'after close' : e.session}
                      </td>
                      <td className="tiny" style={{ color: e.confirmed ? undefined : 'var(--status-warning)' }}>
                        {e.confirmed ? 'confirmed' : 'projected'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="card-body muted tiny">No upcoming reports found.</div>
            )}
          </div>
        </div>

        <div className="card">
          <div className="card-head">
            <h2><Link href="/portfolio" style={{ color: 'inherit' }}>Portfolio risk →</Link></h2>
            <span className="sub">
              {risk && typeof risk.positions === 'number' && risk.positions > 0
                ? `${risk.positions} open · ${risk.persistent ? 'persisted' : 'session only'}`
                : 'no positions tracked'}
            </span>
          </div>
          {risk && typeof risk.positions === 'number' && risk.positions > 0 ? (
            <>
              <div className="tiles">
                {(['delta', 'gamma', 'theta', 'vega'] as const).map((g) => {
                  const value = (risk.greeks as Record<string, number>)?.[g] ?? 0
                  return (
                    <div className="tile" key={g}>
                      <div className="k">Net {g}</div>
                      <div className={`v ${value >= 0 ? 'pos' : 'neg'}`}>
                        {g === 'gamma' ? value.toFixed(3) : value.toFixed(1)}
                      </div>
                      <div className="n">
                        {g === 'theta' ? 'dollars per day' : g === 'vega' ? 'per IV point' : 'share equivalent'}
                      </div>
                    </div>
                  )
                })}
              </div>
              {Array.isArray(risk.warnings) && (risk.warnings as string[]).length > 0 && (
                <div>
                  {(risk.warnings as string[]).map((w) => (
                    <div className="insight" key={w}>
                      <span className="icon" style={{ color: 'var(--status-serious)' }} aria-hidden>!</span>
                      <div className="d">{w}</div>
                    </div>
                  ))}
                </div>
              )}
            </>
          ) : (
            <div className="card-body muted tiny">
              Open a strategy as a paper trade from the <Link href="/lab">Strategy Lab</Link>,
              use the paper ticket on the <Link href="/charting">Charting</Link> terminal, or{' '}
              <code>POST /api/portfolio/positions</code> — aggregate greeks, beta-weighted delta
              and concentration warnings appear here and on the <Link href="/portfolio">Portfolio</Link> page.
            </div>
          )}
        </div>
      </div>

      {/*
        * Two TradingView panels, added where they answer something this app's
        * own data cannot. Everything above is synthetic unless credentials are
        * configured; these two are real market data, and they are the only
        * things on this page that are — which is why they are labelled rather
        * than mixed into the tables above. Do not read them against the
        * synthetic figures.
        */}
      <div className="page">
        <div className="grid-2">
          <TvPanel title="Market overview" label="Market overview"
            subtitle="indices, futures, bonds and forex — independent live context"
            kind="market-overview" height={440}
            config={{
              showFloatingTooltip: true, isTransparent: true,
              width: '100%', height: 440, showSymbolLogo: true,
              showChart: true, dateRange: '12M',
              tabs: [
                { title: 'Indices', symbols: [
                  { s: 'FOREXCOM:SPXUSD', d: 'S&P 500' },
                  { s: 'FOREXCOM:NSXUSD', d: 'Nasdaq 100' },
                  { s: 'FOREXCOM:DJI', d: 'Dow 30' },
                  { s: 'INDEX:RUT', d: 'Russell 2000' },
                  { s: 'CBOE:VIX', d: 'VIX' },
                ]},
                { title: 'Futures', symbols: [
                  { s: 'CME_MINI:ES1!', d: 'S&P 500' },
                  { s: 'CME_MINI:NQ1!', d: 'Nasdaq 100' },
                  { s: 'NYMEX:CL1!', d: 'Crude oil' },
                  { s: 'COMEX:GC1!', d: 'Gold' },
                ]},
                { title: 'Bonds', symbols: [
                  { s: 'CBOT:ZN1!', d: '10-year note' },
                  { s: 'CBOT:ZB1!', d: '30-year bond' },
                  { s: 'CBOT:ZF1!', d: '5-year note' },
                ]},
              ],
            }} />

          <TvPanel title="S&amp;P 500 heatmap" label="Stock heatmap"
            subtitle="by market cap, coloured by change and grouped by sector"
            kind="stock-heatmap" height={440}
            config={{
              dataSource: 'SPX500', blockSize: 'market_cap_basic',
              blockColor: 'change', grouping: 'sector', symbolUrl: '',
              hasTopBar: false, isDataSetEnabled: false, isZoomEnabled: true,
              hasSymbolTooltip: true, isTransparent: true,
              width: '100%', height: 440,
            }} />
        </div>
      </div>

      {board && <SegmentBoard data={board} badge={<LiveBadge state={boardLive} />} />}
      {news && <ImpactFeed data={news} badge={<LiveBadge state={newsLive} />}
        symbols={symbols} onSymbols={(v) => { setInput(v); setSymbols(v) }} />}
    </Shell>
  )
}
