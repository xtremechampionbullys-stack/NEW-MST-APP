'use client'

import { useMemo, useRef, useState } from 'react'
import { compact, ticks } from '@/lib/format'
import type { BarPoint } from '@/lib/types'

/**
 * Underlying price history with an optional expected-move band.
 *
 * Candle up/down is polarity, so it uses the reserved status colors rather than
 * categorical slots -- green/red here means "closed higher/lower", not series
 * identity. The expected-move band is drawn from the option chain's own implied
 * volatility, which is the point: it puts the market's forecast on the same
 * axis as the history, so you can see whether the move you are paying for is
 * large or small relative to how this name has actually traded.
 */

const W = 900
const H = 250
const PAD = { top: 12, right: 74, bottom: 26, left: 10 }

interface Props {
  bars: BarPoint[]
  symbol: string
  expectedMove?: { low: number; high: number } | null
  height?: number
}

export function PriceChart({ bars, symbol, expectedMove, height = H }: Props) {
  const svgRef = useRef<SVGSVGElement>(null)
  const [hover, setHover] = useState<number | null>(null)

  const geom = useMemo(() => {
    if (!bars.length) return null
    let lo = Math.min(...bars.map((b) => b.l))
    let hi = Math.max(...bars.map((b) => b.h))
    if (expectedMove) {
      lo = Math.min(lo, expectedMove.low)
      hi = Math.max(hi, expectedMove.high)
    }
    const pad = (hi - lo) * 0.06
    lo -= pad
    hi += pad

    const iw = W - PAD.left - PAD.right
    const ih = height - PAD.top - PAD.bottom
    const step = iw / bars.length
    const sx = (i: number) => PAD.left + i * step + step / 2
    const sy = (v: number) => PAD.top + (1 - (v - lo) / (hi - lo)) * ih
    return { lo, hi, iw, ih, step, sx, sy }
  }, [bars, expectedMove, height])

  if (!geom || !bars.length) {
    return <div className="skeleton" style={{ height }} />
  }

  const yTicks = ticks(geom.lo, geom.hi, 5)
  const bw = Math.max(1.2, Math.min(geom.step * 0.62, 9))
  const last = bars[bars.length - 1]

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = svgRef.current?.getBoundingClientRect()
    if (!rect) return
    const px = ((e.clientX - rect.left) / rect.width) * W
    const i = Math.round((px - PAD.left - geom.step / 2) / geom.step)
    setHover(i >= 0 && i < bars.length ? i : null)
  }

  const hb = hover !== null ? bars[hover] : null

  return (
    <div className="chart-wrap" onMouseLeave={() => setHover(null)}>
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${height}`}
        role="img"
        aria-label={`${symbol} daily price history`}
        onMouseMove={onMove}
      >
        {yTicks.map((t) => (
          <g key={t}>
            <line className="grid-line" x1={PAD.left} x2={W - PAD.right} y1={geom.sy(t)} y2={geom.sy(t)} />
            <text className="axis-label" x={W - PAD.right + 6} y={geom.sy(t) + 3.5}>
              {t.toFixed(t < 20 ? 2 : 0)}
            </text>
          </g>
        ))}

        {expectedMove && (
          <g>
            <rect
              x={PAD.left}
              y={geom.sy(expectedMove.high)}
              width={geom.iw}
              height={Math.max(geom.sy(expectedMove.low) - geom.sy(expectedMove.high), 1)}
              fill="var(--series-1)"
              opacity={0.09}
            />
            <line
              x1={PAD.left}
              x2={W - PAD.right}
              y1={geom.sy(expectedMove.high)}
              y2={geom.sy(expectedMove.high)}
              stroke="var(--series-1)"
              strokeWidth={1}
              opacity={0.55}
            />
            <line
              x1={PAD.left}
              x2={W - PAD.right}
              y1={geom.sy(expectedMove.low)}
              y2={geom.sy(expectedMove.low)}
              stroke="var(--series-1)"
              strokeWidth={1}
              opacity={0.55}
            />
            <text className="axis-label" x={PAD.left + 4} y={geom.sy(expectedMove.high) - 4} fill="var(--series-1)">
              ±1σ expected move
            </text>
          </g>
        )}

        {bars.map((b, i) => {
          const up = b.c >= b.o
          const stroke = up ? 'var(--status-good)' : 'var(--status-critical)'
          const yO = geom.sy(b.o)
          const yC = geom.sy(b.c)
          return (
            <g key={i} opacity={hover === null || hover === i ? 1 : 0.68}>
              <line x1={geom.sx(i)} x2={geom.sx(i)} y1={geom.sy(b.h)} y2={geom.sy(b.l)} stroke={stroke} strokeWidth={1} />
              <rect
                x={geom.sx(i) - bw / 2}
                y={Math.min(yO, yC)}
                width={bw}
                height={Math.max(Math.abs(yC - yO), 1)}
                fill={stroke}
                rx={1}
              />
            </g>
          )
        })}

        <line
          x1={PAD.left}
          x2={W - PAD.right}
          y1={geom.sy(last.c)}
          y2={geom.sy(last.c)}
          stroke="var(--text-secondary)"
          strokeWidth={1}
          opacity={0.5}
        />

        {hover !== null && (
          <line
            x1={geom.sx(hover)}
            x2={geom.sx(hover)}
            y1={PAD.top}
            y2={height - PAD.bottom}
            stroke="var(--text-secondary)"
            strokeWidth={1}
            pointerEvents="none"
          />
        )}
      </svg>

      {hb && (
        <div className="tooltip" style={{ left: hover! > bars.length / 2 ? 14 : undefined, right: hover! > bars.length / 2 ? undefined : 88, top: 8 }}>
          <div className="tt-head">{new Date(hb.t).toLocaleDateString()}</div>
          <div className="tt-row"><span>Open</span><span>{hb.o.toFixed(2)}</span></div>
          <div className="tt-row"><span>High</span><span>{hb.h.toFixed(2)}</span></div>
          <div className="tt-row"><span>Low</span><span>{hb.l.toFixed(2)}</span></div>
          <div className="tt-row">
            <span>Close</span>
            <strong className={hb.c >= hb.o ? 'pos' : 'neg'}>{hb.c.toFixed(2)}</strong>
          </div>
          <div className="tt-row"><span>Volume</span><span>{compact(hb.v)}</span></div>
        </div>
      )}
    </div>
  )
}
