'use client'

import { useMemo, useState } from 'react'
import { money } from '@/lib/format'
import type { AnalyzeResponse } from '@/lib/types'

/**
 * P/L across price and time.
 *
 * This is a diverging encoding -- P/L has a meaningful zero and two opposite
 * directions -- so it uses a blue/red pair with a neutral gray midpoint pinned
 * at exactly zero, never a rainbow and never a single sequential ramp (which
 * would hide the sign). The two arms are scaled independently against their own
 * extremes so a structure with a small max profit and a large max loss still
 * shows detail on the profit side.
 */

const NEG = ['var(--div-neg-1)', 'var(--div-neg-2)', 'var(--div-neg-3)']
const POS = ['var(--div-pos-1)', 'var(--div-pos-2)', 'var(--div-pos-3)']

interface Props {
  data: AnalyzeResponse
}

export function PLHeatmap({ data }: Props) {
  const [hover, setHover] = useState<{ r: number; c: number } | null>(null)
  const surface = data.surface
  const spot = data.analysis.spot

  const color = useMemo(() => {
    if (!surface) return () => 'var(--div-mid)'
    const maxPos = Math.max(surface.max, 1e-9)
    const maxNeg = Math.min(surface.min, -1e-9)
    return (v: number) => {
      if (Math.abs(v) < Math.max(maxPos, -maxNeg) * 0.02) return 'var(--div-mid)'
      if (v > 0) {
        const t = Math.min(v / maxPos, 1)
        return POS[t > 0.66 ? 2 : t > 0.33 ? 1 : 0]
      }
      const t = Math.min(v / maxNeg, 1)
      return NEG[t > 0.66 ? 2 : t > 0.33 ? 1 : 0]
    }
  }, [surface])

  if (!surface) return null

  // Column labels get thinned so they never collide.
  const colStep = Math.max(1, Math.ceil(surface.prices.length / 9))
  const rowStep = Math.max(1, Math.ceil(surface.days.length / 8))

  return (
    <div className="chart-wrap" onMouseLeave={() => setHover(null)}>
      <div className="legend">
        <span className="item muted">Loss</span>
        {[...NEG].reverse().map((c) => (
          <span key={c} className="line" style={{ background: c, width: 18, height: 10, borderRadius: 2 }} />
        ))}
        <span className="line" style={{ background: 'var(--div-mid)', width: 18, height: 10, borderRadius: 2 }} />
        {POS.map((c) => (
          <span key={c} className="line" style={{ background: c, width: 18, height: 10, borderRadius: 2 }} />
        ))}
        <span className="item muted">Profit</span>
        <span className="item muted" style={{ marginLeft: 'auto' }}>
          rows: days held · columns: {data.position.symbol} price
        </span>
      </div>

      <div style={{ padding: '4px 14px 14px', overflowX: 'auto' }}>
        <table
          className="num"
          style={{ borderCollapse: 'separate', borderSpacing: '2px', fontSize: 10 }}
          aria-label="Profit and loss by underlying price and days held"
        >
          <thead>
            <tr>
              <th style={{ position: 'sticky', left: 0, background: 'var(--surface-1)', padding: '0 6px 0 0', textAlign: 'right', color: 'var(--text-muted)', fontWeight: 500 }}>
                day
              </th>
              {surface.prices.map((p, c) => (
                <th
                  key={c}
                  style={{
                    padding: 0,
                    fontWeight: 500,
                    color: 'var(--text-muted)',
                    fontSize: 9.5,
                    minWidth: 22,
                  }}
                >
                  {c % colStep === 0 ? p.toFixed(0) : ''}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {surface.grid.map((row, r) => (
              <tr key={r}>
                <td
                  style={{
                    position: 'sticky',
                    left: 0,
                    background: 'var(--surface-1)',
                    padding: '0 6px 0 0',
                    textAlign: 'right',
                    color: 'var(--text-muted)',
                    fontSize: 9.5,
                  }}
                >
                  {r % rowStep === 0 ? surface.days[r].toFixed(0) : ''}
                </td>
                {row.map((v, c) => (
                  <td
                    key={c}
                    onMouseEnter={() => setHover({ r, c })}
                    style={{
                      background: color(v),
                      width: 22,
                      height: 15,
                      borderRadius: 2,
                      padding: 0,
                      cursor: 'crosshair',
                      outline:
                        hover?.r === r && hover?.c === c
                          ? '2px solid var(--text-primary)'
                          : Math.abs(surface.prices[c] - spot) < (surface.prices[1] - surface.prices[0]) / 2
                            ? '1px solid var(--border-strong)'
                            : 'none',
                    }}
                  />
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {hover && (
        <div className="tooltip" style={{ right: 18, top: 34 }}>
          <div className="tt-head">{surface.prices[hover.c].toFixed(2)}</div>
          <div className="tt-row">
            <span>Day</span>
            <span>{surface.days[hover.r].toFixed(0)}</span>
          </div>
          <div className="tt-row">
            <span>P/L</span>
            <strong className={surface.grid[hover.r][hover.c] >= 0 ? 'pos' : 'neg'}>
              {money(surface.grid[hover.r][hover.c], 0)}
            </strong>
          </div>
        </div>
      )}
    </div>
  )
}
