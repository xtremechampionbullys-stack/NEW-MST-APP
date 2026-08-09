'use client'

import { useMemo } from 'react'
import { money, pct } from '@/lib/format'
import type { EarningsReport } from '@/lib/types'

/**
 * Earnings: the date, what the chain charges for it, and what the stock has
 * actually done.
 *
 * The two implied-move numbers are shown side by side rather than reduced to
 * one, because they answer different questions. The straddle covers the whole
 * life of the front option -- event plus every ordinary day. The term-structure
 * figure isolates the event itself. Collapsing them to a single "expected move"
 * is the most common way this gets presented, and it silently overstates the
 * event whenever the front expiry is more than a few days out.
 *
 * The history chart is a horizontal bar per past report, colored by direction
 * (polarity, so status colors), with the implied move drawn as a reference
 * line. That comparison is the whole decision.
 */

interface Props {
  report: EarningsReport
}

export function EarningsPanel({ report }: Props) {
  const next = report.next_event
  const move = report.implied_move
  const straddle = move?.straddle
  const term = move?.term_structure
  const history = report.history

  const preferred = move?.preferred === 'term-structure' ? term : straddle
  const impliedPct = preferred?.expected_move_pct ?? null

  const bars = useMemo(() => {
    const moves = history?.moves ?? []
    const max = Math.max(
      ...moves.map((m) => m.abs_move_pct),
      impliedPct ?? 0,
      0.01,
    )
    return { moves: moves.slice(0, 8), max }
  }, [history, impliedPct])

  if (!next) {
    return (
      <div className="card-body muted tiny">
        No upcoming earnings date found for {report.symbol}.
      </div>
    )
  }

  return (
    <div>
      {report.synthetic && (
        <div className="banner tiny">
          <span aria-hidden>▲</span>
          <span>Synthetic calendar — connect Benzinga, Finnhub, or Nasdaq for real dates.</span>
        </div>
      )}

      <div className="tiles">
        <div className="tile">
          <div className="k">Next report</div>
          <div className="v" style={{ fontSize: 16 }}>{next.event_date}</div>
          <div className="n">
            {next.days_away === 0
              ? 'today'
              : next.days_away === 1
                ? 'tomorrow'
                : `in ${next.days_away} days`}
            {' · '}
            {next.session === 'bmo' ? 'before open'
              : next.session === 'amc' ? 'after close'
              : next.session === 'dmh' ? 'during hours' : 'time unknown'}
          </div>
        </div>

        <div className="tile">
          <div className="k">Date status</div>
          <div className="v" style={{ fontSize: 16, color: next.is_confirmed ? undefined : 'var(--status-warning)' }}>
            {next.is_confirmed ? 'Confirmed' : 'Projected'}
          </div>
          <div className="n">
            {next.is_confirmed ? 'company confirmed' : 'can move by a week'}
          </div>
        </div>

        <div className="tile">
          <div className="k">Implied move (event)</div>
          <div className="v">{term?.expected_move_pct != null ? pct(term.expected_move_pct, 1) : '—'}</div>
          <div className="n">
            {term?.expected_move_pct != null
              ? `${money(term.expected_move_abs, 2)} · isolated from term structure`
              : 'term structure shows no event premium'}
          </div>
        </div>

        <div className="tile">
          <div className="k">Straddle move</div>
          <div className="v">{straddle?.expected_move_pct != null ? pct(straddle.expected_move_pct, 1) : '—'}</div>
          <div className="n">
            whole life of the {straddle?.front_dte ?? '—'}-day option
          </div>
        </div>
      </div>

      {term?.expected_move_pct != null && straddle?.expected_move_pct != null && (
        <div className="card-body tiny secondary" style={{ borderBottom: '1px solid var(--border)' }}>
          The {straddle.front_dte}-day straddle prices a{' '}
          <strong>{pct(straddle.expected_move_pct, 1)}</strong> move, but only{' '}
          <strong>{pct(term.expected_move_pct, 1)}</strong> of that is the announcement —
          the rest is ordinary day-to-day volatility over the remaining{' '}
          {straddle.front_dte} days. Ex-event volatility is running{' '}
          {pct(term.diffusive_vol, 1)} annualized.
        </div>
      )}

      {term?.expected_move_pct == null && term?.note && (
        <div className="card-body tiny secondary" style={{ borderBottom: '1px solid var(--border)' }}>
          {term.note}
        </div>
      )}

      {history && history.count > 0 && (
        <div style={{ padding: '12px 14px' }}>
          <div className="spread" style={{ marginBottom: 8 }}>
            <span className="tiny muted">
              Realized move on the last {history.count} reports
            </span>
            <span className="tiny muted">
              avg {pct(history.average_abs_move, 1)} · median {pct(history.median_abs_move, 1)}
            </span>
          </div>

          <div style={{ position: 'relative' }}>
            {impliedPct != null && (
              <div
                aria-hidden
                style={{
                  position: 'absolute',
                  left: `calc(82px + (100% - 82px - 62px) * ${impliedPct / bars.max})`,
                  top: 0,
                  bottom: 0,
                  width: 2,
                  background: 'var(--series-1)',
                  zIndex: 1,
                }}
              />
            )}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            {bars.moves.map((m) => {
              const w = (m.abs_move_pct / bars.max) * 100
              const up = m.move_pct >= 0
              return (
                <div key={m.event_date} className="row" style={{ gap: 8 }}>
                  <span className="tiny muted num" style={{ width: 74, flexShrink: 0 }}>
                    {m.event_date}
                  </span>
                  <div style={{ flex: 1, height: 12 }}>
                    <div
                      style={{
                        width: `${w}%`,
                        height: '100%',
                        background: up ? 'var(--status-good)' : 'var(--status-critical)',
                        opacity: 0.75,
                        borderRadius: 2,
                      }}
                      title={`${m.event_date}: ${(m.move_pct * 100).toFixed(2)}%`}
                    />
                  </div>
                  <span
                    className={`tiny num ${up ? 'pos' : 'neg'}`}
                    style={{ width: 54, textAlign: 'right', flexShrink: 0 }}
                  >
                    {up ? '+' : ''}{(m.move_pct * 100).toFixed(1)}%
                  </span>
                </div>
              )
            })}
            </div>
          </div>

          <div className="legend" style={{ padding: '10px 0 0' }}>
            <span className="item">
              <span className="line" style={{ background: 'var(--status-good)', opacity: 0.75 }} />
              up
            </span>
            <span className="item">
              <span className="line" style={{ background: 'var(--status-critical)', opacity: 0.75 }} />
              down
            </span>
            {impliedPct != null && (
              <span className="item">
                <span className="line" style={{ background: 'var(--series-1)', width: 2, height: 12 }} />
                implied move now ({pct(impliedPct, 1)})
              </span>
            )}
          </div>
        </div>
      )}

      {report.verdict && (
        <div className="insight" style={{ borderTop: '1px solid var(--border)' }}>
          <span
            className="icon"
            style={{
              color: report.verdict.ratio > 1.15 ? 'var(--status-good)'
                : report.verdict.ratio < 0.85 ? 'var(--status-warning)'
                : 'var(--text-muted)',
            }}
            aria-hidden
          >
            {report.verdict.ratio > 1.15 ? '↑' : report.verdict.ratio < 0.85 ? '↓' : '='}
          </span>
          <div>
            <div className="t">
              Implied is {report.verdict.ratio.toFixed(2)}× the historical average
            </div>
            <div className="d">{report.verdict.reading}</div>
          </div>
        </div>
      )}
    </div>
  )
}
