'use client'

import { useEffect, useState } from 'react'
import { useLive } from '@/components/live'
import { api } from '@/lib/api'
import type { BrokerStatus, ChartScan, KeyStats, NewsFeedResponse } from '@/lib/types'

/**
 * Terminal panels: symbol statistics, the series scan, depth, execution.
 *
 * Three of these four are straightforward. The other two are the ones a trading
 * terminal usually lies about, so they are worth stating plainly here:
 *
 * **Depth.** A DOM shows the resting order book — the queue of bids and offers
 * at each price. That is Level 2 data, it comes from an exchange feed, and this
 * application does not have one. What it does have is top of book: one bid, one
 * ask, one spread. So the panel shows exactly that and says the ladder is
 * absent and why. Generating plausible-looking ladder rows would be the single
 * most dangerous thing in this codebase — depth is read as a picture of real
 * resting liquidity, and someone would size a trade against it.
 *
 * **Execution.** There is no broker connection, so the ticket is inert and
 * says so. The blur behind the lock is a real order form rendered disabled,
 * not a screenshot: what you see is what would become live, which makes the
 * locked state informative rather than decorative.
 */

function fmtBig(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  const a = Math.abs(n)
  if (a >= 1e12) return `${(n / 1e12).toFixed(2)}T`
  if (a >= 1e9) return `${(n / 1e9).toFixed(2)}B`
  if (a >= 1e6) return `${(n / 1e6).toFixed(2)}M`
  if (a >= 1e3) return `${(n / 1e3).toFixed(1)}K`
  return n.toFixed(2)
}

function fmt(n: number | null | undefined, dp = 2): string {
  return n === null || n === undefined ? '—' : n.toFixed(dp)
}

/** Where the last price sits inside a low–high band, clamped to [0, 1]. */
function position(low: number | null, high: number | null, at: number): number | null {
  if (low === null || high === null || high <= low) return null
  return Math.min(1, Math.max(0, (at - low) / (high - low)))
}

function RangeBar({ low, high, at, label }: {
  low: number | null; high: number | null; at: number; label: string
}) {
  const p = position(low, high, at)
  return (
    <div className="range-row">
      <span className="range-end num">{fmt(low)}</span>
      <div className="range-track">
        <div className="range-fill" style={{ width: `${(p ?? 0) * 100}%` }} />
        {p !== null && <span className="range-mark" style={{ left: `${p * 100}%` }} aria-hidden />}
      </div>
      <span className="range-end num">{fmt(high)}</span>
      <span className="range-label">{label}</span>
    </div>
  )
}

/* ============================================================== symbol */

export function SymbolPanel({ symbol, proxyFor }: { symbol: string; proxyFor?: string | null }) {
  const stats = useLive<KeyStats>(() => api.keyStats(symbol), [symbol], 15000)
  const d = stats.data
  if (!d) return <div className="skeleton" style={{ height: 320, margin: 12 }} />

  const up = (d.change ?? 0) >= 0
  return (
    <div className="sym-panel">
      <div className="sym-ident">
        <div className="sym-name">{d.name ?? d.symbol}</div>
        <div className="sym-meta">
          {d.exchange ?? '—'}
          {d.sector ? <> · {d.sector}</> : null}
        </div>
        {/* The chart is on the futures contract and this panel is not. Company
          * facts — sector, market cap, earnings — belong to the proxy, and a
          * futures contract has none of them, so the substitution has to be
          * visible rather than inferred from a ticker that quietly changed. */}
        {proxyFor && (
          <div className="sym-proxy">
            standing in for {proxyFor} futures — the figures below are {d.symbol}&apos;s,
            and the two diverge overnight and around roll
          </div>
        )}
      </div>

      <div className="sym-price">
        <span className="num">{fmt(d.last)}</span>
        <small>USD</small>
      </div>
      <div className={`sym-change num ${up ? 'pos' : 'neg'}`}>
        {up ? '+' : ''}{fmt(d.change)} &nbsp; {up ? '+' : ''}{fmt(d.change_pct)}%
      </div>

      <RangeBar low={d.day_low} high={d.day_high} at={d.last} label="Day" />
      {/*
        * The 52-week label is conditional on there being 52 weeks of bars. A
        * range taken over eight months is a real number and a false label, and
        * the label is the part people read.
        */}
      <RangeBar low={d.range_52w.low} high={d.range_52w.high} at={d.last}
        label={d.range_52w.complete ? '52wk' : `${d.range_52w.sessions}d`} />

      <div className="sym-stats">
        <div className="kv"><span>Bid / Ask</span>
          <strong className="num">{fmt(d.bid)} / {fmt(d.ask)}</strong></div>
        <div className="kv"><span>Volume</span>
          <strong className="num">{fmtBig(d.volume)}</strong></div>
        <div className="kv">
          <span>Avg volume ({d.avg_volume_sessions}d)</span>
          <strong className="num">{fmtBig(d.avg_volume)}</strong>
        </div>
        <div className="kv"><span>Market cap</span>
          <strong className="num">{fmtBig(d.market_cap)}</strong></div>
        <div className="kv">
          <span>Next earnings</span>
          <strong>
            {d.days_to_earnings !== null ? `in ${d.days_to_earnings} days` : '—'}
          </strong>
        </div>
      </div>
      {d.earnings_note && <p className="sym-note">{d.earnings_note}</p>}
      {stats.error && <p className="sym-note err-text">{stats.error}</p>}
    </div>
  )
}

/* ================================================================ scan */

const KIND_ICON: Record<string, string> = {
  trend: 'M4 17l5-6 4 3 7-9',
  momentum: 'M12 3v18M6 9v9M18 6v12',
  volatility: 'M3 12h3l3-7 4 14 3-7h5',
  support: 'M4 18h16M8 14l4 4 4-4',
  resistance: 'M4 6h16M8 10l4-4 4 4',
}

export function ScanPanel({ symbol, onClose }: { symbol: string; onClose: () => void }) {
  const [data, setData] = useState<ChartScan | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    setData(null)
    setError(null)
    api.chartScan(symbol)
      .then((r) => { if (live) setData(r) })
      .catch((e) => { if (live) setError(String(e)) })
    return () => { live = false }
  }, [symbol])

  return (
    <div className="scan-sheet">
      <div className="scan-head">
        <strong>Series scan · {symbol}</strong>
        <button className="rail-icon-btn" onClick={onClose} aria-label="Close scan">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="2" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18" /></svg>
        </button>
      </div>

      {error && <div className="err">{error}</div>}
      {!data && !error && <div className="skeleton" style={{ height: 200 }} />}

      {data && (
        <>
          <div className="scan-list">
            {data.findings.map((f, i) => (
              <div className="scan-item" key={`${f.kind}-${i}`}>
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                  strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"
                  className={`scan-icon k-${f.kind}`} aria-hidden>
                  <path d={KIND_ICON[f.kind] ?? KIND_ICON.trend} />
                </svg>
                <div>
                  <div className="scan-label">{f.label}</div>
                  <div className="scan-detail">{f.detail}</div>
                </div>
              </div>
            ))}
            {data.findings.length === 0 && (
              <p className="tiny muted" style={{ padding: 12 }}>Nothing to report.</p>
            )}
          </div>
          <p className="scan-note">{data.note}</p>
        </>
      )}
    </div>
  )
}

/* =============================================================== depth */

export function DepthPanel({ symbol }: { symbol: string }) {
  const stats = useLive<KeyStats>(() => api.keyStats(symbol), [symbol], 5000)
  const d = stats.data
  const spread = d && d.bid !== null && d.ask !== null ? d.ask - d.bid : null
  const bps = spread !== null && d ? (spread / d.last) * 10000 : null

  return (
    <div className="depth-panel">
      <div className="depth-top">
        <div className="depth-side bid">
          <span className="depth-tag">BID</span>
          <span className="num">{fmt(d?.bid)}</span>
        </div>
        <div className="depth-mid">
          <span className="depth-tag">SPREAD</span>
          <span className="num">{spread === null ? '—' : spread.toFixed(2)}</span>
          <small>{bps === null ? '' : `${bps.toFixed(1)} bps`}</small>
        </div>
        <div className="depth-side ask">
          <span className="depth-tag">ASK</span>
          <span className="num">{fmt(d?.ask)}</span>
        </div>
      </div>

      {/*
        * The reason there is no ladder, stated where the ladder would be. This
        * is the one panel where a convincing-looking placeholder would be
        * actively harmful: depth is read as resting liquidity, and someone
        * would size against it.
        */}
      <div className="empty-state" style={{ padding: '20px 16px' }}>
        <strong>No order book.</strong>
        <p>
          A depth ladder needs Level 2 exchange data, which is a paid feed this app is
          not connected to. Top of book above is the whole of what is available —
          one bid, one ask. Nothing is drawn below it, because a ladder of invented
          sizes is the kind of thing people size a position against.
        </p>
      </div>
    </div>
  )
}

/* =========================================================== execution */

export function ExecutionPanel({ symbol, last }: { symbol: string; last?: number }) {
  const [brokers, setBrokers] = useState<BrokerStatus[] | null>(null)
  useEffect(() => { api.brokerStatus().then(setBrokers).catch(() => {}) }, [])
  const connected = (brokers ?? []).filter((b) => b.connected)
  const tradable = connected.filter((b) => b.trading_enabled)

  /*
   * Two modes, one honest split. Paper is a WORKING ticket: it fills into the
   * paper book at the live quote's mid, server-side, and the Portfolio page
   * marks it from then on. Live is the locked pane — no broker permits order
   * routing yet, and a disabled form is the truthful rendering of that.
   * The default is paper, because the working mode should be the one a click
   * lands on.
   */
  const [mode, setMode] = useState<'paper' | 'live'>('paper')
  const [side, setSide] = useState<'BUY' | 'SELL'>('BUY')
  const [qty, setQty] = useState(100)
  const [ticket, setTicket] = useState<'idle' | 'busy'>('idle')
  const [done, setDone] = useState<{ name: string; price: number; method: string } | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const submit = async () => {
    setTicket('busy')
    setErr(null)
    try {
      const r = await api.paperOrder({ symbol, side, quantity: qty })
      setDone({ name: r.position.name, price: r.fill.price, method: r.fill.method })
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setTicket('idle')
    }
  }

  return (
    <aside className="exec-panel">
      <div className="exec-head">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          strokeWidth="1.8" aria-hidden><path d="M3 12h4l3-8 4 16 3-8h4" /></svg>
        <span>EXECUTION</span>
        <div className="tabs" style={{ marginLeft: 6 }}>
          <button className="tab" aria-selected={mode === 'paper'}
            onClick={() => setMode('paper')}>Paper</button>
          <button className="tab" aria-selected={mode === 'live'}
            onClick={() => setMode('live')}>Live</button>
        </div>
        <span className="exec-status">
          {mode === 'paper'
            ? <><i className="dot paper" aria-hidden />PAPER</>
            : <><i className="dot off" aria-hidden />OFFLINE</>}
        </span>
      </div>

      {mode === 'paper' && (
        <div className="exec-ticket">
          <div className="exec-sides">
            <button className={`exec-side buy ${side === 'BUY' ? 'is-on' : ''}`}
              onClick={() => setSide('BUY')}>BUY</button>
            <button className={`exec-side sell ${side === 'SELL' ? 'is-on' : ''}`}
              onClick={() => setSide('SELL')}>SELL</button>
          </div>
          <label className="field"><span>Quantity (shares)</span>
            <input className="input" type="number" min={1} max={100000} value={qty}
              onChange={(e) => setQty(Math.max(1, Number(e.target.value) || 1))} /></label>
          <div className="kv"><span>Fills at</span>
            <strong className="num">
              {last ? `~$${last.toFixed(2)} (live mid at execution)` : 'live mid at execution'}
            </strong></div>
          <div className="kv"><span>Estimated {side === 'BUY' ? 'cost' : 'credit'}</span>
            <strong className="num">{last ? `$${(last * qty).toFixed(2)}` : '—'}</strong></div>
          <button className="btn primary" onClick={submit} disabled={ticket === 'busy'}>
            {ticket === 'busy' ? 'Filling…' : `${side} ${qty} ${symbol} · paper`}
          </button>
          {done && (
            <div className="exec-fill">
              <strong>Filled: {done.name} @ ${done.price.toFixed(2)}</strong>
              <span className="tiny muted">{done.method} · now marked on the{' '}
                <a href="/portfolio">Portfolio</a> page</span>
            </div>
          )}
          {err && <p className="tiny err-text">{err}</p>}
          <p className="exec-paper-note">
            Paper fills have no queue, partial fills, slippage or fees — expect live
            results to be worse than paper at the same decisions. Options structures are
            built in the <a href="/lab">Strategy Lab</a>, which prices every leg in view,
            and land in the same paper book.
          </p>
        </div>
      )}

      {mode === 'live' && (<>

      {/* A real ticket, rendered disabled — not a picture of one. What is behind
        * the lock is what would become live, so the locked state is showing the
        * truth rather than a mock-up of it. */}
      <div className="exec-body" aria-hidden>
        <div className="exec-sides">
          <button className="exec-side buy" disabled>BUY</button>
          <button className="exec-side sell" disabled>SELL</button>
        </div>
        <label className="field"><span>Order type</span>
          <div className="tabs">
            {['Market', 'Limit', 'Stop'].map((t) => (
              <button key={t} className="tab" disabled aria-selected={t === 'Market'}>{t}</button>
            ))}
          </div>
        </label>
        <label className="field"><span>Quantity</span>
          <input className="input" defaultValue={100} disabled /></label>
        <label className="field"><span>Limit price</span>
          <input className="input" defaultValue={last ? last.toFixed(2) : ''} disabled /></label>
        <div className="kv"><span>Estimated cost</span>
          <strong className="num">{last ? `$${(last * 100).toFixed(2)}` : '—'}</strong></div>
      </div>

      <div className="exec-lock">
        <span className="lock-icon" aria-hidden>
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="1.6"><rect x="4" y="10" width="16" height="10" rx="2" />
            <path d="M8 10V7a4 4 0 0 1 8 0v3" /></svg>
        </span>
        <strong>Terminal locked</strong>
        <p>
          {connected.length === 0
            ? 'No brokerage is connected, so nothing here can send an order. Connecting one is a real integration — credentials, account selection and an order-routing agreement — not a switch in this UI.'
            : 'Credentials are present, but order routing is still disabled server-side until it has been exercised against a sandbox account.'}
        </p>
        {/* The real state of every adapter, read from /api/brokers/status —
          * not a decorative button. "Connect broker" with nowhere to go is
          * exactly the kind of thing that makes a locked panel feel like a
          * mock-up, so this lists what each one actually needs. */}
        <div className="broker-list">
          {(brokers ?? []).map((b) => (
            <div className="broker-row" key={b.id}>
              <div className="broker-name">
                <i className={`dot ${b.connected ? 'on' : 'off'}`} aria-hidden />
                {b.name}
                <span className="chip">
                  {b.trading_enabled ? 'orders enabled'
                    : b.connected ? 'data only' : 'not connected'}
                </span>
              </div>
              <p>{b.notes}</p>
            </div>
          ))}
          {brokers === null && <div className="skeleton" style={{ height: 90 }} />}
        </div>
      </div>

      </>)}

      <p className="exec-foot">
        {mode === 'paper'
          ? 'Paper orders never touch a broker: the fill happens in this app\u2019s own book, priced from the live quote server-side.'
          : tradable.length === 0
            ? `Order entry is disabled in the broker layer itself, not only in this panel — so a UI bug cannot route a live order for ${symbol}.`
            : `${tradable.length} broker(s) permit order routing. This panel still does not send orders — the Strategy Lab does, through the preview-then-submit path.`}
      </p>
    </aside>
  )
}

/* ============================================================== ticker */

export function NewsTicker({ symbols }: { symbols: string[] }) {
  const feed = useLive<NewsFeedResponse>(
    () => api.watchlistNews(symbols, 24), [symbols.join(',')], 60000,
  )
  const items = feed.data?.items ?? []
  if (!items.length) return null

  // Duplicated once so the marquee can loop without a visible jump: the second
  // copy is under the first at the moment the animation resets.
  const strip = [...items, ...items]
  return (
    <div className="news-ticker">
      <span className="ticker-tag">
        <i className="pulse-dot" aria-hidden /> HEADLINES
      </span>
      <div className="ticker-viewport">
        <div className="ticker-track">
          {strip.map((it, i) => (
            <a className="ticker-item" key={`${it.id}-${i}`} href={it.url}
              target="_blank" rel="noreferrer">
              {/* Coloured by measured impact band, and `unscored` is its own
                * state rather than a zero — an item with no completed session
                * behind it has not been measured, which is different from
                * having been measured as unimportant. */}
              <b className={`t-${it.scoring.band}`}>{it.symbol}</b>
              {it.title}
              <small>{it.publisher}</small>
            </a>
          ))}
        </div>
      </div>
    </div>
  )
}
