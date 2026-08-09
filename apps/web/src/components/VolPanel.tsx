'use client'

import { useMemo } from 'react'
import { pct, ticks } from '@/lib/format'
import type { VolResponse } from '@/lib/types'

/**
 * Volatility term structure and skew.
 *
 * Two separate small charts rather than one chart with two y-axes. Implied vol
 * by expiry and implied vol by strike share a unit but not a domain, and a
 * dual-axis plot would invent a relationship between them. Two plots, one axis
 * each, is the honest rendering.
 */

const W = 400
const H = 150
const PAD = { top: 12, right: 12, bottom: 24, left: 40 }

export function VolPanel({ vol }: { vol: VolResponse }) {
  const term = vol.term_structure
  const rank = vol.iv_rank

  const termGeom = useMemo(() => {
    if (term.length < 2) return null
    const xs = term.map((t) => t.dte)
    const ys = term.map((t) => t.atm_iv)
    const lo = Math.min(...ys) * 0.96
    const hi = Math.max(...ys) * 1.04
    const iw = W - PAD.left - PAD.right
    const ih = H - PAD.top - PAD.bottom
    const sx = (d: number) => PAD.left + ((d - xs[0]) / (xs[xs.length - 1] - xs[0])) * iw
    const sy = (v: number) => PAD.top + (1 - (v - lo) / (hi - lo)) * ih
    return { xs, ys, lo, hi, sx, sy }
  }, [term])

  const skewGeom = useMemo(() => {
    const pts = vol.skew.filter((s) => Math.abs(s.moneyness) <= 0.25)
    if (pts.length < 4) return null
    // Convention: the skew curve is built from out-of-the-money contracts --
    // puts below spot, calls above. Plotting both full curves would draw one
    // series directly on top of the other wherever they share a surface, which
    // reads as a missing series rather than as agreement.
    const calls = pts
      .filter((p) => p.right === 'call' && p.strike >= vol.spot)
      .sort((a, b) => a.strike - b.strike)
    const puts = pts
      .filter((p) => p.right === 'put' && p.strike <= vol.spot)
      .sort((a, b) => a.strike - b.strike)
    if (calls.length < 2 || puts.length < 2) return null
    const all = [...calls, ...puts]
    const lo = Math.min(...all.map((p) => p.iv)) * 0.96
    const hi = Math.max(...all.map((p) => p.iv)) * 1.04
    const kLo = Math.min(...all.map((p) => p.strike))
    const kHi = Math.max(...all.map((p) => p.strike))
    const iw = W - PAD.left - PAD.right
    const ih = H - PAD.top - PAD.bottom
    const sx = (k: number) => PAD.left + ((k - kLo) / (kHi - kLo)) * iw
    const sy = (v: number) => PAD.top + (1 - (v - lo) / (hi - lo)) * ih
    return { calls, puts, lo, hi, kLo, kHi, sx, sy }
  }, [vol.skew])

  const line = (pts: { x: number; y: number }[]) =>
    pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(2)},${p.y.toFixed(2)}`).join(' ')

  return (
    <div>
      <div className="tiles">
        <div className="tile">
          <div className="k">IV rank</div>
          <div className="v">{rank?.iv_rank != null ? pct(rank.iv_rank, 0) : '—'}</div>
          <div className="n">
            percentile {rank?.iv_percentile != null ? pct(rank.iv_percentile, 0) : '—'}
            {rank?.method === 'history'
              ? ` · ${rank.observations}d history`
              : rank?.method === 'cross-sectional'
                ? ' · chain estimate'
                : ''}
          </div>
        </div>
        <div className="tile">
          <div className="k">ATM IV 30d</div>
          <div className="v">{pct(vol.atm_iv_30d, 1)}</div>
          <div className="n">implied</div>
        </div>
        <div className="tile">
          <div className="k">Realized 20d</div>
          <div className="v">{pct(vol.realized_vol_20d, 1)}</div>
          <div className="n">close-to-close</div>
        </div>
        <div className="tile">
          <div className="k">Variance premium</div>
          <div className={`v ${(vol.variance_risk_premium ?? 0) >= 0 ? 'pos' : 'neg'}`}>
            {vol.variance_risk_premium != null
              ? `${vol.variance_risk_premium >= 0 ? '+' : ''}${(vol.variance_risk_premium * 100).toFixed(1)}`
              : '—'}
          </div>
          <div className="n">implied − realized, points</div>
        </div>
      </div>

      {rank?.method === 'cross-sectional' && (
        <div className="banner tiny">
          <span aria-hidden>▲</span>
          <span>
            <strong>IV rank is an estimate.</strong> {rank.note}
          </span>
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: 1, background: 'var(--border)' }}>
        <div style={{ background: 'var(--surface-1)', padding: '10px 12px' }}>
          <div className="tiny muted" style={{ marginBottom: 4 }}>
            Term structure — ATM implied volatility by days to expiry
          </div>
          {termGeom ? (
            <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="ATM implied volatility term structure">
              {ticks(termGeom.lo, termGeom.hi, 4).map((t) => (
                <g key={t}>
                  <line className="grid-line" x1={PAD.left} x2={W - PAD.right} y1={termGeom.sy(t)} y2={termGeom.sy(t)} />
                  <text className="axis-label" x={PAD.left - 6} y={termGeom.sy(t) + 3} textAnchor="end">
                    {(t * 100).toFixed(0)}%
                  </text>
                </g>
              ))}
              <path
                d={line(term.map((t) => ({ x: termGeom.sx(t.dte), y: termGeom.sy(t.atm_iv) })))}
                fill="none"
                stroke="var(--series-1)"
                strokeWidth={2}
              />
              {term.map((t) => (
                <circle key={t.dte} cx={termGeom.sx(t.dte)} cy={termGeom.sy(t.atm_iv)} r={3.5} fill="var(--series-1)" stroke="var(--surface-1)" strokeWidth={2}>
                  <title>{`${t.dte} DTE · IV ${(t.atm_iv * 100).toFixed(1)}% · expected move ±${t.expected_move.toFixed(2)}`}</title>
                </circle>
              ))}
              <line className="axis-line" x1={PAD.left} x2={W - PAD.right} y1={H - PAD.bottom} y2={H - PAD.bottom} />
              {term.filter((_, i) => i % Math.ceil(term.length / 5) === 0).map((t) => (
                <text key={t.dte} className="axis-label" x={termGeom.sx(t.dte)} y={H - PAD.bottom + 14} textAnchor="middle">
                  {t.dte}d
                </text>
              ))}
            </svg>
          ) : (
            <div className="muted tiny">Not enough expirations quoted.</div>
          )}
        </div>

        <div style={{ background: 'var(--surface-1)', padding: '10px 12px' }}>
          <div className="tiny muted" style={{ marginBottom: 4 }}>
            Skew — implied volatility by strike, {vol.skew_expiration ?? 'nearest expiry'}
          </div>
          {skewGeom ? (
            <>
              <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Implied volatility skew by strike">
                {ticks(skewGeom.lo, skewGeom.hi, 4).map((t) => (
                  <g key={t}>
                    <line className="grid-line" x1={PAD.left} x2={W - PAD.right} y1={skewGeom.sy(t)} y2={skewGeom.sy(t)} />
                    <text className="axis-label" x={PAD.left - 6} y={skewGeom.sy(t) + 3} textAnchor="end">
                      {(t * 100).toFixed(0)}%
                    </text>
                  </g>
                ))}
                <line
                  x1={skewGeom.sx(vol.spot)}
                  x2={skewGeom.sx(vol.spot)}
                  y1={PAD.top}
                  y2={H - PAD.bottom}
                  stroke="var(--text-muted)"
                  strokeWidth={1}
                />
                <path
                  d={line(skewGeom.puts.map((p) => ({ x: skewGeom.sx(p.strike), y: skewGeom.sy(p.iv) })))}
                  fill="none"
                  stroke="var(--series-3)"
                  strokeWidth={2}
                />
                <path
                  d={line(skewGeom.calls.map((p) => ({ x: skewGeom.sx(p.strike), y: skewGeom.sy(p.iv) })))}
                  fill="none"
                  stroke="var(--series-2)"
                  strokeWidth={2}
                />
                <line className="axis-line" x1={PAD.left} x2={W - PAD.right} y1={H - PAD.bottom} y2={H - PAD.bottom} />
                <text className="axis-label" x={skewGeom.sx(skewGeom.kLo)} y={H - PAD.bottom + 14}>
                  {skewGeom.kLo.toFixed(0)}
                </text>
                <text className="axis-label" x={skewGeom.sx(skewGeom.kHi)} y={H - PAD.bottom + 14} textAnchor="end">
                  {skewGeom.kHi.toFixed(0)}
                </text>
              </svg>
              <div className="legend" style={{ padding: '2px 0 0' }}>
                <span className="item">
                  <span className="line" style={{ background: 'var(--series-3)' }} />
                  OTM puts
                </span>
                <span className="item">
                  <span className="line" style={{ background: 'var(--series-2)' }} />
                  OTM calls
                </span>
                <span className="item muted">vertical rule = spot</span>
              </div>
            </>
          ) : (
            <div className="muted tiny">Not enough strikes quoted with implied volatility.</div>
          )}
        </div>
      </div>
    </div>
  )
}
