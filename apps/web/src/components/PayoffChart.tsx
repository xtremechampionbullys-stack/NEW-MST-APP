'use client'

import { useMemo, useRef, useState } from 'react'
import { money, nearestIndex, ticks } from '@/lib/format'
import type { AnalyzeResponse } from '@/lib/types'

/**
 * The risk graph.
 *
 * Encoding decisions worth stating, because each is load-bearing:
 *
 * - The expiration line is the anchor and gets categorical slot 2 (orange). The
 *   T+N theoretical curves are an *ordered* series -- time to expiry -- so they
 *   use a single-hue blue ramp, light for today through dark as expiry
 *   approaches. Ordered data gets an ordinal ramp; unordered identity would get
 *   distinct hues. Mixing those up is the most common charting error here.
 * - Profit and loss shading is polarity, so it uses the reserved status colors
 *   at low alpha with the zero line as a hard midpoint. The fills never carry
 *   meaning alone: the tooltip and the tiles print the numbers.
 * - Every series is in the legend, and the two that matter (expiry, today) are
 *   also direct-labeled at the right edge, so identity never depends on color.
 */

const CURVE_RAMP = ['var(--seq-250)', 'var(--seq-350)', 'var(--seq-450)', 'var(--seq-550)']

const W = 900
const H = 420
const PAD = { top: 18, right: 74, bottom: 34, left: 62 }

interface Props {
  data: AnalyzeResponse
  height?: number
}

export function PayoffChart({ data, height = H }: Props) {
  const svgRef = useRef<SVGSVGElement>(null)
  const [hover, setHover] = useState<{ i: number; x: number; y: number } | null>(null)

  const { prices, expiry, theoretical } = data.curves
  const spot = data.analysis.spot
  const breakevens = data.analysis.breakevens

  // The last theoretical curve duplicates the expiry line; drop it so the
  // legend does not claim two series that draw on top of each other.
  const timeCurves = useMemo(
    () => theoretical.filter((c) => c.days < (data.analysis.dte || Infinity) - 1e-9),
    [theoretical, data.analysis.dte],
  )

  const geom = useMemo(() => {
    const xs = prices
    const all = [...expiry, ...timeCurves.flatMap((c) => c.pl)]
    let lo = Math.min(...all)
    let hi = Math.max(...all)
    const pad = Math.max((hi - lo) * 0.12, 1)
    lo -= pad
    hi += pad
    // Zero must be on screen -- a risk graph without the break-even line is
    // unreadable.
    lo = Math.min(lo, 0)
    hi = Math.max(hi, 0)

    const xMin = xs[0]
    const xMax = xs[xs.length - 1]
    const iw = W - PAD.left - PAD.right
    const ih = height - PAD.top - PAD.bottom

    const sx = (p: number) => PAD.left + ((p - xMin) / (xMax - xMin)) * iw
    const sy = (v: number) => PAD.top + (1 - (v - lo) / (hi - lo)) * ih

    return { xs, lo, hi, xMin, xMax, sx, sy, iw, ih }
  }, [prices, expiry, timeCurves, height])

  const path = (values: number[]) =>
    values.map((v, i) => `${i === 0 ? 'M' : 'L'}${geom.sx(geom.xs[i]).toFixed(2)},${geom.sy(v).toFixed(2)}`).join(' ')

  /** Area between the expiry curve and zero, split at the sign change. */
  const areas = useMemo(() => {
    const zeroY = geom.sy(0)
    const pos: string[] = []
    const neg: string[] = []
    let run: { i: number; sign: number }[] = []

    const flush = () => {
      if (run.length < 2) {
        run = []
        return
      }
      const d =
        `M${geom.sx(geom.xs[run[0].i]).toFixed(2)},${zeroY.toFixed(2)} ` +
        run.map((r) => `L${geom.sx(geom.xs[r.i]).toFixed(2)},${geom.sy(expiry[r.i]).toFixed(2)}`).join(' ') +
        ` L${geom.sx(geom.xs[run[run.length - 1].i]).toFixed(2)},${zeroY.toFixed(2)} Z`
      ;(run[0].sign > 0 ? pos : neg).push(d)
      run = []
    }

    for (let i = 0; i < expiry.length; i++) {
      const sign = expiry[i] >= 0 ? 1 : -1
      if (run.length && run[run.length - 1].sign !== sign) {
        flush()
      }
      run.push({ i, sign })
    }
    flush()
    return { pos, neg }
  }, [expiry, geom])

  const yTicks = ticks(geom.lo, geom.hi, 6)
  const xTicks = ticks(geom.xMin, geom.xMax, 7)
  const strikes = [...new Set(data.position.legs.map((l) => l.strike).filter((s): s is number => !!s))]

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = svgRef.current?.getBoundingClientRect()
    if (!rect) return
    const px = ((e.clientX - rect.left) / rect.width) * W
    if (px < PAD.left || px > W - PAD.right) {
      setHover(null)
      return
    }
    const value = geom.xMin + ((px - PAD.left) / geom.iw) * (geom.xMax - geom.xMin)
    const i = nearestIndex(geom.xs, value)
    setHover({ i, x: ((e.clientX - rect.left) / rect.width) * 100, y: ((e.clientY - rect.top) / rect.height) * 100 })
  }

  const hoverPl = hover ? expiry[hover.i] : null

  return (
    <div className="chart-wrap" onMouseLeave={() => setHover(null)}>
      <div className="legend">
        <span className="item">
          <span className="line" style={{ background: 'var(--series-2)' }} />
          At expiration
        </span>
        {timeCurves.map((c, i) => (
          <span className="item" key={c.label}>
            <span className="line" style={{ background: CURVE_RAMP[Math.min(i, CURVE_RAMP.length - 1)] }} />
            {c.label === 'T+0' ? 'Today (T+0)' : c.label}
          </span>
        ))}
        <span className="item muted">
          <span className="line" style={{ background: 'var(--status-good)', opacity: 0.55 }} />
          profit
          <span className="line" style={{ background: 'var(--status-critical)', opacity: 0.55, marginLeft: 8 }} />
          loss
        </span>
      </div>

      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${height}`}
        role="img"
        aria-label={`Profit and loss versus ${data.position.symbol} price at expiration`}
        onMouseMove={onMove}
      >
        {/* horizontal grid */}
        {yTicks.map((t) => (
          <g key={`y${t}`}>
            <line className="grid-line" x1={PAD.left} x2={W - PAD.right} y1={geom.sy(t)} y2={geom.sy(t)} />
            <text className="axis-label" x={PAD.left - 8} y={geom.sy(t) + 3.5} textAnchor="end">
              {money(t, 0)}
            </text>
          </g>
        ))}

        {/* P/L polarity shading */}
        {areas.pos.map((d, i) => (
          <path key={`p${i}`} d={d} fill="var(--status-good)" opacity={0.14} />
        ))}
        {areas.neg.map((d, i) => (
          <path key={`n${i}`} d={d} fill="var(--status-critical)" opacity={0.14} />
        ))}

        {/* zero line */}
        <line
          x1={PAD.left}
          x2={W - PAD.right}
          y1={geom.sy(0)}
          y2={geom.sy(0)}
          stroke="var(--axis)"
          strokeWidth={1.5}
        />

        {/* strike ticks along the bottom */}
        {strikes.map((k) =>
          k >= geom.xMin && k <= geom.xMax ? (
            <line
              key={`k${k}`}
              x1={geom.sx(k)}
              x2={geom.sx(k)}
              y1={height - PAD.bottom}
              y2={height - PAD.bottom - 7}
              stroke="var(--text-muted)"
              strokeWidth={1.5}
            />
          ) : null,
        )}

        {/* spot marker */}
        {spot >= geom.xMin && spot <= geom.xMax && (
          <g>
            <line
              x1={geom.sx(spot)}
              x2={geom.sx(spot)}
              y1={PAD.top}
              y2={height - PAD.bottom}
              stroke="var(--text-muted)"
              strokeWidth={1}
              opacity={0.7}
            />
            <text className="axis-label" x={geom.sx(spot) + 5} y={PAD.top + 10}>
              spot {spot.toFixed(2)}
            </text>
          </g>
        )}

        {/* time-slice curves, ordinal ramp */}
        {timeCurves.map((c, i) => (
          <path
            key={c.label}
            d={path(c.pl)}
            fill="none"
            stroke={CURVE_RAMP[Math.min(i, CURVE_RAMP.length - 1)]}
            strokeWidth={2}
            strokeLinejoin="round"
          />
        ))}

        {/* expiration line */}
        <path d={path(expiry)} fill="none" stroke="var(--series-2)" strokeWidth={2} strokeLinejoin="round" />

        {/* breakeven markers */}
        {breakevens.map((b) =>
          b >= geom.xMin && b <= geom.xMax ? (
            <g key={`be${b}`}>
              <circle cx={geom.sx(b)} cy={geom.sy(0)} r={4.5} fill="var(--series-2)" stroke="var(--surface-1)" strokeWidth={2} />
              <text className="axis-label" x={geom.sx(b)} y={geom.sy(0) - 9} textAnchor="middle" fill="var(--text-secondary)">
                {b.toFixed(2)}
              </text>
            </g>
          ) : null,
        )}

        {/* direct labels at the right edge */}
        <text className="axis-label" x={W - PAD.right + 6} y={geom.sy(expiry[expiry.length - 1]) + 3.5} fill="var(--series-2)">
          expiry
        </text>
        {timeCurves.length > 0 && (
          <text
            className="axis-label"
            x={W - PAD.right + 6}
            y={geom.sy(timeCurves[0].pl[timeCurves[0].pl.length - 1]) + 3.5}
            fill={CURVE_RAMP[0]}
          >
            today
          </text>
        )}

        {/* x axis */}
        <line className="axis-line" x1={PAD.left} x2={W - PAD.right} y1={height - PAD.bottom} y2={height - PAD.bottom} />
        {xTicks.map((t) => (
          <text key={`x${t}`} className="axis-label" x={geom.sx(t)} y={height - PAD.bottom + 15} textAnchor="middle">
            {t.toFixed(t < 20 ? 1 : 0)}
          </text>
        ))}
        <text className="axis-label" x={(PAD.left + W - PAD.right) / 2} y={height - 3} textAnchor="middle">
          {data.position.symbol} price at expiration
        </text>

        {/* crosshair */}
        {hover && (
          <g pointerEvents="none">
            <line
              x1={geom.sx(geom.xs[hover.i])}
              x2={geom.sx(geom.xs[hover.i])}
              y1={PAD.top}
              y2={height - PAD.bottom}
              stroke="var(--text-secondary)"
              strokeWidth={1}
            />
            <circle
              cx={geom.sx(geom.xs[hover.i])}
              cy={geom.sy(expiry[hover.i])}
              r={4}
              fill="var(--series-2)"
              stroke="var(--surface-1)"
              strokeWidth={2}
            />
            {timeCurves.map((c, i) => (
              <circle
                key={c.label}
                cx={geom.sx(geom.xs[hover.i])}
                cy={geom.sy(c.pl[hover.i])}
                r={3.5}
                fill={CURVE_RAMP[Math.min(i, CURVE_RAMP.length - 1)]}
                stroke="var(--surface-1)"
                strokeWidth={2}
              />
            ))}
          </g>
        )}
      </svg>

      {hover && hoverPl !== null && (
        <div
          className="tooltip"
          style={{
            left: `${hover.x > 62 ? hover.x - 20 : hover.x + 3}%`,
            top: '10px',
            transform: hover.x > 62 ? 'translateX(-100%)' : undefined,
          }}
        >
          <div className="tt-head">{geom.xs[hover.i].toFixed(2)}</div>
          <div className="tt-row">
            <span>At expiration</span>
            <strong className={hoverPl >= 0 ? 'pos' : 'neg'}>{money(hoverPl, 0)}</strong>
          </div>
          {timeCurves.map((c) => (
            <div className="tt-row" key={c.label}>
              <span>{c.label === 'T+0' ? 'Today' : c.label}</span>
              <span className={c.pl[hover.i] >= 0 ? 'pos' : 'neg'}>{money(c.pl[hover.i], 0)}</span>
            </div>
          ))}
          <div className="tt-row" style={{ marginTop: 4, opacity: 0.75 }}>
            <span>vs spot</span>
            <span>{(((geom.xs[hover.i] - spot) / spot) * 100).toFixed(1)}%</span>
          </div>
        </div>
      )}
    </div>
  )
}
