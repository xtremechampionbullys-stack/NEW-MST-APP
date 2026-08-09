'use client'

import { useMemo, useRef, useState } from 'react'
import { money, num, ticks } from '@/lib/format'

/**
 * Candlestick + volume chart drawn from this API's own bars.
 *
 * This exists because the TradingView widget renders TradingView's data and
 * cannot render ours — so without a native chart, the Charting page shows the
 * market but never the series the strategies, backtests and forecasts are all
 * computed from. Those are the bars a user needs to see to check the analytics
 * against something.
 *
 * It reads the UDF `/api/tv/history` endpoint, which means it exercises the
 * same datafeed the TradingView Charting Library would use. If this chart is
 * right, that integration is right.
 */

export interface Candle { t: number; o: number; h: number; l: number; c: number; v: number }

const W = 1000
// left padding fits the first date label; without it the earliest tick is
// clipped by the panel edge and reads as a rendering fault.
const PAD = { top: 14, right: 62, bottom: 22, left: 34 }
const VOL_H = 54

export function CandleChart({
  candles,
  height = 380,
  symbol,
}: {
  candles: Candle[]
  height?: number
  symbol: string
}) {
  const svgRef = useRef<SVGSVGElement>(null)
  const [hover, setHover] = useState<number | null>(null)

  const geom = useMemo(() => {
    if (candles.length < 2) return null
    const lo = Math.min(...candles.map((c) => c.l))
    const hi = Math.max(...candles.map((c) => c.h))
    const pad = (hi - lo) * 0.06 || 1
    const yLo = lo - pad
    const yHi = hi + pad
    const priceH = height - PAD.top - PAD.bottom - VOL_H
    const iw = W - PAD.left - PAD.right
    const step = iw / candles.length
    const bw = Math.max(1, Math.min(11, step * 0.68))
    const x = (i: number) => PAD.left + i * step + step / 2
    const y = (v: number) => PAD.top + priceH - ((v - yLo) / (yHi - yLo)) * priceH
    const maxVol = Math.max(...candles.map((c) => c.v), 1)
    const vy = (v: number) => (v / maxVol) * (VOL_H - 8)
    return { x, y, vy, yLo, yHi, step, bw, priceH, iw }
  }, [candles, height])

  if (!geom) return <div className="skeleton" style={{ height }} />

  const volTop = PAD.top + geom.priceH + 12
  const h = candles[hover ?? -1]

  return (
    <div className="chart-wrap">
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${height}`}
        role="img"
        aria-label={`${symbol} daily candles with volume`}
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const r = svgRef.current?.getBoundingClientRect()
          if (!r) return
          const px = ((e.clientX - r.left) / r.width) * W
          const i = Math.floor((px - PAD.left) / geom.step)
          setHover(i >= 0 && i < candles.length ? i : null)
        }}
      >
        {ticks(geom.yLo, geom.yHi, 5).map((t) => (
          <g key={t}>
            <line className="grid-line" x1={PAD.left} x2={W - PAD.right} y1={geom.y(t)} y2={geom.y(t)} />
            <text className="axis-label" x={W - PAD.right + 6} y={geom.y(t) + 3.5}>
              {num(t, 2)}
            </text>
          </g>
        ))}

        {candles.map((c, i) => {
          const up = c.c >= c.o
          const col = up ? 'var(--status-good)' : 'var(--status-critical)'
          const yO = geom.y(c.o)
          const yC = geom.y(c.c)
          return (
            <g key={c.t} opacity={hover === null || hover === i ? 1 : 0.55}>
              <line x1={geom.x(i)} x2={geom.x(i)} y1={geom.y(c.h)} y2={geom.y(c.l)}
                stroke={col} strokeWidth={1} />
              <rect
                x={geom.x(i) - geom.bw / 2}
                y={Math.min(yO, yC)}
                width={geom.bw}
                height={Math.max(1, Math.abs(yC - yO))}
                fill={col}
              />
              <rect x={geom.x(i) - geom.bw / 2} y={volTop + (VOL_H - 8) - geom.vy(c.v)}
                width={geom.bw} height={geom.vy(c.v)} fill={col} opacity={0.32} />
            </g>
          )
        })}

        <line className="axis-line" x1={PAD.left} x2={W - PAD.right} y1={PAD.top + geom.priceH} y2={PAD.top + geom.priceH} />

        {candles
          .map((c, i) => ({ c, i }))
          .filter(({ i }) => i % Math.max(1, Math.floor(candles.length / 7)) === 0)
          .map(({ c, i }) => (
            <text key={c.t} className="axis-label" x={geom.x(i)} y={height - 6} textAnchor="middle">
              {new Date(c.t * 1000).toISOString().slice(2, 10)}
            </text>
          ))}

        {hover != null && (
          <line x1={geom.x(hover)} x2={geom.x(hover)} y1={PAD.top} y2={PAD.top + geom.priceH}
            stroke="var(--border-strong)" strokeWidth={1} />
        )}
      </svg>

      {h && (
        <div className="tooltip" style={{ left: `${(geom.x(hover!) / W) * 100}%`, top: 8 }}>
          <div className="tt-head">{new Date(h.t * 1000).toISOString().slice(0, 10)}</div>
          {(['o', 'h', 'l', 'c'] as const).map((k) => (
            <div className="tt-row" key={k}>
              <span>{k.toUpperCase()}</span>
              <strong>{num(h[k], 2)}</strong>
            </div>
          ))}
          <div className="tt-row">
            <span>Vol</span>
            <span>{money(h.v, 0).replace('$', '')}</span>
          </div>
        </div>
      )}
    </div>
  )
}
