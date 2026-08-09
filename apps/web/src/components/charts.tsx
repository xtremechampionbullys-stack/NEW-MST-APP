'use client'

import { useMemo, useRef, useState } from 'react'
import { money, ticks } from '@/lib/format'

/**
 * Shared chart primitives for the dashboard and backtest pages.
 *
 * Encoding notes:
 *
 * - `EquityCurve` and `DrawdownChart` are two stacked plots sharing one x axis,
 *   never one plot with two y scales. Equity in dollars and drawdown in dollars
 *   below peak are different quantities; overlaying them on a dual axis would
 *   invent a relationship whose apparent tightness is an artefact of how the
 *   two scales happen to be aligned.
 * - `Histogram` colours by sign, which is polarity, so it uses the reserved
 *   status tokens rather than categorical slots. The values are printed in the
 *   tooltip and the axis, so colour is never the only channel.
 * - `Sparkline` deliberately has no axes. It is a shape cue inside a table row;
 *   the exact numbers live in the adjacent columns.
 */

/* ------------------------------------------------------------- sparkline */

export function Sparkline({
  values,
  width = 104,
  height = 26,
  positive,
}: {
  values: number[]
  width?: number
  height?: number
  positive?: boolean
}) {
  if (!values || values.length < 2) {
    return <div style={{ width, height }} className="skeleton" />
  }
  const lo = Math.min(...values)
  const hi = Math.max(...values)
  const span = hi - lo || 1
  const step = width / (values.length - 1)
  const up = positive ?? values[values.length - 1] >= values[0]

  const d = values
    .map((v, i) => `${i === 0 ? 'M' : 'L'}${(i * step).toFixed(1)},${(height - ((v - lo) / span) * height).toFixed(1)}`)
    .join(' ')

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden>
      <path
        d={d}
        fill="none"
        stroke={up ? 'var(--status-good)' : 'var(--status-critical)'}
        strokeWidth={1.5}
        strokeLinejoin="round"
      />
    </svg>
  )
}

/* ---------------------------------------------------------- equity curve */

interface EquityPoint {
  date: string
  equity: number
  realized: number
  unrealized: number
  spot: number
  open_positions: number
}

const W = 900
const PAD = { top: 14, right: 62, bottom: 26, left: 66 }

export function EquityCurve({ points, height = 240 }: { points: EquityPoint[]; height?: number }) {
  const svgRef = useRef<SVGSVGElement>(null)
  const [hover, setHover] = useState<number | null>(null)

  const geom = useMemo(() => {
    if (points.length < 2) return null
    const values = points.map((p) => p.equity)
    let lo = Math.min(...values, 0)
    let hi = Math.max(...values, 0)
    const pad = Math.max((hi - lo) * 0.1, 1)
    lo -= pad
    hi += pad
    const iw = W - PAD.left - PAD.right
    const ih = height - PAD.top - PAD.bottom
    const sx = (i: number) => PAD.left + (i / (points.length - 1)) * iw
    const sy = (v: number) => PAD.top + (1 - (v - lo) / (hi - lo)) * ih
    return { lo, hi, iw, ih, sx, sy }
  }, [points, height])

  if (!geom) return <div className="skeleton" style={{ height }} />

  const line = points
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${geom.sx(i).toFixed(1)},${geom.sy(p.equity).toFixed(1)}`)
    .join(' ')
  const area = `${line} L${geom.sx(points.length - 1).toFixed(1)},${geom.sy(0).toFixed(1)} L${geom.sx(0).toFixed(1)},${geom.sy(0).toFixed(1)} Z`

  const yTicks = ticks(geom.lo, geom.hi, 5)
  const labelEvery = Math.max(1, Math.floor(points.length / 6))

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = svgRef.current?.getBoundingClientRect()
    if (!rect) return
    const px = ((e.clientX - rect.left) / rect.width) * W
    const i = Math.round(((px - PAD.left) / geom.iw) * (points.length - 1))
    setHover(i >= 0 && i < points.length ? i : null)
  }

  const hp = hover !== null ? points[hover] : null

  return (
    <div className="chart-wrap" onMouseLeave={() => setHover(null)}>
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${height}`}
        role="img"
        aria-label="Backtest equity curve"
        onMouseMove={onMove}
      >
        {yTicks.map((t) => (
          <g key={t}>
            <line className="grid-line" x1={PAD.left} x2={W - PAD.right} y1={geom.sy(t)} y2={geom.sy(t)} />
            <text className="axis-label" x={PAD.left - 8} y={geom.sy(t) + 3.5} textAnchor="end">
              {money(t, 0)}
            </text>
          </g>
        ))}

        <path d={area} fill="var(--series-1)" opacity={0.12} />
        <line
          x1={PAD.left}
          x2={W - PAD.right}
          y1={geom.sy(0)}
          y2={geom.sy(0)}
          stroke="var(--axis)"
          strokeWidth={1.5}
        />
        <path d={line} fill="none" stroke="var(--series-1)" strokeWidth={2} strokeLinejoin="round" />

        <line className="axis-line" x1={PAD.left} x2={W - PAD.right} y1={height - PAD.bottom} y2={height - PAD.bottom} />
        {points.map((p, i) =>
          i % labelEvery === 0 ? (
            <text key={p.date} className="axis-label" x={geom.sx(i)} y={height - PAD.bottom + 15} textAnchor="middle">
              {p.date.slice(2, 7)}
            </text>
          ) : null,
        )}

        {hover !== null && (
          <g pointerEvents="none">
            <line x1={geom.sx(hover)} x2={geom.sx(hover)} y1={PAD.top} y2={height - PAD.bottom} stroke="var(--text-secondary)" strokeWidth={1} />
            <circle cx={geom.sx(hover)} cy={geom.sy(points[hover].equity)} r={4} fill="var(--series-1)" stroke="var(--surface-1)" strokeWidth={2} />
          </g>
        )}
      </svg>

      {hp && (
        <div className="tooltip" style={{ left: hover! > points.length / 2 ? 16 : undefined, right: hover! > points.length / 2 ? undefined : 74, top: 8 }}>
          <div className="tt-head">{hp.date}</div>
          <div className="tt-row">
            <span>Equity</span>
            <strong className={hp.equity >= 0 ? 'pos' : 'neg'}>{money(hp.equity, 0)}</strong>
          </div>
          <div className="tt-row"><span>Realized</span><span>{money(hp.realized, 0)}</span></div>
          <div className="tt-row"><span>Open</span><span>{money(hp.unrealized, 0)}</span></div>
          <div className="tt-row"><span>Underlying</span><span>{hp.spot.toFixed(2)}</span></div>
          <div className="tt-row"><span>Positions</span><span>{hp.open_positions}</span></div>
        </div>
      )}
    </div>
  )
}

/* -------------------------------------------------------------- drawdown */

export function DrawdownChart({ points, height = 110 }: { points: EquityPoint[]; height?: number }) {
  const series = useMemo(() => {
    let peak = 0
    return points.map((p) => {
      peak = Math.max(peak, p.equity)
      return p.equity - peak
    })
  }, [points])

  if (series.length < 2) return null

  const lo = Math.min(...series, -1)
  const iw = W - PAD.left - PAD.right
  const ih = height - PAD.top - 16
  const sx = (i: number) => PAD.left + (i / (series.length - 1)) * iw
  const sy = (v: number) => PAD.top + (v / lo) * ih

  const d =
    series.map((v, i) => `${i === 0 ? 'M' : 'L'}${sx(i).toFixed(1)},${sy(v).toFixed(1)}`).join(' ') +
    ` L${sx(series.length - 1).toFixed(1)},${PAD.top} L${sx(0).toFixed(1)},${PAD.top} Z`

  return (
    <div className="chart-wrap">
      <svg viewBox={`0 0 ${W} ${height}`} role="img" aria-label="Drawdown from peak equity">
        <line className="grid-line" x1={PAD.left} x2={W - PAD.right} y1={sy(lo)} y2={sy(lo)} />
        <text className="axis-label" x={PAD.left - 8} y={sy(lo) + 3.5} textAnchor="end">
          {money(lo, 0)}
        </text>
        <text className="axis-label" x={PAD.left - 8} y={PAD.top + 3.5} textAnchor="end">
          $0
        </text>
        <path d={d} fill="var(--status-critical)" opacity={0.28} />
        <path
          d={series.map((v, i) => `${i === 0 ? 'M' : 'L'}${sx(i).toFixed(1)},${sy(v).toFixed(1)}`).join(' ')}
          fill="none"
          stroke="var(--status-critical)"
          strokeWidth={1.5}
        />
        <line x1={PAD.left} x2={W - PAD.right} y1={PAD.top} y2={PAD.top} stroke="var(--axis)" strokeWidth={1} />
      </svg>
    </div>
  )
}

/* ------------------------------------------------------------- histogram */

export function Histogram({
  edges,
  counts,
  height = 180,
  below = 0,
  above = 0,
  min,
  max,
}: {
  edges: number[]
  counts: number[]
  height?: number
  /**
   * Observations outside the binned range. The forward-test histogram bins the
   * 1st-99th percentile so a single catastrophic path cannot flatten the chart,
   * and the excluded paths are reported here rather than dropped. On a short
   * premium position they are the whole risk, so they get a sentence rather
   * than a bar one pixel high.
   */
  below?: number
  above?: number
  min?: number
  max?: number
}) {
  const [hover, setHover] = useState<number | null>(null)
  if (!counts.length) return <div className="card-body muted tiny">No trades to plot.</div>

  const maxCount = Math.max(...counts, 1)
  const iw = W - PAD.left - PAD.right
  const ih = height - PAD.top - PAD.bottom
  const bw = iw / counts.length

  /*
   * A managed position produces a genuine point mass: every path that hits the
   * profit target exits at the same P/L, so one bucket holds most of the
   * distribution and a linear count axis flattens everything else to zero. That
   * spike is real and must stay visible -- but so must the shape of the losses,
   * which is the part that decides whether the strategy is survivable.
   *
   * So when one bucket dwarfs the rest, the count axis switches to a square
   * root scale. Ticks still carry true counts and the axis says what it is
   * doing, because a rescaled axis presented as linear is a lie about
   * magnitude. Bar HEIGHTS are no longer proportional to counts under this
   * scale, which is the trade being made deliberately and in the open.
   */
  const nonZero = counts.filter((c) => c > 0).sort((a, b) => a - b)
  const typical = nonZero.length ? nonZero[Math.floor(nonZero.length / 2)] : 1
  const compress = maxCount > typical * 20
  const scale = (c: number) => (compress ? Math.sqrt(c) / Math.sqrt(maxCount) : c / maxCount)

  return (
    <div className="chart-wrap" onMouseLeave={() => setHover(null)}>
      <svg viewBox={`0 0 ${W} ${height}`} role="img" aria-label="Distribution of trade profit and loss">
        {ticks(0, maxCount, 4).map((t) => (
          <g key={t}>
            <line className="grid-line" x1={PAD.left} x2={W - PAD.right} y1={PAD.top + ih - scale(t) * ih} y2={PAD.top + ih - scale(t) * ih} />
            <text className="axis-label" x={PAD.left - 8} y={PAD.top + ih - scale(t) * ih + 3.5} textAnchor="end">
              {t.toFixed(0)}
            </text>
          </g>
        ))}

        {counts.map((c, i) => {
          const h = scale(c) * ih
          const mid = (edges[i] + edges[i + 1]) / 2
          const win = mid >= 0
          return (
            <rect
              key={i}
              x={PAD.left + i * bw + 1}
              y={PAD.top + ih - h}
              width={Math.max(bw - 2, 1)}
              height={Math.max(h, c > 0 ? 2 : 0)}
              rx={2}
              fill={win ? 'var(--status-good)' : 'var(--status-critical)'}
              opacity={hover === null || hover === i ? 0.8 : 0.45}
              onMouseEnter={() => setHover(i)}
            />
          )
        })}

        <line className="axis-line" x1={PAD.left} x2={W - PAD.right} y1={PAD.top + ih} y2={PAD.top + ih} />
        {counts.map((_, i) =>
          i % Math.max(1, Math.floor(counts.length / 6)) === 0 ? (
            <text key={i} className="axis-label" x={PAD.left + i * bw + bw / 2} y={height - PAD.bottom + 15} textAnchor="middle">
              {money(edges[i], 0)}
            </text>
          ) : null,
        )}
        <text className="axis-label" x={(PAD.left + W - PAD.right) / 2} y={height - 3} textAnchor="middle">
          profit / loss per trade
        </text>
      </svg>

      {hover !== null && (
        <div className="tooltip" style={{ right: 16, top: 8 }}>
          <div className="tt-head">
            {money(edges[hover], 0)} to {money(edges[hover + 1], 0)}
          </div>
          <div className="tt-row">
            <span>Trades</span>
            <strong>{counts[hover]}</strong>
          </div>
        </div>
      )}

      {(below > 0 || above > 0 || compress) && (
        <div className="legend" style={{ paddingBottom: 10 }}>
          {compress && (
            <span className="item muted">
              count axis is square-root scaled — one bucket holds most of the paths (a
              profit target makes every winner exit at the same P/L) and a linear axis
              would flatten the rest to nothing
            </span>
          )}
          {below > 0 && (
            <span className="item neg">
              {below} beyond {money(edges[0], 0)} — worst {money(min, 0)}
            </span>
          )}
          {above > 0 && (
            <span className="item pos">
              {above} beyond {money(edges[edges.length - 1], 0)} — best {money(max, 0)}
            </span>
          )}
          <span className="item muted">off the chart so the rest is legible, not to hide them</span>
        </div>
      )}
    </div>
  )
}
