'use client'

import Link from 'next/link'
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'next/navigation'
import { Shell } from '@/components/Shell'
import { ChainGrid } from '@/components/ChainGrid'
import { EarningsPanel } from '@/components/EarningsPanel'
import { SavedStrategies } from '@/components/SavedStrategies'
import { PayoffChart } from '@/components/PayoffChart'
import { PLHeatmap } from '@/components/PLHeatmap'
import { PriceChart } from '@/components/PriceChart'
import { StrategyPicker } from '@/components/StrategyPicker'
import { VolPanel } from '@/components/VolPanel'
import {
  ExplainPanel,
  GreeksPanel,
  InsightList,
  LegTable,
  MetricTiles,
  ParamControls,
  VolSensitivity,
} from '@/components/panels'
import { api } from '@/lib/api'
import { money, pct } from '@/lib/format'
import type {
  AnalyzeResponse,
  BarPoint,
  Category,
  ChainResponse,
  EarningsReport,
  Quote,
  SavedStrategy,
  TemplateMeta,
  VolResponse,
} from '@/lib/types'

const WATCHLIST = ['SPY', 'QQQ', 'NVDA', 'AAPL', 'TSLA', 'AMD', 'META', 'COIN']
type Tab = 'payoff' | 'surface' | 'chain' | 'volatility' | 'earnings'

export default function Page() {
  // useSearchParams needs a Suspense boundary for static prerendering.
  return (
    <Suspense fallback={<div className="page"><div className="skeleton" style={{ height: 400 }} /></div>}>
      <StrategyLab />
    </Suspense>
  )
}

function StrategyLab() {
  const searchParams = useSearchParams()
  const initial = (searchParams.get('symbol') || 'NVDA').toUpperCase()
  const [symbol, setSymbol] = useState(initial)
  const [symbolInput, setSymbolInput] = useState(initial)
  const [templateId, setTemplateId] = useState('iron_condor')
  const [params, setParams] = useState<Record<string, number | string>>({})
  const [tab, setTab] = useState<Tab>('payoff')

  const [catalog, setCatalog] = useState<{ categories: Category[]; templates: TemplateMeta[] } | null>(null)
  const [analysis, setAnalysis] = useState<AnalyzeResponse | null>(null)
  const [quote, setQuote] = useState<Quote | null>(null)
  const [bars, setBars] = useState<BarPoint[]>([])
  const [chain, setChain] = useState<ChainResponse | null>(null)
  const [vol, setVol] = useState<VolResponse | null>(null)
  const [earnings, setEarnings] = useState<EarningsReport | null>(null)
  const [chainExpiration, setChainExpiration] = useState('')

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const requestId = useRef(0)

  const template = useMemo(
    () => catalog?.templates.find((t) => t.id === templateId) ?? null,
    [catalog, templateId],
  )

  /* ---- catalog ---- */
  useEffect(() => {
    api
      .templates()
      .then(setCatalog)
      .catch((e) => setError(String(e.message ?? e)))
  }, [])

  /* ---- reset params when the strategy changes ---- */
  useEffect(() => {
    if (!template) return
    const next: Record<string, number | string> = {}
    for (const p of template.params) next[p.key] = p.default
    setParams(next)
  }, [template])

  /* ---- symbol-scoped data ---- */
  useEffect(() => {
    let cancelled = false
    Promise.allSettled([
      api.quote(symbol),
      api.bars(symbol, 180),
      api.chain(symbol),
      api.volatility(symbol),
      api.earnings(symbol),
    ]).then(([q, b, c, v, e]) => {
      if (cancelled) return
      if (e.status === 'fulfilled') setEarnings(e.value)
      else setEarnings(null)
      if (q.status === 'fulfilled') setQuote(q.value)
      if (b.status === 'fulfilled') setBars(b.value.bars)
      if (c.status === 'fulfilled') {
        setChain(c.value)
        setChainExpiration((prev) =>
          c.value.expirations.includes(prev) ? prev : (c.value.expirations[2] ?? c.value.expirations[0] ?? ''),
        )
      }
      if (v.status === 'fulfilled') setVol(v.value)
    })
    return () => {
      cancelled = true
    }
  }, [symbol])

  /* ---- analysis, debounced so dragging a slider does not flood the API ---- */
  const runAnalysis = useCallback(async () => {
    if (!template) return
    const id = ++requestId.current
    setLoading(true)
    try {
      const result = await api.analyze({
        symbol,
        template: templateId,
        params,
        include_surface: true,
      })
      if (id !== requestId.current) return // a newer request already won
      setAnalysis(result)
      setError(null)
    } catch (e) {
      if (id !== requestId.current) return
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (id === requestId.current) setLoading(false)
    }
  }, [symbol, templateId, params, template])

  useEffect(() => {
    if (!template) return
    const t = setTimeout(runAnalysis, 140)
    return () => clearTimeout(t)
  }, [runAnalysis, template])

  /* ---- paper trade ----
   * The analyzed legs go into the paper book exactly as priced — entry prices
   * are the theoretical values on screen, so what the Portfolio page marks
   * from here on is the position the user was actually looking at. */
  const [papering, setPapering] = useState<'idle' | 'busy' | 'done'>('idle')
  const openAsPaper = async () => {
    if (!analysis) return
    setPapering('busy')
    try {
      await api.openPaperPosition({
        symbol: analysis.position.symbol,
        name: analysis.position.name,
        legs: analysis.position.legs.map((l) => ({
          kind: l.kind, quantity: l.quantity, strike: l.strike,
          dte: l.dte, entry_price: l.entry_price, iv: l.iv,
        })),
        notes: `opened from Strategy Lab at theoretical ${analysis.meta.chain_used ? 'chain' : 'model'} prices`,
      })
      setPapering('done')
      setTimeout(() => setPapering('idle'), 4000)
    } catch {
      setPapering('idle')
    }
  }

  const onParam = (key: string, value: number | string) =>
    setParams((p) => ({ ...p, [key]: value }))

  const loadSaved = (saved: SavedStrategy) => {
    // Re-prices against today's chain rather than restoring a frozen snapshot:
    // the useful question is what this structure costs now.
    if (saved.symbol !== symbol) {
      setSymbol(saved.symbol)
      setSymbolInput(saved.symbol)
    }
    if (saved.template_id) setTemplateId(saved.template_id)
    if (saved.params && Object.keys(saved.params).length) {
      setParams(saved.params)
    }
  }

  const submitSymbol = (e: React.FormEvent) => {
    e.preventDefault()
    const s = symbolInput.trim().toUpperCase()
    if (s) setSymbol(s)
  }

  const synthetic = analysis?.meta.data_source === 'synthetic'

  return (
    <Shell actions={<><form onSubmit={submitSymbol} className="row" style={{ gap: 6 }}>
     <input
      className="input"
      style={{ width: 108, textTransform: 'uppercase' }}
      value={symbolInput}
      onChange={(e) => setSymbolInput(e.target.value)}
      aria-label="Ticker symbol"
     />
     <button className="btn" type="submit">Load</button>
    </form>

    <div className="row" style={{ gap: 4, flexWrap: 'wrap' }}>
     {WATCHLIST.map((s) => (
      <button
       key={s}
       className="btn ghost tiny"
       aria-pressed={s === symbol}
       onClick={() => {
        setSymbol(s)
        setSymbolInput(s)
       }}
       style={{ padding: '3px 8px' }}
      >
       {s}
      </button>
     ))}
    </div>

    {quote && (
     <div className="row num" style={{ gap: 10, marginLeft: 'auto' }}>
      <strong style={{ fontSize: 17 }}>{quote.last.toFixed(2)}</strong>
      <span className={(quote.change ?? 0) >= 0 ? 'pos' : 'neg'}>
       {(quote.change ?? 0) >= 0 ? '+' : ''}
       {quote.change?.toFixed(2)} ({quote.change_pct?.toFixed(2)}%)
      </span>
      <span className="chip">{quote.source}</span>
     </div>
    )}</>}>

      {synthetic && (
        <div className="banner">
          <span aria-hidden>▲</span>
          <span>
            <strong>Synthetic data.</strong> No market data credentials are configured, so prices, chains
            and volatility are generated — internally consistent and deterministic, but not real. Add a
            Massive or Webull key to switch to live data; nothing else changes.
          </span>
        </div>
      )}

      <div className="shell">
        {/* ---------------- left rail ---------------- */}
        <aside className="rail">
          {catalog ? (
            <StrategyPicker
              categories={catalog.categories}
              templates={catalog.templates}
              selected={templateId}
              onSelect={setTemplateId}
            />
          ) : (
            <div className="skeleton" style={{ height: 380 }} />
          )}

          {template && (
            <div className="card">
              <div className="card-head">
                <h2>Parameters</h2>
                <span className="sub">{template.name}</span>
              </div>
              <div className="card-body">
                <ParamControls params={template.params} values={params} onChange={onParam} />
              </div>
            </div>
          )}

          <SavedStrategies
            symbol={symbol}
            templateId={templateId}
            templateName={template?.name ?? 'Setup'}
            params={params}
            spot={quote?.last ?? null}
            onLoad={loadSaved}
          />
        </aside>

        {/* ---------------- main ---------------- */}
        <main className="main">
          {error && <div className="err">{error}</div>}

          {analysis ? (
            <div className="card">
              <div className="card-head">
                <div>
                  <h2>{analysis.position.name}</h2>
                  <span className="sub">
                    {analysis.position.symbol} · {analysis.analysis.contracts} contracts ·{' '}
                    {analysis.analysis.is_credit ? 'credit' : 'debit'}{' '}
                    {money(Math.abs(analysis.analysis.net_debit), 0)}
                  </span>
                </div>
                <div className="row" style={{ gap: 6 }}>
                  {!analysis.analysis.risk_defined && (
                    <span className="chip" style={{ color: 'var(--status-serious)' }}>
                      ⚠ undefined risk
                    </span>
                  )}
                  {analysis.earnings?.has_event && (
                    <button
                      className="chip"
                      style={{ color: 'var(--status-warning)', cursor: 'pointer' }}
                      onClick={() => setTab('earnings')}
                      title="An earnings announcement falls inside this expiry"
                    >
                      ▲ earnings in {analysis.earnings.events_in_window[0]?.days_away}d
                      {analysis.earnings.implied_move_pct
                        ? ` · ${(analysis.earnings.implied_move_pct * 100).toFixed(1)}% implied`
                        : ''}
                    </button>
                  )}
                  <span className="chip">{analysis.meta.chain_used ? 'chain-priced' : 'model-priced'}</span>
                  {papering === 'done' ? (
                    <Link className="btn" href="/portfolio">Opened — view in Portfolio</Link>
                  ) : (
                    <button className="btn" onClick={openAsPaper} disabled={papering === 'busy'}
                      title="Put this structure in the paper book at the prices shown — the Portfolio page then marks it against live quotes">
                      {papering === 'busy' ? 'Opening…' : 'Paper trade'}
                    </button>
                  )}
                  {loading && <span className="chip">updating…</span>}
                </div>
              </div>
              <MetricTiles data={analysis} />
            </div>
          ) : (
            <div className="skeleton" style={{ height: 110 }} />
          )}

          <div className="card">
            <div className="card-head">
              <div className="tabs" role="tablist">
                {(
                  [
                    ['payoff', 'Risk graph'],
                    ['surface', 'P/L surface'],
                    ['chain', 'Option chain'],
                    ['volatility', 'Volatility'],
                    ['earnings', 'Earnings'],
                  ] as [Tab, string][]
                ).map(([id, label]) => (
                  <button key={id} className="tab" role="tab" aria-selected={tab === id} onClick={() => setTab(id)}>
                    {label}
                  </button>
                ))}
              </div>
              <span className="sub">
                {tab === 'payoff' && 'profit and loss against price, at expiry and along the way'}
                {tab === 'surface' && 'every price and every day between now and expiry'}
                {tab === 'chain' && `${chain?.expirations.length ?? 0} expirations quoted`}
                {tab === 'volatility' && 'term structure, skew, implied versus realized'}
                {tab === 'earnings' && (earnings?.next_event
                  ? `next report ${earnings.next_event.event_date}`
                  : 'calendar, implied move, historical reactions')}
              </span>
            </div>

            <div className="card-body flush">
              {tab === 'payoff' &&
                (analysis ? <PayoffChart data={analysis} /> : <div className="skeleton" style={{ height: 420 }} />)}
              {tab === 'surface' &&
                (analysis?.surface ? <PLHeatmap data={analysis} /> : <div className="skeleton" style={{ height: 320 }} />)}
              {tab === 'chain' &&
                (chain ? (
                  <ChainGrid chain={chain} expiration={chainExpiration} onExpirationChange={setChainExpiration} />
                ) : (
                  <div className="skeleton" style={{ height: 320 }} />
                ))}
              {tab === 'volatility' &&
                (vol ? <VolPanel vol={vol} /> : <div className="skeleton" style={{ height: 320 }} />)}
              {tab === 'earnings' &&
                (earnings ? <EarningsPanel report={earnings} />
                  : <div className="skeleton" style={{ height: 320 }} />)}
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <h2>{symbol} price history</h2>
              <span className="sub">
                {analysis
                  ? `±1σ band over ${analysis.analysis.dte.toFixed(0)} days at ${pct(analysis.analysis.blended_iv, 1)} IV`
                  : 'daily candles'}
              </span>
            </div>
            <div className="card-body flush">
              <PriceChart
                bars={bars}
                symbol={symbol}
                expectedMove={analysis ? analysis.analysis.expected_move_1sd : null}
              />
            </div>
          </div>

          {analysis && (
            <div className="card">
              <div className="card-head">
                <h2>Legs</h2>
                <span className="sub">{analysis.position.pricing_model}</span>
              </div>
              <div className="card-body flush">
                <LegTable legs={analysis.position.legs} />
              </div>
            </div>
          )}
        </main>

        {/* ---------------- right rail ---------------- */}
        <aside className="rail rail-right">
          {analysis && (
            <div className="card">
              <div className="card-head">
                <h2>What the model flags</h2>
                <span className="sub">{analysis.insights.length}</span>
              </div>
              <div className="card-body flush">
                <InsightList insights={analysis.insights} />
              </div>
            </div>
          )}

          {analysis && (
            <div className="card">
              <div className="card-head">
                <h2>Net greeks</h2>
              </div>
              <div className="card-body flush">
                <GreeksPanel data={analysis} />
              </div>
            </div>
          )}

          {analysis && (
            <div className="card">
              <div className="card-head">
                <h2>Volatility sensitivity</h2>
                <span className="sub">parallel IV shift</span>
              </div>
              <div className="card-body flush">
                <VolSensitivity data={analysis} />
              </div>
            </div>
          )}

          {analysis && (
            <div className="card">
              <div className="card-head">
                <h2>How this strategy works</h2>
              </div>
              <ExplainPanel data={analysis} />
            </div>
          )}
        </aside>
      </div>
    </Shell>
  )
}
