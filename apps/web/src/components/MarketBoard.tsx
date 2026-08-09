'use client'

import { useState } from 'react'
import { Sparkline } from '@/components/charts'
import { num, pct } from '@/lib/format'
import type { NewsFeedResponse, RankedNewsItem, SegmentsResponse } from '@/lib/types'

/**
 * The market board and the impact-ranked news feed.
 *
 * Two encoding decisions worth stating:
 *
 * - Rows that quote an ETF in place of a futures contract carry a visible
 *   marker, not a footnote. SPY and ES track closely during the cash session
 *   and diverge overnight, which is exactly when someone opens a futures
 *   board — so the substitution has to be legible at the row, at a glance.
 *
 * - The impact meter is never the only channel. The band name and the numeric
 *   score sit next to the bar, because a colour-length pair alone would make
 *   the ranking unreadable to anyone who cannot separate the band colours, and
 *   because a bar with no number invites the reader to trust a measurement they
 *   cannot check.
 */

const BAND_COLOR: Record<string, string> = {
  high: 'var(--status-critical)',
  elevated: 'var(--status-serious)',
  moderate: 'var(--status-warning)',
  low: 'var(--text-muted)',
  unscored: 'var(--axis)',
}

export function SegmentBoard({ data, badge }: { data: SegmentsResponse; badge?: React.ReactNode }) {
  return (
    <>
      <div className="card">
        <div className="card-head">
          <div>
            <h2>Market board</h2>
            <span className="sub">
              {data.breadth.advancing} of {data.breadth.instruments} advancing ·
              benchmark {data.benchmark}
            </span>
          </div>
          <div className="row" style={{ gap: 8 }}>
            <span className="chip">
              {data.futures_available ? 'futures' : 'ETF proxies for futures'}
            </span>
            {badge}
          </div>
        </div>

        <div className="card-body flush">
          {data.segments.map((seg) => (
            <div key={seg.name}>
              <div className="seg-head">{seg.name}</div>
              <table className="table">
                <thead>
                  <tr>
                    <th>Instrument</th>
                    <th>Last</th>
                    <th>Day</th>
                    <th>1W</th>
                    <th>1M</th>
                    <th>3M</th>
                    <th>vs {data.benchmark}</th>
                    <th>RV 20d</th>
                    <th>From 52w high</th>
                    <th style={{ width: 118 }}>60 sessions</th>
                  </tr>
                </thead>
                <tbody>
                  {seg.rows.map((r) => (
                    <tr key={r.symbol}>
                      <td>
                        <span className="inst">
                          <span className="inst-label">{r.label}</span>
                          <span className="inst-sym">
                            {r.symbol}
                            {r.is_proxy && (
                              <span
                                className="proxy-tag"
                                title={`No futures feed is configured, so this row quotes ${r.symbol} in place of ${r.proxy_for}. They track closely during the cash session and diverge overnight.`}
                              >
                                ≈ {r.proxy_for}
                              </span>
                            )}
                          </span>
                        </span>
                      </td>
                      <td>{r.last != null ? num(r.last, 2) : '—'}</td>
                      <Move v={r.change_1d} />
                      <Move v={r.change_1w} />
                      <Move v={r.change_1m} />
                      <Move v={r.change_3m} />
                      <Move v={r.rs_1m_vs_benchmark} />
                      <td>{r.realized_vol_20d != null ? pct(r.realized_vol_20d, 0) : '—'}</td>
                      <td className={(r.pct_from_52w_high ?? 0) < -0.1 ? 'neg' : ''}>
                        {r.pct_from_52w_high != null ? pct(r.pct_from_52w_high, 1) : '—'}
                      </td>
                      <td style={{ textAlign: 'right' }}>
                        <Sparkline values={r.sparkline} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </div>

        <div className="card-body tiny muted">{data.provenance}</div>
      </div>
    </>
  )
}

function Move({ v }: { v: number | null }) {
  if (v == null) return <td className="muted">—</td>
  return <td className={v >= 0 ? 'pos' : 'neg'}>{pct(v, 2)}</td>
}

export function ImpactFeed({
  data, badge, symbols, onSymbols,
}: {
  data: NewsFeedResponse
  badge?: React.ReactNode
  symbols?: string
  onSymbols?: (v: string) => void
}) {
  const scored = data.items.filter((i) => i.scoring.impact != null).length
  const [draft, setDraft] = useState(symbols ?? '')
  const [only, setOnly] = useState<string | null>(null)
  const shown = only ? data.items.filter((i) => i.symbol === only) : data.items
  return (
    <div className="card">
      <div className="card-head">
        <div>
          <h2>Watchlist news by impact</h2>
          <span className="sub">
            {scored} of {data.count} scored · {data.symbols.join(' · ')}
          </span>
        </div>
        <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
          {onSymbols && (
            <input
              className="input sym-field"
              style={{ width: 210 }}
              value={draft}
              aria-label="News symbols"
              onChange={(e) => setDraft(e.target.value.toUpperCase())}
              onKeyDown={(e) => e.key === 'Enter' && onSymbols(draft)}
              onBlur={() => draft !== symbols && onSymbols(draft)}
            />
          )}
          {/* Filter to one ticker without refetching — the feed is already
              loaded, and a round trip to hide rows is a round trip wasted. */}
          {data.symbols.map((sy) => (
            <button
              key={sy}
              className="chip filter-chip"
              aria-pressed={only === sy}
              onClick={() => setOnly(only === sy ? null : sy)}
            >
              {sy}
            </button>
          ))}
          {badge}
          {(['high', 'elevated', 'moderate', 'low'] as const).map((b) =>
            data.bands[b] ? (
              <span className="chip" key={b}>
                <span className="swatch" style={{ background: BAND_COLOR[b] }} />
                {data.bands[b]} {b}
              </span>
            ) : null,
          )}
        </div>
      </div>

      <div className="card-body flush">
        <div className="scroll" style={{ maxHeight: 520 }}>
          {shown.map((item) => (
            <NewsRow key={item.id + item.symbol} item={item} />
          ))}
          {!shown.length && <div className="symsearch-empty">No items for {only}.</div>}
        </div>
      </div>

      <div className="card-body tiny muted">
        <p style={{ margin: '0 0 6px' }}>{data.method}</p>
        <p style={{ margin: 0 }}>{data.limits}</p>
      </div>
    </div>
  )
}

function NewsRow({ item }: { item: RankedNewsItem }) {
  const s = item.scoring
  const c = s.components
  const width = s.impact != null ? `${s.impact}%` : '0%'

  return (
    <div className="news-row">
      <div className="news-impact">
        <div className="news-meter" aria-hidden>
          <div style={{ width, background: BAND_COLOR[s.band] }} />
        </div>
        <div className="news-score">
          {s.impact != null ? (
            <>
              <strong>{s.impact.toFixed(0)}</strong>
              <span className="band">{s.band}</span>
            </>
          ) : (
            <span className="band">unscored</span>
          )}
        </div>
      </div>

      <div className="news-body">
        <div className="news-title">
          <span className="chip">{item.symbol}</span>
          {item.media_type === 'video' && <span className="chip">video</span>}
          <a href={item.url} target="_blank" rel="noreferrer">
            {item.title}
          </a>
        </div>
        <div className="news-meta">
          {s.impact != null ? (
            <>
              <span className={s.direction === 'down' ? 'neg' : 'pos'}>
                {c.realized_move_pct != null ? `${c.realized_move_pct > 0 ? '+' : ''}${c.realized_move_pct.toFixed(2)}%` : '—'}
              </span>
              <span>{c.move_sigma?.toFixed(1)}σ move</span>
              {c.volume_vs_median != null && <span>{c.volume_vs_median.toFixed(1)}× volume</span>}
              {(c.coverage_items_24h ?? 0) > 1 && <span>{c.coverage_items_24h} items</span>}
              {c.days_to_earnings != null && Math.abs(c.days_to_earnings) <= 10 && (
                <span className="warn">earnings in {c.days_to_earnings}d</span>
              )}
              <span className="muted">
                measured {s.window?.prior_close} → {s.window?.reaction_close}
              </span>
            </>
          ) : (
            <span className="muted">{s.reason}</span>
          )}
        </div>
      </div>
    </div>
  )
}
