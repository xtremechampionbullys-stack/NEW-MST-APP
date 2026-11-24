'use client'

import { useMemo, useRef, useState } from 'react'
import { compact, money, num, ticks } from '@/lib/format'
import type { LiquidityMap } from '@/lib/types'

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

/** Which parts of the volume/order/liquidity map to draw over the candles. */
export interface OverlayLayers { zones: boolean; levels: boolean; liquidity: boolean; profile: boolean }

const PROFILE_W = 110

const W = 1000
// left padding fits the first date label; without it the earliest tick is
// clipped by the panel edge and reads as a rendering fault.
const PAD = { top: 14, right: 62, bottom: 22, left: 34 }
const VOL_H = 54

export function CandleChart({
  candles,
  height = 380,
  symbol,
  overlay,
  layers = { zones: true, levels: true, liquidity: true, profile: true },
}: {
  candles: Candle[]
  height?: number
  symbol: string
  /**
   * The volume/order/liquidity map for the same series. Drawn behind the
   * candles; anything priced outside the visible range is simply not drawn
   * rather than stretching the axis to reach it.
   */
  overlay?: LiquidityMap | null
  layers?: OverlayLayers
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

  const plotR = W - PAD.right
  const plotB = PAD.top + geom.priceH
  const inRange = (p: number) => p >= geom.yLo && p <= geom.yHi
  // Left edge of the candle at or after a timestamp. A zone that formed before
  // the first visible candle starts at the left edge of the plot.
  const xAt = (t: number) => {
    const i = candles.findIndex((c) => c.t >= t)
    return i <= 0 ? PAD.left : geom.x(i) - geom.step / 2
  }
  const clipId = `plot-${symbol.replace(/[^A-Za-z0-9]/g, '')}`

  const zones = overlay && layers.zones
    ? overlay.order_blocks.filter((z) => z.top >= geom.yLo && z.bottom <= geom.yHi) : []
  const pools = overlay && layers.liquidity
    ? overlay.liquidity.filter((p) => inRange(p.price)) : []
  const levels = overlay && layers.levels
    ? overlay.levels.filter((l) => inRange(l.price)) : []
  const profile = overlay && layers.profile ? overlay.profile : null
  const maxBin = profile ? Math.max(...profile.bins.map((b) => b.volume), 1) : 1

  // Price tags in the right gutter, nudged apart so two close levels do not
  // print on top of each other. The line stays at the true price.
  const tags = [...levels]
    .sort((a, b) => b.price - a.price)
    .reduce<{ lv: (typeof levels)[number]; ty: number }[]>((acc, lv) => {
      const want = geom.y(lv.price)
      const prev = acc[acc.length - 1]
      acc.push({ lv, ty: prev ? Math.max(want, prev.ty + 13) : want })
      return acc
    }, [])

  return (
    <div className="chart-wrap">
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${height}`}
        role="img"
        aria-label={overlay
          ? `${symbol} daily candles with volume, order blocks, liquidity and support/resistance`
          : `${symbol} daily candles with volume`}
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

        <defs>
          <clipPath id={clipId}>
            <rect x={PAD.left} y={PAD.top} width={plotR - PAD.left} height={geom.priceH} />
          </clipPath>
        </defs>

        {/* --- overlay, behind the candles ------------------------------ */}
        <g clipPath={`url(#${clipId})`}>
          {profile && profile.bins.map((b) => {
            const yT = geom.y(b.hi)
            const hh = Math.max(1, geom.y(b.lo) - yT - 1)
            const wBuy = (b.buy_volume / maxBin) * PROFILE_W
            const wSell = (b.sell_volume / maxBin) * PROFILE_W
            return (
              <g key={b.lo} opacity={0.3}>
                <rect x={PAD.left} y={yT} width={wBuy} height={hh} fill="var(--status-good)" />
                <rect x={PAD.left + wBuy} y={yT} width={wSell} height={hh} fill="var(--status-critical)" />
              </g>
            )
          })}
          {profile && inRange(profile.poc) && (
            <line x1={PAD.left} x2={PAD.left + PROFILE_W + 26} y1={geom.y(profile.poc)}
              y2={geom.y(profile.poc)} stroke="var(--series-4)" strokeWidth={1.2} />
          )}

          {zones.map((z) => {
            const col = z.side === 'bullish' ? 'var(--status-good)' : 'var(--status-critical)'
            const x0 = xAt(z.formed_t)
            const yT = geom.y(z.top)
            return (
              <rect key={z.id} x={x0} y={yT} width={plotR - x0}
                height={Math.max(2, geom.y(z.bottom) - yT)}
                fill={col} fillOpacity={z.status === 'fresh' ? 0.17 : 0.1}
                stroke={col} strokeOpacity={0.6} strokeWidth={1} />
            )
          })}

          {levels.map((lv) => {
            const col = lv.kind === 'support' ? 'var(--status-good)' : 'var(--status-critical)'
            return (
              <line key={`${lv.kind}-${lv.price}`} x1={PAD.left} x2={plotR}
                y1={geom.y(lv.price)} y2={geom.y(lv.price)} stroke={col}
                strokeWidth={lv.major ? 1.8 : 1} strokeOpacity={lv.major ? 0.95 : 0.55} />
            )
          })}

          {pools.map((p) => (
            <line key={`${p.side}-${p.price}`} x1={xAt(p.first_t)} x2={plotR}
              y1={geom.y(p.price)} y2={geom.y(p.price)} stroke="var(--series-4)"
              strokeWidth={1.2} strokeDasharray="5 4" />
          ))}
        </g>

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

        {/* --- overlay labels, above the candles so they stay readable --- */}
        <g clipPath={`url(#${clipId})`} pointerEvents="none">
          {zones.map((z) => {
            const mid = (Math.max(geom.y(z.top), PAD.top) + Math.min(geom.y(z.bottom), plotB)) / 2
            return (
              <text key={z.id} className="ob-label" x={plotR - 6} y={mid + 3.5} textAnchor="end">
                {z.id} · {z.touches} {z.touches === 1 ? 'touch' : 'touches'}
                {z.orders !== null ? ` · ${compact(z.orders)} orders` : ` · ${compact(z.volume)} vol`}
              </text>
            )
          })}
          {levels.filter((lv) => lv.major).map((lv) => (
            <text key={`m-${lv.price}`} className="ob-label" x={PAD.left + PROFILE_W + 34}
              y={geom.y(lv.price) + (lv.kind === 'support' ? -5 : 12)}>
              MAJOR {lv.kind.toUpperCase()} · {lv.touches} touches
            </text>
          ))}
          {pools.map((p) => (
            <text key={`pl-${p.side}-${p.price}`} className="ob-label liq"
              x={Math.min(xAt(p.first_t) + 4, plotR - 150)}
              y={geom.y(p.price) + (p.side === 'buy' ? -5 : 12)}>
              {p.side === 'buy' ? 'Buy-side' : 'Sell-side'} liquidity ×{p.touches}
            </text>
          ))}
        </g>

        {tags.map(({ lv, ty }) => {
          const col = lv.kind === 'support' ? 'var(--status-good)' : 'var(--status-critical)'
          return (
            <g key={`tag-${lv.kind}-${lv.price}`}>
              <rect x={plotR + 2} y={ty - 6.5} width={PAD.right - 4} height={13} rx={2}
                fill="var(--surface-1)" stroke={col} strokeWidth={lv.major ? 1.5 : 0.8} />
              <text className="axis-label" x={plotR + 6} y={ty + 3.5} fill={col}
                style={{ fill: col, fontWeight: lv.major ? 700 : 500 }}>
                {lv.kind === 'support' ? 'S' : 'R'} {num(lv.price, 2)}
              </text>
            </g>
          )
        })}

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
