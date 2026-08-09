'use client'

import { useMemo, useRef, useState } from 'react'
import { money, nearestIndex, num, pct, ticks } from '@/lib/format'
import type { CalibrationResponse, DirectionResponse, VolProjection } from '@/lib/types'

/**
 * Charts for the Research page.
 *
 * Encoding notes, in the same spirit as charts.tsx:
 *
 * - `FanChart` uses the sequential blue ramp for the percentile bands, not the
 *   categorical slots. The bands are ordered quantities (5 < 25 < 50 < 75 < 95)
 *   and an ordered ramp says so; categorical colours would imply five unrelated
 *   series. The median is drawn as a line on top because it is the one band
 *   people read as a value rather than a boundary.
 * - `VolTermChart` separates projected total volatility from its diffusive
 *   component as two lines rather than a stack. The gap between them IS the
 *   event contribution, and a gap is easier to read than a stack segment when
 *   what you care about is "how much of this is earnings".
 * - `SkillBar` and `CalibrationChart` render measured skill against the
 *   baseline that makes it meaningful. Neither will draw without its baseline.
 */

const W = 900
const PAD = { top: 16, right: 20, bottom: 28, left: 60 }

/* ------------------------------------------------------------- fan chart */

export function FanChart({
  days,
  bands,
  spot,
  height = 260,
}: {
  days: number[]
  bands: Record<string, number[]>
  spot?: number
  height?: number
}) {
  const svgRef = useRef<SVGSVGElement>(null)
  const [hover, setHover] = useState<number | null>(null)

  const geom = useMemo(() => {
    const all = Object.values(bands).flat()
    if (!days.length || !all.length) return null
    const lo = Math.min(...all)
    const hi = Math.max(...all)
    const pad = (hi - lo) * 0.06 || 1
    const yLo = lo - pad
    const yHi = hi + pad
    const iw = W - PAD.left - PAD.right
    const ih = height - PAD.top - PAD.bottom
    const x = (d: number) => PAD.left + ((d - days[0]) / (days[days.length - 1] - days[0] || 1)) * iw
    const y = (v: number) => PAD.top + ih - ((v - yLo) / (yHi - yLo || 1)) * ih
    return { x, y, yLo, yHi, iw, ih }
  }, [days, bands, height])

  if (!geom) return <div className="skeleton" style={{ height }} />

  const area = (lower: number[], upper: number[]) => {
    const up = upper.map((v, i) => `${i === 0 ? 'M' : 'L'}${geom.x(days[i]).toFixed(1)},${geom.y(v).toFixed(1)}`)
    const down = lower
      .map((v, i) => `L${geom.x(days[i]).toFixed(1)},${geom.y(v).toFixed(1)}`)
      .reverse()
    return `${up.join(' ')} ${down.join(' ')} Z`
  }

  const line = (vals: number[]) =>
    vals.map((v, i) => `${i === 0 ? 'M' : 'L'}${geom.x(days[i]).toFixed(1)},${geom.y(v).toFixed(1)}`).join(' ')

  const yTicks = ticks(geom.yLo, geom.yHi, 5)

  return (
    <div className="chart-wrap">
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${height}`}
        role="img"
        aria-label="Simulated price paths shown as percentile bands over the projection horizon"
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const rect = svgRef.current?.getBoundingClientRect()
          if (!rect) return
          const px = ((e.clientX - rect.left) / rect.width) * W
          const frac = (px - PAD.left) / geom.iw
          const day = days[0] + frac * (days[days.length - 1] - days[0])
          setHover(nearestIndex(days, day))
        }}
      >
        {yTicks.map((t) => (
          <g key={t}>
            <line className="grid-line" x1={PAD.left} x2={W - PAD.right} y1={geom.y(t)} y2={geom.y(t)} />
            <text className="axis-label" x={PAD.left - 8} y={geom.y(t) + 3} textAnchor="end">
              {t.toFixed(0)}
            </text>
          </g>
        ))}

        {bands['5'] && bands['95'] && (
          <path d={area(bands['5'], bands['95'])} fill="var(--seq-600)" opacity={0.30} />
        )}
        {bands['25'] && bands['75'] && (
          <path d={area(bands['25'], bands['75'])} fill="var(--seq-450)" opacity={0.45} />
        )}
        {bands['50'] && (
          <path d={line(bands['50'])} fill="none" stroke="var(--seq-250)" strokeWidth={2} />
        )}

        {spot != null && (
          <line
            x1={PAD.left}
            x2={W - PAD.right}
            y1={geom.y(spot)}
            y2={geom.y(spot)}
            stroke="var(--axis)"
            strokeWidth={1}
            strokeDasharray="4 4"
          />
        )}

        <line className="axis-line" x1={PAD.left} x2={W - PAD.right} y1={height - PAD.bottom} y2={height - PAD.bottom} />
        {days
          .filter((_, i) => i % Math.max(1, Math.floor(days.length / 6)) === 0)
          .map((d) => (
            <text key={d} className="axis-label" x={geom.x(d)} y={height - PAD.bottom + 16} textAnchor="middle">
              {d}d
            </text>
          ))}

        {hover != null && (
          <line
            x1={geom.x(days[hover])}
            x2={geom.x(days[hover])}
            y1={PAD.top}
            y2={height - PAD.bottom}
            stroke="var(--border-strong)"
            strokeWidth={1}
          />
        )}
      </svg>

      {hover != null && (
        <div className="tooltip" style={{ left: `${(geom.x(days[hover]) / W) * 100}%`, top: 8 }}>
          <div className="tt-head">Day {days[hover]}</div>
          {['95', '75', '50', '25', '5'].map(
            (b) =>
              bands[b] && (
                <div className="tt-row" key={b}>
                  <span>{b}th</span>
                  <span>{bands[b][hover].toFixed(2)}</span>
                </div>
              ),
          )}
        </div>
      )}

      <div className="legend">
        <span className="item">
          <span className="line" style={{ background: 'var(--seq-250)' }} /> median
        </span>
        <span className="item">
          <span className="swatch" style={{ background: 'var(--seq-450)', opacity: 0.75 }} /> 25–75th
        </span>
        <span className="item">
          <span className="swatch" style={{ background: 'var(--seq-600)', opacity: 0.6 }} /> 5–95th
        </span>
        {spot != null && <span className="item muted">dashed line: spot at entry</span>}
      </div>
    </div>
  )
}

/* ------------------------------------------------------- vol projections */

export function VolTermChart({
  projections,
  impliedNow,
  height = 230,
}: {
  projections: VolProjection[]
  impliedNow?: number | null
  height?: number
}) {
  if (projections.length < 2) return <div className="skeleton" style={{ height }} />

  const xs = projections.map((p) => p.days)
  const all = [
    ...projections.map((p) => p.projected_vol),
    ...projections.map((p) => p.diffusive_vol),
    ...(impliedNow != null ? [impliedNow] : []),
  ]
  const lo = Math.min(...all) * 0.9
  const hi = Math.max(...all) * 1.08
  const iw = W - PAD.left - PAD.right
  const ih = height - PAD.top - PAD.bottom
  const x = (d: number) => PAD.left + ((d - xs[0]) / (xs[xs.length - 1] - xs[0] || 1)) * iw
  const y = (v: number) => PAD.top + ih - ((v - lo) / (hi - lo || 1)) * ih

  const path = (key: 'projected_vol' | 'diffusive_vol') =>
    projections
      .map((p, i) => `${i === 0 ? 'M' : 'L'}${x(p.days).toFixed(1)},${y(p[key]).toFixed(1)}`)
      .join(' ')

  const gap = projections
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${x(p.days).toFixed(1)},${y(p.projected_vol).toFixed(1)}`)
    .concat(
      projections
        .map((p) => `L${x(p.days).toFixed(1)},${y(p.diffusive_vol).toFixed(1)}`)
        .reverse(),
    )
    .join(' ')

  return (
    <div className="chart-wrap">
      <svg viewBox={`0 0 ${W} ${height}`} role="img"
        aria-label="Projected volatility by horizon, split into the diffusive component and the earnings-event contribution">
        {ticks(lo, hi, 5).map((t) => (
          <g key={t}>
            <line className="grid-line" x1={PAD.left} x2={W - PAD.right} y1={y(t)} y2={y(t)} />
            <text className="axis-label" x={PAD.left - 8} y={y(t) + 3} textAnchor="end">
              {(t * 100).toFixed(0)}%
            </text>
          </g>
        ))}

        <path d={gap} fill="var(--series-2)" opacity={0.16} />

        {impliedNow != null && (
          <>
            <line
              x1={PAD.left}
              x2={W - PAD.right}
              y1={y(impliedNow)}
              y2={y(impliedNow)}
              stroke="var(--series-4)"
              strokeWidth={1.5}
              strokeDasharray="5 4"
            />
            <text className="axis-label" x={W - PAD.right - 2} y={y(impliedNow) - 5} textAnchor="end"
              fill="var(--series-4)">
              implied now {(impliedNow * 100).toFixed(0)}%
            </text>
          </>
        )}

        <path d={path('projected_vol')} fill="none" stroke="var(--series-1)" strokeWidth={2} />
        <path d={path('diffusive_vol')} fill="none" stroke="var(--series-3)" strokeWidth={2}
          strokeDasharray="6 3" />

        {projections.map((p) => (
          <g key={p.days}>
            <circle cx={x(p.days)} cy={y(p.projected_vol)} r={3} fill="var(--series-1)" />
            {p.events_in_window > 0 && (
              <text className="axis-label" x={x(p.days)} y={y(p.projected_vol) - 9} textAnchor="middle">
                {p.events_in_window}E
              </text>
            )}
          </g>
        ))}

        <line className="axis-line" x1={PAD.left} x2={W - PAD.right} y1={height - PAD.bottom} y2={height - PAD.bottom} />
        {projections.map((p) => (
          <text key={p.days} className="axis-label" x={x(p.days)} y={height - PAD.bottom + 16} textAnchor="middle">
            {p.days}d
          </text>
        ))}
      </svg>

      <div className="legend">
        <span className="item">
          <span className="line" style={{ background: 'var(--series-1)' }} /> projected total
        </span>
        <span className="item">
          <span className="line" style={{ background: 'var(--series-3)' }} /> diffusive only
        </span>
        <span className="item muted">shaded gap: earnings contribution · “2E” = two announcements in the window</span>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------ skill bars */

export function SkillReport({ d }: { d: DirectionResponse }) {
  const os = d.out_of_sample
  const lo = Math.min(0.35, os.accuracy - 0.05, os.base_rate - 0.05)
  const hi = Math.max(0.65, os.accuracy + 0.05, os.base_rate + 0.05)
  const place = (v: number) => `${((v - lo) / (hi - lo)) * 100}%`

  return (
    <div className="card-body">
      <div className="skill">
        <div className="skill-track">
          <div className="skill-fill" style={{ width: place(os.accuracy) }} />
          <div className="skill-mark" style={{ left: place(os.base_rate) }} title="base rate" />
          <div
            className="skill-band"
            style={{
              left: place(os.base_rate - 2 * os.standard_error),
              width: `${((4 * os.standard_error) / (hi - lo)) * 100}%`,
            }}
          />
        </div>
        <div className="skill-legend">
          <span>
            <span className="swatch" style={{ background: 'var(--series-1)' }} />
            out-of-sample accuracy <strong>{pct(os.accuracy, 1)}</strong>
          </span>
          <span>
            <span className="swatch" style={{ background: 'var(--text-secondary)' }} />
            base rate <strong>{pct(os.base_rate, 1)}</strong>
          </span>
          <span className="muted">shaded: ±2 standard errors around the base rate</span>
        </div>
      </div>

      <p className={`verdict ${d.trustworthy ? 'ok' : 'null'}`}>{d.verdict}</p>

      <div className="tiles" style={{ marginTop: 12 }}>
        <div className="tile">
          <div className="k">Edge</div>
          <div className={`v ${os.edge > 0 ? 'pos' : 'neg'}`}>{pct(os.edge, 1)}</div>
          <div className="n">accuracy − base rate</div>
        </div>
        <div className="tile">
          <div className="k">AUC</div>
          <div className="v">{num(os.auc, 3)}</div>
          <div className="n">0.5 = no ranking ability</div>
        </div>
        <div className="tile">
          <div className="k">Brier</div>
          <div className="v">{num(os.brier, 4)}</div>
          <div className="n">vs {num(os.brier_base_rate, 4)} for the base rate</div>
        </div>
        <div className="tile">
          <div className="k">Samples</div>
          <div className="v">{os.samples}</div>
          <div className="n">out-of-sample predictions</div>
        </div>
      </div>
    </div>
  )
}

/* ---------------------------------------------------------- calibration */

export function CalibrationChart({ report }: { report: CalibrationResponse['calibration'] }) {
  const buckets = report.buckets ?? []
  if (!buckets.length) return null

  const h = 200
  const iw = W - PAD.left - PAD.right
  const ih = h - PAD.top - PAD.bottom
  const x = (v: number) => PAD.left + v * iw
  const y = (v: number) => PAD.top + ih - v * ih

  return (
    <div className="chart-wrap">
      <svg viewBox={`0 0 ${W} ${h}`} role="img"
        aria-label="Predicted win rate against realized win rate; the diagonal is perfect calibration">
        {[0, 0.25, 0.5, 0.75, 1].map((t) => (
          <g key={t}>
            <line className="grid-line" x1={PAD.left} x2={W - PAD.right} y1={y(t)} y2={y(t)} />
            <text className="axis-label" x={PAD.left - 8} y={y(t) + 3} textAnchor="end">
              {(t * 100).toFixed(0)}%
            </text>
            <text className="axis-label" x={x(t)} y={h - PAD.bottom + 16} textAnchor="middle">
              {(t * 100).toFixed(0)}%
            </text>
          </g>
        ))}
        <line x1={x(0)} y1={y(0)} x2={x(1)} y2={y(1)} stroke="var(--axis)" strokeDasharray="4 4" />
        {buckets.map((b) => (
          <g key={b.bucket}>
            <line
              x1={x(b.predicted_win_rate)}
              y1={y(b.predicted_win_rate)}
              x2={x(b.predicted_win_rate)}
              y2={y(b.realized_win_rate)}
              stroke={Math.abs(b.gap) > 0.1 ? 'var(--status-critical)' : 'var(--series-3)'}
              strokeWidth={2}
            />
            <circle
              cx={x(b.predicted_win_rate)}
              cy={y(b.realized_win_rate)}
              r={Math.max(4, Math.min(11, Math.sqrt(b.trades) * 1.2))}
              fill="var(--series-1)"
              opacity={0.85}
            />
          </g>
        ))}
      </svg>
      <div className="legend">
        <span className="item muted">
          x: predicted · y: realized · circle area ∝ trades · dashed diagonal = perfectly calibrated
        </span>
      </div>
    </div>
  )
}

/* ------------------------------------------------------- outcome ribbon */

export function OutcomeRibbon({
  target,
  stop,
  timed,
}: {
  target: number
  stop: number
  timed: number
}) {
  const segs = [
    { k: 'Profit target', v: target, c: 'var(--status-good)' },
    { k: 'Stop loss', v: stop, c: 'var(--status-critical)' },
    { k: 'Held to exit', v: timed, c: 'var(--seq-450)' },
  ]
  return (
    <div className="card-body">
      <div className="ribbon" role="img" aria-label={segs.map((s) => `${s.k} ${pct(s.v, 1)}`).join(', ')}>
        {segs.map((s) => (
          <div key={s.k} style={{ width: `${s.v * 100}%`, background: s.c }} title={`${s.k} ${pct(s.v, 1)}`} />
        ))}
      </div>
      <div className="legend" style={{ padding: '10px 0 0' }}>
        {segs.map((s) => (
          <span className="item" key={s.k}>
            <span className="swatch" style={{ background: s.c }} />
            {s.k} <strong style={{ marginLeft: 4 }}>{pct(s.v, 1)}</strong>
          </span>
        ))}
      </div>
    </div>
  )
}

export function moneyRange(lo: number, hi: number) {
  return `${money(lo, 0)} … ${money(hi, 0)}`
}
