'use client'

import { Sparkline } from '@/components/charts'
import { LiveBadge, useLive } from '@/components/live'
import { api } from '@/lib/api'
import { num, pct } from '@/lib/format'
import type { SegmentsResponse } from '@/lib/types'

/**
 * Live futures and index strip for the landing page.
 *
 * The rows are labelled with the contract a trader reads — ES, NQ, RTY, YM, CL,
 * GC, VX — and each one carries the `≈` marker showing which ETF is actually
 * being quoted, because no configured provider serves futures. A strip that
 * printed "ES" over an SPY quote with no marker would be most wrong overnight,
 * which is exactly when someone glances at a futures strip.
 */

const WANTED = [
  'SPY', 'QQQ', 'IWM', 'DIA', 'TLT', 'USO', 'GLD', 'VXX',
  'XLK', 'XLF', 'XLE', 'XLV', 'XLI', 'XLY', 'SMH', 'XLU',
]

export function MarketStrip() {
  const board = useLive<SegmentsResponse>(() => api.segments(), [], 20000)
  const rows = (board.data?.segments ?? [])
    .flatMap((s) => s.rows)
    .filter((r) => WANTED.includes(r.symbol) && !r.error)
    .sort((a, b) => WANTED.indexOf(a.symbol) - WANTED.indexOf(b.symbol))
  const embeddedError = (board.data?.segments ?? [])
    .flatMap((s) => s.rows)
    .find((r) => r.error)?.error
  const unavailableReason = board.error ?? embeddedError

  return (
    <div className="market-strip">
      <div className="strip-head">
        <span className="type-caption">Futures &amp; indices</span>
        <LiveBadge state={board} />
      </div>

      <div className="strip-rows">
        {rows.length === 0 && unavailableReason ? (
          <div className="strip-unavailable" role="status">
            <strong>Live feed needs a market-data provider</strong>
            <span>{unavailableReason}</span>
          </div>
        ) : rows.length === 0 ? (
          WANTED.map((s) => <div className="strip-cell skeleton" key={s} style={{ height: 52 }} />)
        ) : null}

        {rows.map((r) => {
          const up = (r.change_1d ?? 0) >= 0
          return (
            <div className="strip-cell" key={r.symbol}>
              <div className="strip-top">
                <span className="strip-name">
                  {r.proxy_for ?? r.symbol}
                  {r.is_proxy && (
                    <span
                      className="proxy-tag"
                      title={`No futures feed is configured, so this quotes ${r.symbol} in place of ${r.proxy_for}. They track closely during the cash session and diverge overnight.`}
                    >
                      ≈ {r.symbol}
                    </span>
                  )}
                </span>
                <Sparkline values={r.sparkline?.slice(-30) ?? []} width={54} height={16} positive={up} />
              </div>
              <div className="strip-bottom">
                <span className="strip-last">{r.last != null ? num(r.last, 2) : '—'}</span>
                <span className={up ? 'pos' : 'neg'}>
                  {r.change_1d != null ? pct(r.change_1d, 2) : '—'}
                </span>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
