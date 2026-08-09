'use client'

import { useState } from 'react'
import Link from 'next/link'
import { Shell } from '@/components/Shell'
import { LiveBadge, useLive } from '@/components/live'
import { Sparkline } from '@/components/charts'
import { api } from '@/lib/api'
import type { BrokerAccount, PortfolioRisk, PositionMark, PositionRiskRow } from '@/lib/types'

/**
 * Portfolio: every account the app can monitor, and every position in them.
 *
 * Two sources feed this page and they are kept visibly distinct. The paper
 * account is this app's own book — positions opened from the Strategy Lab or
 * by the agent, marked against live quotes, with aggregate greeks computed by
 * the same engine that priced them. Broker accounts are synced from the
 * broker's API when credentials are configured, and until then they appear as
 * cards that say exactly what connecting takes.
 *
 * What this page never does is blend the two into one total. A paper P&L added
 * to a live balance produces a number that is neither, and a monitoring page
 * has exactly one job: numbers that mean what they appear to mean.
 */

function money(n: number | null | undefined, dp = 2): string {
  if (n === null || n === undefined) return '—'
  const sign = n < 0 ? '-' : ''
  return `${sign}$${Math.abs(n).toFixed(dp)}`
}

function big(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  const a = Math.abs(n)
  if (a >= 1e9) return `$${(n / 1e9).toFixed(2)}B`
  if (a >= 1e6) return `$${(n / 1e6).toFixed(2)}M`
  if (a >= 1e3) return `$${(n / 1e3).toFixed(1)}K`
  return money(n)
}

/* ============================================================= accounts */

function AccountCard({ a }: { a: BrokerAccount }) {
  return (
    <div className={`acct-card ${a.connected ? '' : 'is-off'}`}>
      <div className="acct-head">
        <span className={`dot ${a.connected ? 'on' : 'off'}`} aria-hidden />
        <strong>{a.name}</strong>
        {/* "live" is only worn by an account that is actually connected — on an
          * unconnected card it reads as a status rather than a kind, and a
          * disconnected broker labelled "live" is the wrong thing to glance at. */}
        <span className="chip">
          {!a.connected ? 'not connected' : a.kind === 'paper' ? 'paper' : 'live'}
        </span>
        {a.kind === 'paper' && (
          <span className="chip" title="Whether positions survive an API restart">
            {a.persistent ? 'persisted' : `${a.backend} only`}
          </span>
        )}
      </div>

      {a.connected ? (
        <div className="acct-body">
          {a.open_positions !== undefined && (
            <div className="kv"><span>Open positions</span>
              <strong className="num">{a.open_positions}</strong></div>
          )}
          {a.balances && (
            <>
              <div className="kv"><span>Market value</span>
                <strong className="num">{big(a.balances.total_market_value)}</strong></div>
              <div className="kv"><span>Cash</span>
                <strong className="num">{big(a.balances.total_cash)}</strong></div>
              <div className="kv"><span>Buying power</span>
                <strong className="num">{big(a.balances.buying_power)}</strong></div>
              <div className="kv"><span>Unrealized</span>
                <strong className={`num ${(a.balances.unrealized_profit_loss ?? 0) >= 0 ? 'pos' : 'neg'}`}>
                  {big(a.balances.unrealized_profit_loss)}
                </strong></div>
            </>
          )}
          {a.balance_error && (
            <p className="tiny err-text">Balance sync failed: {a.balance_error}</p>
          )}
          {a.balance_note && <p className="tiny muted">{a.balance_note}</p>}
        </div>
      ) : (
        <div className="acct-body">
          <p className="tiny muted">{a.reason}</p>
        </div>
      )}
    </div>
  )
}

/* ============================================================ positions */

function MarksRow({ id }: { id: string }) {
  const marks = useLive<PositionMark[]>(
    async () => (await api.positionMarks(id)).marks, [id], 60000,
  )
  const values = (marks.data ?? [])
    .map((m) => m.unrealized)
    .filter((v): v is number => typeof v === 'number')
  if (!marks.data) return <div className="skeleton" style={{ height: 44 }} />
  if (values.length < 2) {
    return (
      <p className="tiny muted" style={{ margin: '6px 0' }}>
        {values.length === 0 ? 'No marks recorded yet.' : 'One mark recorded.'}{' '}
        Marks are measurements taken when you press “record marks” (or a scheduled
        job does) — the curve is not reconstructed after the fact, because that
        would mean guessing what implied volatility was on each day.
      </p>
    )
  }
  return (
    <div className="row" style={{ gap: 10, alignItems: 'center', margin: '4px 0' }}>
      <Sparkline values={values} width={220} height={36} />
      <span className="tiny muted">{values.length} marks · unrealized P&L over time</span>
    </div>
  )
}

function PositionRow({ r, onChanged }: { r: PositionRiskRow; onChanged: () => void }) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    try { await fn(); onChanged() } finally { setBusy(false) }
  }

  if (r.error) {
    return (
      <tr><td>{r.symbol}</td>
        <td colSpan={8} style={{ textAlign: 'left' }} className="err-text tiny">{r.error}</td></tr>
    )
  }
  const up = (r.unrealized ?? 0) >= 0
  return (
    <>
      <tr className="pos-row" onClick={() => setOpen((v) => !v)}>
        <td><strong>{r.symbol}</strong></td>
        <td style={{ textAlign: 'left' }}>{r.name}</td>
        <td className="num">{r.spot?.toFixed(2) ?? '—'}</td>
        <td className={`num ${up ? 'pos' : 'neg'}`}>{money(r.unrealized)}</td>
        <td className="num">
          {r.max_loss_unbounded
            ? <span className="neg" title="This position's loss is not capped">unbounded</span>
            : money(r.max_loss)}
        </td>
        <td className="num">{r.probability_of_profit !== undefined
          ? `${(r.probability_of_profit * 100).toFixed(0)}%` : '—'}</td>
        <td className="num">{r.dte ?? '—'}</td>
        <td className="num">{r.greeks ? r.greeks.delta.toFixed(1) : '—'}</td>
        <td className="num">{r.greeks ? r.greeks.theta.toFixed(1) : '—'}</td>
      </tr>
      {open && (
        <tr className="pos-detail">
          <td colSpan={9}>
            <MarksRow id={r.id} />
            <div className="row" style={{ gap: 8 }}>
              <button className="btn" disabled={busy}
                onClick={(e) => { e.stopPropagation(); act(() => api.closePosition(r.id)) }}>
                Close position
              </button>
              <button className="btn ghost" disabled={busy}
                onClick={(e) => { e.stopPropagation(); act(() => api.deletePosition(r.id)) }}>
                Delete
              </button>
              <span className="tiny muted">
                Close records an exit against the book; delete removes the record entirely.
              </span>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

/* ================================================================= page */

export default function PortfolioPage() {
  const [marking, setMarking] = useState(false)
  const accounts = useLive<{ accounts: BrokerAccount[] }>(() => api.brokerAccounts(), [], 30000)
  const risk = useLive<PortfolioRisk>(() => api.portfolioRiskFull(), [], 15000)

  const d = risk.data
  const rows = d?.detail ?? []

  // One change handler for anything that mutates the book. The close bug this
  // fixes was visible on screen: the positions table refreshed immediately but
  // the account card kept saying the old count for up to 30 seconds, and two
  // numbers disagreeing about the same book is worse than either being stale.
  const bookChanged = () => { risk.refresh(); accounts.refresh() }

  const recordMarks = async () => {
    setMarking(true)
    try { await api.portfolioRiskFull(true); risk.refresh() } finally { setMarking(false) }
  }

  return (
    <Shell actions={
      <div className="row" style={{ gap: 8, marginLeft: 'auto' }}>
        <button className="btn" onClick={recordMarks} disabled={marking || !rows.length}
          title="Write one mark per open position — measurements, not reconstructions">
          {marking ? 'Recording…' : 'Record marks'}
        </button>
        <LiveBadge state={risk} />
      </div>
    }>
      <div className="page">
        {/* --- accounts --- */}
        <div className="acct-grid">
          {(accounts.data?.accounts ?? []).map((a) => (
            <AccountCard key={`${a.broker}-${a.id}`} a={a} />
          ))}
          {!accounts.data && <div className="skeleton" style={{ height: 130 }} />}
        </div>

        {/* --- aggregate risk --- */}
        {d && d.positions > 0 && d.greeks && (
          <div className="card">
            <div className="card-head">
              <div>
                <h2>Book risk</h2>
                <span className="sub">
                  aggregate greeks across {d.positions} position{d.positions === 1 ? '' : 's'} ·
                  beta-weighted Δ {d.beta_weighted_delta} vs {d.beta_benchmark}
                </span>
              </div>
            </div>
            <div className="tiles">
              {(['delta', 'gamma', 'theta', 'vega'] as const).map((g) => {
                const v = d.greeks![g]
                return (
                  <div className="tile" key={g}>
                    <div className="k">Net {g}</div>
                    <div className={`v ${v >= 0 ? 'pos' : 'neg'}`}>
                      {g === 'gamma' ? v.toFixed(3) : v.toFixed(1)}
                    </div>
                    <div className="n">
                      {g === 'theta' ? 'dollars per day'
                        : g === 'vega' ? 'per IV point' : 'share equivalent'}
                    </div>
                  </div>
                )
              })}
            </div>
            {d.beta_note && <div className="card-body tiny muted">{d.beta_note}</div>}
            {(d.warnings ?? []).map((w) => (
              <div className="insight" key={w}>
                <span className="icon" style={{ color: 'var(--status-serious)' }} aria-hidden>!</span>
                <div className="d">{w}</div>
              </div>
            ))}
          </div>
        )}

        {/* --- positions --- */}
        <div className="card">
          <div className="card-head">
            <div>
              <h2>Positions</h2>
              <span className="sub">
                paper book, marked against live quotes — click a row for marks and actions
              </span>
            </div>
          </div>
          {rows.length > 0 ? (
            <div className="card-body flush">
              <table className="table">
                <thead>
                  <tr>
                    <th>Symbol</th><th style={{ textAlign: 'left' }}>Position</th>
                    <th>Spot</th><th>Unrealized</th><th>Max loss</th>
                    <th>PoP</th><th>DTE</th><th>Δ</th><th>Θ</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <PositionRow key={r.id} r={r} onChanged={bookChanged} />
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="empty-state" style={{ padding: '28px 16px' }}>
              <strong>No open positions.</strong>
              <p>
                Positions arrive here from the <Link href="/lab">Strategy Lab</Link> (open a
                built strategy as a paper trade), from the agent, or from{' '}
                <code>POST /api/portfolio/positions</code>. Once one exists, this page marks
                it against live quotes, aggregates the greeks, and raises the risk warnings.
              </p>
            </div>
          )}
          {d && !d.persistent && rows.length > 0 && (
            <div className="card-body tiny muted">
              Positions are held in memory — they vanish when the API restarts. Set{' '}
              <code>DATABASE_URL</code> to persist them.
            </div>
          )}
        </div>
      </div>
    </Shell>
  )
}
