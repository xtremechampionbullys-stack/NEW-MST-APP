'use client'

import { useEffect, useState } from 'react'
import { Shell } from '@/components/Shell'
import { SymbolSearch } from '@/components/SymbolSearch'
import { CandleChart, type Candle } from '@/components/CandleChart'
import { LiveBadge, SymbolField, useLive } from '@/components/live'
import { ChainGrid } from '@/components/ChainGrid'
import { TvWidget } from '@/components/TradingView'
import {
  DepthPanel, ExecutionPanel, NewsTicker, ScanPanel, SymbolPanel,
} from '@/components/Terminal'
import { api } from '@/lib/api'
import type { ChainResponse, KeyStats, OptionsLevels, TvStatus, TvTier } from '@/lib/types'

// The strip TradingView renders at the top of the page. Real contracts, their
// data — the one place in this app showing genuine live prices.
const TAPE = [
  { proName: 'CME_MINI:ES1!', title: 'S&P 500' },
  { proName: 'CME_MINI:NQ1!', title: 'Nasdaq 100' },
  { proName: 'CME_MINI:RTY1!', title: 'Russell 2000' },
  { proName: 'NYMEX:CL1!', title: 'Crude' },
  { proName: 'COMEX:GC1!', title: 'Gold' },
  { proName: 'CBOE:VIX', title: 'VIX' },
  { proName: 'NASDAQ:NVDA', title: 'NVDA' },
  { proName: 'NASDAQ:AAPL', title: 'AAPL' },
]

/**
 * TradingView symbol -> a ticker this app's engine actually has.
 *
 * The engine holds US equities and ETFs. TradingView's list includes futures
 * and an index, and the engine has neither — so the root has to be mapped to a
 * tradeable proxy. `ES` becomes `SPY`, and so on.
 *
 * The mapping is returned alongside the ticker rather than applied silently,
 * because the panels have to say which one they are showing. Without that, the
 * synthetic provider will happily invent "ES Holdings, Inc." with a market cap
 * and an earnings date, and a futures contract with a P/E is not a small
 * cosmetic error — it is the panel asserting something that cannot exist.
 */
const PROXY: Record<string, string> = {
  ES: 'SPY', NQ: 'QQQ', RTY: 'IWM', CL: 'USO', GC: 'GLD', VIX: 'VXX',
}

function nativeFor(tv: string): { ticker: string; proxyFor: string | null } {
  const root = (tv.split(':').pop() ?? tv).replace(/\d*!$/, '').toUpperCase()
  const mapped = PROXY[root]
  return mapped ? { ticker: mapped, proxyFor: root } : { ticker: root, proxyFor: null }
}

/** Kept for the chain and headings, which only need the plain root. */
function plainTicker(tv: string): string {
  return nativeFor(tv).ticker
}

/**
 * TradingView charting.
 *
 * The widget below is TradingView's free Advanced Real-Time Chart embed. It
 * renders TradingView's data, not this application's, and it cannot place
 * orders. Both of those are stated on the page rather than left for the user to
 * discover, because "TradingView charts with broker integration" is a phrase
 * that spans three different TradingView products and only the least capable of
 * them is a script tag.
 *
 * The other two tiers are described from `/api/tv/status`, which is generated
 * server-side so this page cannot claim a capability the API does not have.
 */

/*
 * Indicator presets, passed to the widget as `studies`. These are TradingView's
 * built-in study ids — the same ones the chart's own Indicators dialog adds, so
 * a preset and a manual add produce identical panes.
 *
 * The widget is re-created when a preset changes because the embed exposes no
 * API to add or remove studies after mount. That is a real constraint of the
 * free embed, not a shortcut: the Charting Library (see the status panel below)
 * does expose that API, and this is one of the things it would buy.
 */
const PRESETS: { id: string; label: string; studies: string[] }[] = [
  { id: 'clean', label: 'Clean', studies: [] },
  { id: 'trend', label: 'Trend', studies: ['STD;EMA', 'STD;EMA', 'STD;Supertrend'] },
  { id: 'momentum', label: 'Momentum', studies: ['STD;RSI', 'STD;MACD'] },
  { id: 'volatility', label: 'Volatility', studies: ['STD;Bollinger_Bands', 'STD;Average_True_Range'] },
  { id: 'volume', label: 'Volume', studies: ['STD;Volume', 'STD;Visible%1Average%1Price'] },
  { id: 'full', label: 'Everything', studies: ['STD;EMA', 'STD;Bollinger_Bands', 'STD;RSI', 'STD;MACD', 'STD;Volume'] },
]

/*
 * The secondary panels. Each is a real TradingView widget whose script name was
 * verified against the CDN — see the note in components/TradingView.tsx, where
 * six plausible names turned out to 404.
 *
 * They are here because each answers a question the chart cannot. Technicals is
 * a second opinion on the same bars; profile and financials are the company
 * behind the ticker; the calendar is the macro events that move a whole book at
 * once; the screener and heatmap are how a name gets found in the first place.
 */
const TABS: { id: string; label: string; sub: string }[] = [
  { id: 'technicals', label: 'Technicals', sub: "TradingView's oscillator and moving-average gauge, on their data" },
  { id: 'profile', label: 'Profile', sub: 'the company behind the ticker — sector, employees, description' },
  { id: 'financials', label: 'Financials', sub: 'revenue, earnings, balance sheet and cash flow as TradingView reports them' },
  { id: 'news', label: 'News', sub: 'TradingView headlines for this symbol' },
  { id: 'calendar', label: 'Calendar', sub: 'high-importance economic events — the ones that move a whole book at once' },
  { id: 'screener', label: 'Screener', sub: 'TradingView’s stock screener, for finding the name in the first place' },
  { id: 'heatmap', label: 'Heatmap', sub: 'S&P 500 by market cap, coloured by change and grouped by sector' },
]

const INTERVALS = [
  { id: '1', label: '1m' },
  { id: '5', label: '5m' },
  { id: '15', label: '15m' },
  { id: '60', label: '1h' },
  { id: '240', label: '4h' },
  { id: 'D', label: '1D' },
  { id: 'W', label: '1W' },
  { id: 'M', label: '1M' },
]

export default function ChartingPage() {
  const [symbol, setSymbol] = useState('NASDAQ:NVDA')
  const [interval, setInterval] = useState('D')
  const [preset, setPreset] = useState('momentum')
  const [status, setStatus] = useState<TvStatus | null>(null)
  const [note, setNote] = useState<{ synthetic: boolean; note: string } | null>(null)
  const [native, setNative] = useState('NVDA')
  const [expiry, setExpiry] = useState('')
  const [tab, setTab] = useState('technicals')
  const [tall, setTall] = useState(false)
  const [scanOpen, setScanOpen] = useState(false)
  const [side, setSide] = useState<'stats' | 'depth'>('stats')

  const selectSymbol = (next: string, exchange?: string) => {
    const ticker = next.trim().toUpperCase()
    if (!ticker) return
    setSymbol(exchange ? `${exchange}:${ticker}` : ticker)
    setNative(ticker)
    setExpiry('')
    setScanOpen(false)
  }

  // Our own chain for whatever the TradingView chart is showing, so the two
  // panels are about the same underlying.
  const chain = useLive<ChainResponse>(
    () => api.chain(plainTicker(symbol)),
    [symbol],
    30000,
  )

  // Our own bars, through the same UDF endpoint the TradingView Charting
  // Library would call. If this chart is right, that integration is right.
  const bars = useLive<Candle[]>(
    async () => {
      const h = await api.udfHistory(native, 260)
      if (h.s !== 'ok' || !h.t) return []
      return h.t.map((t, i) => ({
        t, o: h.o![i], h: h.h![i], l: h.l![i], c: h.c![i], v: h.v![i],
      }))
    },
    [native],
    20000,
  )

  const stats = useLive<KeyStats>(() => api.keyStats(plainTicker(symbol)), [symbol], 15000)
  const levels = useLive<OptionsLevels>(
    () => api.optionsLevels(plainTicker(symbol)), [symbol], 60000)

  useEffect(() => {
    api.tvStatus().then(setStatus).catch(() => {})
    api.tvDatafeedNote().then(setNote).catch(() => {})
  }, [])

  const { ticker, proxyFor } = nativeFor(symbol)

  return (
    <Shell actions={<><div className="row" style={{ gap: 6, marginLeft: 'auto' }}>
     <SymbolSearch value={symbol} onSelect={selectSymbol} />
     <div className="tabs">
      {INTERVALS.map((i) => (
       <button
        key={i.id}
        className="tab"
        aria-selected={interval === i.id}
        onClick={() => setInterval(i.id)}
       >
        {i.label}
       </button>
      ))}
     </div>
    </div></>}>

      <div className="page">
        {/* --- terminal header: ticker identity and the two panel controls --- */}
        <div className="term-head">
          <div className="term-ticker">
            <h1>{ticker}</h1>
            <span className="badge">LIVE TV</span>
          </div>
          {/* When the chart is on a futures contract, every panel to its right
            * is showing the ETF proxy instead, because the engine has no
            * futures. Saying so here is the difference between a proxy and a
            * mislabelled number. */}
          <div className="term-sub">
            {proxyFor
              ? `chart ${proxyFor} futures · panels use ${ticker} as proxy`
              : 'market data feed'}
          </div>

          <div className="row" style={{ gap: 6, marginLeft: 'auto' }}>
            {/*
              * Named for what it does. The obvious label for this button is
              * "AI vision scan", and it would be a lie — nothing here looks at
              * the rendered chart, and there is no vision model in this
              * application. It reads the bars the chart is drawn from, so it
              * is called a series scan and the panel repeats why.
              */}
            <button className="btn" aria-pressed={scanOpen}
              onClick={() => { setScanOpen((v) => !v); setSide('stats') }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                strokeWidth="1.8" strokeLinecap="round" aria-hidden>
                <path d="M4 17l5-6 4 3 7-9" /></svg>
              Series scan
            </button>
            <div className="tabs">
              <button className="tab" aria-selected={side === 'stats'}
                onClick={() => setSide('stats')}>Stats</button>
              <button className="tab" aria-selected={side === 'depth'}
                onClick={() => setSide('depth')}>Depth</button>
            </div>
          </div>
        </div>

        {/* --- the terminal: chart, side column, execution rail --- */}
        <div className="terminal">
          <div className="term-panel">
            <TvWidget
              kind="advanced-chart"
              height={tall ? 'calc(100vh - var(--header-h) - 40px)' : 620}
              label="The TradingView chart"
              config={{
                autosize: true,
                symbol,
                interval,
                timezone: 'Etc/UTC',
                style: '1',
                allow_symbol_change: true,
                hide_side_toolbar: false,
                hide_top_toolbar: false,
                hide_legend: false,
                withdateranges: true,
                details: true,
                hotlist: true,
                calendar: true,
                studies: PRESETS.find((p) => p.id === preset)?.studies ?? [],
                show_popup_button: true,
                popup_width: '1200',
                popup_height: '800',
                save_image: true,
                watchlist: TAPE.map((t) => t.proName),
                backgroundColor: 'rgba(5, 10, 20, 1)',
                gridColor: 'rgba(155, 180, 218, 0.075)',
                support_host: 'https://www.tradingview.com',
              }}
            />
            <div className="term-panel-head" style={{ borderTop: '1px solid var(--chrome-border)', borderBottom: 0 }}>
              <span>indicators</span>
              <div className="tabs tabs-wrap" style={{ marginLeft: 'auto' }}>
                {PRESETS.map((p) => (
                  <button key={p.id} className="tab" aria-selected={preset === p.id}
                    onClick={() => setPreset(p.id)}>{p.label}</button>
                ))}
              </div>
              <button className="btn ghost" onClick={() => setTall((v) => !v)}>
                {tall ? 'Shrink' : 'Expand'}
              </button>
            </div>
          </div>

          <div className="term-panel">
            <div className="term-panel-head">
              <span>{side === 'stats' ? 'symbol' : 'depth'}</span>
              <LiveBadge state={stats} />
            </div>
            {side === 'stats'
              ? <SymbolPanel symbol={ticker} proxyFor={proxyFor} />
              : <DepthPanel symbol={ticker} />}
            {scanOpen && <ScanPanel symbol={ticker} onClose={() => setScanOpen(false)} />}
          </div>

          <ExecutionPanel symbol={ticker} last={stats.data?.last} />
        </div>

        <NewsTicker symbols={['SPY', 'QQQ', 'NVDA', 'AAPL', 'TSLA', 'AMD', 'META', 'COIN']} />

        <div className="banner">
          <span aria-hidden>▲</span>
          <span>
            <strong>This chart is TradingView&apos;s data, not this app&apos;s.</strong>{' '}
            The free widget renders TradingView&apos;s own feed and cannot place orders.
            The strategies, greeks and backtests elsewhere in MarketShark do not appear
            here — see the integration status below for what would change that.
          </span>
        </div>

        <div className="card">
          <div className="card-head">
            <div>
              <h2>{native} — this app&apos;s data</h2>
              <span className="sub">
                daily candles and volume, served by the same UDF datafeed the
                TradingView Charting Library would read
              </span>
            </div>
            <div className="row" style={{ gap: 8 }}>
              <SymbolField value={native} onChange={setNative} />
              <LiveBadge state={bars} />
            </div>
          </div>
          <div className="card-body flush">
            {bars.data && bars.data.length > 1 ? (
              <CandleChart candles={bars.data} symbol={native} />
            ) : bars.error ? (
              <div className="chart-unavailable" role="status">
                <strong>App-side candle data is unavailable</strong>
                <span>{bars.error}</span>
              </div>
            ) : (
              <div className="skeleton" style={{ height: 380 }} />
            )}
          </div>
          {bars.error && <div className="err">{bars.error}</div>}
        </div>

        {/*
          * The secondary TradingView surface. Tabbed rather than stacked
          * because these are seven full panels — laid out down the page they
          * would push the options chain a very long way below the fold, and
          * every one of them is a third-party iframe that costs a network
          * round trip. Only the selected tab is mounted, so only the one being
          * looked at is fetched.
          */}
        <div className="card">
          <div className="card-head">
            <div>
              <h2>{TABS.find((t) => t.id === tab)?.label}</h2>
              <span className="sub">{TABS.find((t) => t.id === tab)?.sub}</span>
            </div>
            <div className="tabs tabs-wrap">
              {TABS.map((t) => (
                <button key={t.id} className="tab" aria-selected={tab === t.id}
                  onClick={() => setTab(t.id)}>
                  {t.label}
                </button>
              ))}
            </div>
          </div>
          <div className="card-body flush">
            {tab === 'technicals' && (
              <TvWidget kind="technical-analysis" height={460} label="Technical analysis"
                config={{ symbol, interval: '1D', showIntervalTabs: true,
                          isTransparent: true, width: '100%', height: 460 }} />
            )}
            {tab === 'profile' && (
              <TvWidget kind="symbol-profile" height={460} label="Company profile"
                config={{ symbol, isTransparent: true, width: '100%', height: 460 }} />
            )}
            {tab === 'financials' && (
              <TvWidget kind="financials" height={520} label="Fundamental data"
                config={{ symbol, displayMode: 'regular', largeChartUrl: '',
                          isTransparent: true, width: '100%', height: 520 }} />
            )}
            {tab === 'news' && (
              <TvWidget kind="timeline" height={520} label="News timeline"
                config={{ feedMode: 'symbol', symbol, isTransparent: true,
                          displayMode: 'regular', width: '100%', height: 520 }} />
            )}
            {tab === 'calendar' && (
              <TvWidget kind="events" height={520} label="Economic calendar"
                config={{ isTransparent: true, width: '100%', height: 520,
                          importanceFilter: '0,1', countryFilter: 'us,eu,gb,jp' }} />
            )}
            {tab === 'screener' && (
              <TvWidget kind="screener" height={560} label="Screener"
                config={{ defaultColumn: 'overview', defaultScreen: 'most_capitalized',
                          market: 'america', showToolbar: true, isTransparent: true,
                          width: '100%', height: 560 }} />
            )}
            {tab === 'heatmap' && (
              <TvWidget kind="stock-heatmap" height={560} label="Stock heatmap"
                config={{ dataSource: 'SPX500', blockSize: 'market_cap_basic',
                          blockColor: 'change', grouping: 'sector', locale: 'en',
                          symbolUrl: '', hasTopBar: true, isDataSetEnabled: true,
                          isZoomEnabled: true, hasSymbolTooltip: true,
                          isTransparent: true, width: '100%', height: 560 }} />
            )}
          </div>
        </div>

        {/*
          * The options surface is ours, and it has to be: TradingView's public
          * embed library has no options-chain or greeks widget. Chains live
          * inside their own platform, not in the widgets anyone can drop into a
          * page. So this is our engine's chain sitting beside their chart —
          * which is the honest arrangement, and also the better one, since the
          * greeks here are the same ones the strategy tester prices from.
          */}
        <div className="card">
          <div className="card-head">
            <div>
              <h2>Options chain · {plainTicker(symbol)}</h2>
              <span className="sub">
                priced by this app&apos;s engine — TradingView publishes no embeddable
                options widget, so this is the one panel here that is not theirs
              </span>
            </div>
            <div className="row" style={{ gap: 8 }}>
              <LiveBadge state={chain} />
            </div>
          </div>
          <div className="card-body flush">
            {chain.data ? (
              <ChainGrid
                chain={chain.data}
                expiration={expiry || chain.data.expirations[1] || chain.data.expirations[0]}
                onExpirationChange={setExpiry}
              />
            ) : (
              <div className="skeleton" style={{ height: 320 }} />
            )}
          </div>
        </div>

        {levels.data && (levels.data.max_pain || levels.data.gex) && (
          <div className="card">
            <div className="card-head">
              <div>
                <h2>Options levels · {levels.data.symbol}</h2>
                <span className="sub">
                  max pain and dealer gamma for {levels.data.expiration} — heuristics
                  with their assumptions attached, not forecasts
                </span>
              </div>
              <div className="row" style={{ gap: 8 }}>
                {levels.data.synthetic && <span className="chip">synthetic OI</span>}
                <LiveBadge state={levels} />
              </div>
            </div>
            <div className="card-body">
              <div className="tiles">
                {levels.data.max_pain && (
                  <div className="tile">
                    <div className="k">Max pain</div>
                    <div className="v num">{levels.data.max_pain.max_pain.toFixed(1)}</div>
                    <div className="n">
                      minimum-payout strike · {(levels.data.max_pain.total_call_oi / 1000).toFixed(0)}k call /{' '}
                      {(levels.data.max_pain.total_put_oi / 1000).toFixed(0)}k put OI
                    </div>
                  </div>
                )}
                {levels.data.gex && (
                  <>
                    <div className="tile">
                      <div className="k">Net GEX (1% move)</div>
                      <div className={`v num ${levels.data.gex.net_gex >= 0 ? 'pos' : 'neg'}`}>
                        ${(levels.data.gex.net_gex / 1e6).toFixed(1)}M
                      </div>
                      <div className="n">{levels.data.gex.regime}</div>
                    </div>
                    <div className="tile">
                      <div className="k">Gamma flip</div>
                      <div className="v num">
                        {levels.data.gex.flip_strike?.toFixed(1) ?? '—'}
                      </div>
                      <div className="n">cumulative GEX crosses zero here</div>
                    </div>
                  </>
                )}
              </div>
              {levels.data.gex && (
                <p className="tiny muted" style={{ margin: '8px 0 0' }}>
                  {levels.data.gex.assumption}
                  {levels.data.synthetic && ' Open interest here is generated, so this panel demonstrates the computation rather than reading real positioning.'}
                </p>
              )}
            </div>
          </div>
        )}

        {note?.synthetic && (
          <div className="card">
            <div className="card-head">
              <h2>A note on the two data sources on this page</h2>
            </div>
            <div className="card-body tiny muted">
              The chart above is live TradingView data. Every other number in this
              application currently comes from the synthetic provider. {note.note} Do not
              read the two against each other — the chart is real and the analytics are
              generated, so any apparent agreement or disagreement between them is
              meaningless.
            </div>
          </div>
        )}

        {status && (
          <>
            <div className="card">
              <div className="card-head">
                <div>
                  <h2>Integration status</h2>
                  <span className="sub">what is wired, and what each tier would take</span>
                </div>
              </div>
              <div className="card-body flush">
                <Tier t={status.widget} />
                <Tier t={status.charting_library} />
                <Tier t={status.broker_integration} />
              </div>
              <div className="card-body tiny muted">{status.summary}</div>
            </div>

            {status.broker_integration.mapped_from && (
              <div className="card">
                <div className="card-head">
                  <div>
                    <h2>Broker bridge — mapped, not connected</h2>
                    <span className="sub">
                      where TradingView&apos;s broker contract would land on endpoints
                      that already exist
                    </span>
                  </div>
                </div>
                <div className="card-body flush">
                  <table className="table">
                    <thead>
                      <tr><th>TradingView broker method</th><th>This API</th></tr>
                    </thead>
                    <tbody>
                      {Object.entries(status.broker_integration.mapped_from).map(([k, v]) => (
                        <tr key={k}>
                          <td><code>{k}</code></td>
                          <td style={{ textAlign: 'left' }}><code>{v}</code></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="card-body tiny muted">
                  This table is a map, not a connection. Order submission is disabled in
                  the broker layer and TradingView has approved nothing — a mapping that
                  reads as though it were live is exactly the failure this page exists to
                  avoid.
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </Shell>
  )
}

function Tier({ t }: { t: TvTier }) {
  return (
    <div className="tier">
      <div className="tier-head">
        <span className={`dot ${t.live ? 'on' : 'off'}`} aria-hidden />
        <span className="tier-name">{t.tier}</span>
        <span className={`chip ${t.live ? 'accent' : ''}`}>{t.live ? 'live' : 'not wired'}</span>
        {t.trading ? (
          <span className="chip">order entry</span>
        ) : (
          <span className="chip">charts only</span>
        )}
      </div>
      <div className="tier-body">
        <div className="kv"><span>Requires</span><strong>{t.requires}</strong></div>
        {t.data && <div className="kv"><span>Data source</span><strong>{t.data}</strong></div>}
        {t.datafeed_ready && (
          <div className="kv">
            <span>Datafeed</span>
            <strong className="pos">implemented at <code>{t.datafeed_url}</code></strong>
          </div>
        )}
        <p className="tiny muted" style={{ margin: '6px 0 0' }}>{t.note}</p>
        {t.blocked_on && (
          <ul className="blocked">
            {t.blocked_on.map((b) => <li key={b}>{b}</li>)}
          </ul>
        )}
      </div>
    </div>
  )
}
