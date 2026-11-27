'use client'

import { useEffect, useMemo, useRef, useState } from 'react'

/**
 * Footprint chart: one column per one-minute bar, one cell per price row,
 * each cell showing volume that hit the bid × volume that lifted the ask.
 *
 * Drawn on a canvas rather than SVG because a 30-bar window is several
 * thousand text cells, and it has to stay responsive while paging.
 *
 * How to read it, since the encoding is dense:
 *
 * - **Cell shade** is that row's share of the bar's busiest row. The darkest
 *   cell in a column is where the bar's business was done.
 * - **Amber outline** is the POC — the single highest-volume row.
 * - **The bracket** on a column's left edge spans the value area (70% of
 *   volume); the thin candle beside it is the bar's open/close and range.
 * - **Green / red figures** are diagonal imbalances: the ask volume is at least
 *   3× the bid volume one row below (green), or the reverse (red). They are
 *   also set bold, so the signal does not rest on colour alone.
 * - **Dashed lines** are a simulated trade: entry at the POC, the stop, and
 *   the two targets, drawn from the signal bar to the exit.
 *
 * Shade is deliberately a single neutral ramp. Using green/red for the heat
 * as well would make "a lot traded here" and "buyers were aggressive here"
 * the same visual statement, and they are different facts.
 */

export interface FpBar {
  t: number; o: number; h: number; l: number; c: number
  volume: number; delta: number; cum_delta: number
  poc: number; poc_volume: number; poc_pos: number; vah: number; val: number; vwap: number
  buy_imbalances: number; sell_imbalances: number; buy_stack: number; sell_stack: number
  rows?: [number, number, number, number][]
}

export interface FpSignal { bar: number; t: number; side: number; entry: number; stop: number; reason: string }

export interface FpTrade {
  signal_bar: number; side: number; entry_t: number; entry: number; stop: number
  target1: number; target2: number; exit_t: number; exit: number; qty: number
  pnl: number; r: number; costs: number; hold_bars: number; reason: string; hit_t1: boolean
}

const COL_W = 92
const ROW_H = 15
const TOP = 22
const FOOT = 50
const AXIS_W = 62

function cssVar(el: Element, name: string, fallback: string) {
  const v = getComputedStyle(el).getPropertyValue(name).trim()
  return v || fallback
}

const fmtVol = (v: number) =>
  v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e4 ? `${(v / 1e3).toFixed(0)}k`
    : v >= 1e3 ? `${(v / 1e3).toFixed(1)}k` : v % 1 === 0 ? String(v) : v.toFixed(2)

export function FootprintChart({
  bars, start, rowSize, signals, trades, focusBar,
}: {
  bars: FpBar[]
  /** absolute index of bars[0] in the full tape */
  start: number
  rowSize: number
  signals: FpSignal[]
  trades: FpTrade[]
  focusBar?: number | null
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const axisRef = useRef<HTMLCanvasElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const [hover, setHover] = useState<string>('')
  const [themeTick, setThemeTick] = useState(0)

  const dp = useMemo(() => {
    const s = String(rowSize)
    const d = s.includes('.') ? s.split('.')[1].length : 0
    return Math.max(2, Math.min(d, 6))
  }, [rowSize])

  const geom = useMemo(() => {
    if (!bars.length) return null
    let hi = -Infinity
    let lo = Infinity
    for (const b of bars) {
      for (const r of b.rows ?? []) {
        if (r[0] > hi) hi = r[0]
        if (r[0] < lo) lo = r[0]
      }
    }
    if (!Number.isFinite(hi)) return null
    // Leave room for stops and targets of trades drawn in this window.
    for (const t of trades) {
      if (t.signal_bar >= start && t.signal_bar < start + bars.length) {
        hi = Math.max(hi, t.stop, t.target2)
        lo = Math.min(lo, t.stop, t.target2)
      }
    }
    const top = Math.round(hi / rowSize) + 1
    const bottom = Math.round(lo / rowSize) - 1
    const nRows = Math.min(top - bottom + 1, 400)
    const rowH = nRows > 120 ? 9 : nRows > 70 ? 12 : ROW_H
    const y = (price: number) => TOP + (top - price / rowSize) * rowH
    return { top, bottom, nRows, rowH, y, width: bars.length * COL_W, height: TOP + nRows * rowH + FOOT }
  }, [bars, trades, start, rowSize])

  // Re-read theme tokens when the theme attribute changes.
  useEffect(() => {
    const mo = new MutationObserver(() => setThemeTick((n) => n + 1))
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => mo.disconnect()
  }, [])

  useEffect(() => {
    const cv = canvasRef.current
    const ax = axisRef.current
    if (!cv || !ax || !geom) return
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    const C = {
      text: cssVar(cv, '--text-primary', '#ececec'),
      sub: cssVar(cv, '--text-secondary', '#b4b4b4'),
      muted: cssVar(cv, '--text-muted', '#8e8e8e'),
      grid: cssVar(cv, '--grid', 'rgba(255,255,255,0.08)'),
      good: cssVar(cv, '--status-good', '#2dd4a0'),
      bad: cssVar(cv, '--status-critical', '#f0666b'),
      poc: cssVar(cv, '--series-4', '#d9a021'),
      entry: cssVar(cv, '--series-1', '#5aa3f0'),
      mono: cssVar(cv, '--mono', 'monospace'),
    }

    for (const [el, w] of [[cv, geom.width], [ax, AXIS_W]] as const) {
      el.width = w * dpr
      el.height = geom.height * dpr
      el.style.width = `${w}px`
      el.style.height = `${geom.height}px`
    }
    const g = cv.getContext('2d')!
    g.setTransform(dpr, 0, 0, dpr, 0, 0)
    g.clearRect(0, 0, geom.width, geom.height)
    const { rowH, y } = geom
    const showText = rowH >= 12
    const font = (bold = false) => `${bold ? '700 ' : ''}${rowH >= 15 ? 10.5 : 9}px ${C.mono}`
    g.textBaseline = 'middle'

    bars.forEach((b, i) => {
      const x0 = i * COL_W
      const cellX = x0 + 12
      const cellW = COL_W - 16
      const rows = b.rows ?? []
      const maxRow = Math.max(...rows.map((r) => r[1] + r[2]), 1)
      const pocRow = Math.floor(b.poc / rowSize + 1e-9) * rowSize

      // column separator
      g.fillStyle = C.grid
      g.fillRect(x0, TOP, 1, geom.nRows * rowH)

      for (const [price, bid, ask, flag] of rows) {
        const yy = y(price) - rowH / 2
        const share = (bid + ask) / maxRow
        g.globalAlpha = 0.05 + 0.30 * share
        g.fillStyle = C.text
        g.fillRect(cellX, yy + 0.5, cellW, rowH - 1)
        g.globalAlpha = 1

        if (showText) {
          g.font = font(flag === -1)
          g.fillStyle = flag === -1 ? C.bad : C.sub
          g.textAlign = 'right'
          g.fillText(fmtVol(bid), cellX + cellW / 2 - 5, yy + rowH / 2 + 0.5)
          g.font = font(flag === 1)
          g.fillStyle = flag === 1 ? C.good : C.sub
          g.textAlign = 'left'
          g.fillText(fmtVol(ask), cellX + cellW / 2 + 5, yy + rowH / 2 + 0.5)
          g.fillStyle = C.muted
          g.textAlign = 'center'
          g.font = font()
          g.fillText('×', cellX + cellW / 2, yy + rowH / 2 + 0.5)
        } else if (flag !== 0) {
          g.fillStyle = flag === 1 ? C.good : C.bad
          g.fillRect(flag === 1 ? cellX + cellW - 4 : cellX, yy + 1, 4, rowH - 2)
        }

        if (Math.abs(price - pocRow) < rowSize / 2) {
          g.strokeStyle = C.poc
          g.lineWidth = 1.5
          g.strokeRect(cellX + 0.75, yy + 0.75, cellW - 1.5, rowH - 1.5)
        }
      }

      // value-area bracket and the candle sliver
      const up = b.c >= b.o
      g.strokeStyle = C.muted
      g.lineWidth = 1
      g.beginPath()
      const vaTop = y(b.vah - rowSize) - rowH / 2
      const vaBot = y(b.val) + rowH / 2
      g.moveTo(x0 + 10.5, vaTop); g.lineTo(x0 + 8.5, vaTop)
      g.lineTo(x0 + 8.5, vaBot); g.lineTo(x0 + 10.5, vaBot)
      g.stroke()
      const col = up ? C.good : C.bad
      const snap = (p: number) => y(Math.floor(p / rowSize + 1e-9) * rowSize)
      g.fillStyle = col
      g.fillRect(x0 + 4, snap(b.h) - rowH / 2, 1, snap(b.l) - snap(b.h) + rowH)
      const yo = snap(b.o)
      const yc = snap(b.c)
      g.fillRect(x0 + 2.5, Math.min(yo, yc) - rowH / 2 + 1, 4, Math.abs(yc - yo) + rowH - 2)

      // header: time
      g.fillStyle = C.muted
      g.font = `10px ${C.mono}`
      g.textAlign = 'center'
      g.fillText(new Date(b.t * 1000).toISOString().slice(11, 16), x0 + COL_W / 2, 10)

      // footer: volume, delta
      const fy = TOP + geom.nRows * rowH
      g.fillStyle = C.sub
      g.fillText(`V ${fmtVol(b.volume)}`, x0 + COL_W / 2, fy + 14)
      g.fillStyle = b.delta >= 0 ? C.good : C.bad
      g.font = `600 10px ${C.mono}`
      g.fillText(`Δ ${b.delta >= 0 ? '+' : '−'}${fmtVol(Math.abs(b.delta))}`, x0 + COL_W / 2, fy + 30)
    })

    // signals: a marker over the signal bar
    for (const s of signals) {
      const i = s.bar - start
      if (i < 0 || i >= bars.length) continue
      const b = bars[i]
      const cx = i * COL_W + COL_W / 2 + 4
      const yy = s.side > 0 ? y(b.l) + geom.rowH : y(b.h) - geom.rowH
      g.fillStyle = s.side > 0 ? C.good : C.bad
      g.beginPath()
      if (s.side > 0) { g.moveTo(cx, yy - 2); g.lineTo(cx - 6, yy + 8); g.lineTo(cx + 6, yy + 8) }
      else { g.moveTo(cx, yy + 2); g.lineTo(cx - 6, yy - 8); g.lineTo(cx + 6, yy - 8) }
      g.closePath(); g.fill()
    }

    // trades: entry / stop / targets from the bar after the signal to the exit
    for (const t of trades) {
      const i = t.signal_bar - start
      if (i < 0 || i >= bars.length) continue
      const xa = (i + 1) * COL_W
      const xb = Math.min((i + 2 + t.hold_bars) * COL_W, geom.width)
      const line = (price: number, colour: string, label: string, dash: number[]) => {
        const yy = Math.round(y(price)) + 0.5
        g.strokeStyle = colour
        g.lineWidth = 1.25
        g.setLineDash(dash)
        g.beginPath(); g.moveTo(xa, yy); g.lineTo(xb, yy); g.stroke()
        g.setLineDash([])
        g.font = `600 9px ${C.mono}`
        g.textAlign = 'left'
        const w = g.measureText(label).width + 6
        g.fillStyle = colour
        g.fillRect(xa, yy - 6, w, 12)
        g.fillStyle = '#0b0d12'
        g.fillText(label, xa + 3, yy + 0.5)
      }
      line(t.stop, C.bad, 'STOP', [4, 3])
      line(t.target2, C.good, 'T2', [4, 3])
      if (t.target1 !== t.target2) line(t.target1, C.good, 'T1', [2, 3])
      line(t.entry, C.entry, t.side > 0 ? 'LONG' : 'SHORT', [])
    }

    // price axis
    const a = ax.getContext('2d')!
    a.setTransform(dpr, 0, 0, dpr, 0, 0)
    a.clearRect(0, 0, AXIS_W, geom.height)
    a.font = `10px ${C.mono}`
    a.textBaseline = 'middle'
    a.textAlign = 'left'
    a.fillStyle = C.muted
    const every = Math.max(1, Math.round(28 / rowH))
    for (let r = geom.bottom; r <= geom.top; r++) {
      if ((r - geom.bottom) % every !== 0) continue
      a.fillText((r * rowSize).toFixed(dp), 8, y(r * rowSize))
    }
  }, [bars, geom, rowSize, signals, trades, start, dp, themeTick])

  // Bring the focused bar (a clicked trade) or the latest bar into view.
  useEffect(() => {
    const el = scrollRef.current
    if (!el || !geom) return
    const focused = focusBar != null && focusBar >= start && focusBar < start + bars.length
    const i = focused ? focusBar - start : bars.length - 1
    el.scrollLeft = focused ? Math.max(0, i * COL_W - el.clientWidth / 3) : el.scrollWidth
    // Vertically too: a 30-bar window can span far more rows than fit.
    const mid = (geom.y(bars[i].h) + geom.y(bars[i].l)) / 2
    el.scrollTop = Math.max(0, mid - el.clientHeight / 2)
  }, [focusBar, start, bars, geom])

  if (!geom) return <div className="skeleton" style={{ height: 360 }} />

  const onMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const r = e.currentTarget.getBoundingClientRect()
    const i = Math.floor((e.clientX - r.left) / COL_W)
    const b = bars[i]
    if (!b) return setHover('')
    const price = (geom.top - Math.round((e.clientY - r.top - TOP) / geom.rowH)) * rowSize
    const row = b.rows?.find((x) => Math.abs(x[0] - price) < rowSize / 2)
    const time = new Date(b.t * 1000).toISOString().slice(11, 16)
    const head = `${time} UTC · O ${b.o.toFixed(dp)} H ${b.h.toFixed(dp)} L ${b.l.toFixed(dp)} C ${b.c.toFixed(dp)} · POC ${b.poc.toFixed(dp)} · Δ ${b.delta.toFixed(0)}`
    setHover(row
      ? `${head}  |  ${row[0].toFixed(dp)}: bid ${fmtVol(row[1])} × ask ${fmtVol(row[2])}${row[3] === 1 ? ' · buy imbalance' : row[3] === -1 ? ' · sell imbalance' : ''}`
      : head)
  }

  return (
    <div>
      <div className="fp-readout" aria-live="off">{hover || 'Hover a cell for its bid × ask volume.'}</div>
      <div className="fp-frame">
        <div className="fp-scroll" ref={scrollRef}>
          <canvas
            ref={canvasRef}
            role="img"
            aria-label={`Footprint chart of ${bars.length} one-minute bars with bid and ask volume per price row`}
            onMouseMove={onMove}
            onMouseLeave={() => setHover('')}
          />
        </div>
        <canvas ref={axisRef} className="fp-axis" aria-hidden />
      </div>
      <div className="legend fp-legend">
        <span><i className="fp-key fp-key-poc" /> POC (highest-volume row)</span>
        <span><i className="fp-key fp-key-va" /> value area, 70% of volume</span>
        <span><b className="pos">ask</b> / <b className="neg">bid</b> bold = 3× diagonal imbalance</span>
        <span>cell = bid × ask · shade = share of the bar&apos;s busiest row</span>
        <span>▲▼ signal bar</span>
      </div>
    </div>
  )
}
