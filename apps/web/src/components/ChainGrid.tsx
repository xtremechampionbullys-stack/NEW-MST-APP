'use client'

import { useMemo, useState } from 'react'
import { compact, num, pct } from '@/lib/format'
import type { ChainResponse } from '@/lib/types'

/**
 * Option chain, calls left / puts right around a centre strike column.
 *
 * Two design notes:
 *
 * - In-the-money rows carry a faint tint rather than a saturated fill. Large
 *   saturated blocks read as loud and, worse, compete with the volatility
 *   column for attention. The tint is one shade off the surface.
 * - The bid/ask spread column is percentage of mid, not dollars. A $0.05 spread
 *   is nothing on a $12 option and disqualifying on a $0.15 one, and the
 *   percentage is what actually decides whether a structure is tradable.
 */

interface Props {
  chain: ChainResponse
  expiration: string
  onExpirationChange: (exp: string) => void
}

export function ChainGrid({ chain, expiration, onExpirationChange }: Props) {
  const [rangePct, setRangePct] = useState(0.12)

  const rows = useMemo(() => {
    const inExp = chain.contracts.filter((c) => c.expiration === expiration)
    const strikes = [...new Set(inExp.map((c) => c.strike))]
      .filter((k) => Math.abs(k / chain.spot - 1) <= rangePct)
      .sort((a, b) => a - b)
    return strikes.map((k) => ({
      strike: k,
      call: inExp.find((c) => c.strike === k && c.right === 'call') ?? null,
      put: inExp.find((c) => c.strike === k && c.right === 'put') ?? null,
    }))
  }, [chain, expiration, rangePct])

  const itmTint = 'color-mix(in oklab, var(--series-1) 8%, transparent)'

  return (
    <div>
      <div className="row" style={{ padding: '10px 14px', gap: 12, flexWrap: 'wrap' }}>
        <select
          className="select"
          style={{ width: 'auto' }}
          value={expiration}
          onChange={(e) => onExpirationChange(e.target.value)}
          aria-label="Expiration"
        >
          {chain.expirations.map((e) => {
            const dte = Math.round((new Date(e).getTime() - Date.now()) / 86400000)
            return (
              <option key={e} value={e}>
                {e} · {dte}d
              </option>
            )
          })}
        </select>
        <div className="row tiny muted" style={{ gap: 6 }}>
          <span>strike range</span>
          <input
            type="range"
            min={0.03}
            max={0.4}
            step={0.01}
            value={rangePct}
            onChange={(e) => setRangePct(Number(e.target.value))}
            style={{ width: 110 }}
            aria-label="Strike range around spot"
          />
          <span className="num">±{(rangePct * 100).toFixed(0)}%</span>
        </div>
        <span className="chip" style={{ marginLeft: 'auto' }}>{rows.length} strikes</span>
      </div>

      <div className="scroll">
        <table className="table">
          <thead>
            <tr>
              <th colSpan={5} style={{ textAlign: 'center', color: 'var(--text-secondary)' }}>Calls</th>
              <th style={{ textAlign: 'center' }}>Strike</th>
              <th colSpan={5} style={{ textAlign: 'center', color: 'var(--text-secondary)' }}>Puts</th>
            </tr>
            <tr>
              <th>OI</th><th>Vol</th><th>IV</th><th>Δ</th><th>Bid/Ask</th>
              <th style={{ textAlign: 'center' }} />
              <th>Bid/Ask</th><th>Δ</th><th>IV</th><th>Vol</th><th>OI</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const callItm = r.strike < chain.spot
              const putItm = r.strike > chain.spot
              const atm = Math.abs(r.strike - chain.spot) < (rows[1]?.strike - rows[0]?.strike || 1) / 2
              return (
                <tr key={r.strike}>
                  <td style={{ background: callItm ? itmTint : undefined }}>{compact(r.call?.open_interest)}</td>
                  <td style={{ background: callItm ? itmTint : undefined }}>{compact(r.call?.volume)}</td>
                  <td style={{ background: callItm ? itmTint : undefined }}>{pct(r.call?.implied_vol, 1)}</td>
                  <td style={{ background: callItm ? itmTint : undefined }}>{num(r.call?.delta, 2)}</td>
                  <td style={{ background: callItm ? itmTint : undefined }}>
                    {num(r.call?.bid, 2)} / {num(r.call?.ask, 2)}
                    {r.call?.spread_pct != null && r.call.spread_pct > 0.15 && (
                      <span className="tiny" style={{ color: 'var(--status-warning)', marginLeft: 5 }} title="Wide spread">
                        {pct(r.call.spread_pct, 0)}
                      </span>
                    )}
                  </td>
                  <td
                    style={{
                      textAlign: 'center',
                      fontWeight: atm ? 650 : 500,
                      background: atm ? 'var(--surface-3)' : 'var(--surface-2)',
                    }}
                  >
                    {r.strike.toFixed(r.strike < 25 ? 1 : 0)}
                  </td>
                  <td style={{ background: putItm ? itmTint : undefined }}>
                    {num(r.put?.bid, 2)} / {num(r.put?.ask, 2)}
                    {r.put?.spread_pct != null && r.put.spread_pct > 0.15 && (
                      <span className="tiny" style={{ color: 'var(--status-warning)', marginLeft: 5 }} title="Wide spread">
                        {pct(r.put.spread_pct, 0)}
                      </span>
                    )}
                  </td>
                  <td style={{ background: putItm ? itmTint : undefined }}>{num(r.put?.delta, 2)}</td>
                  <td style={{ background: putItm ? itmTint : undefined }}>{pct(r.put?.implied_vol, 1)}</td>
                  <td style={{ background: putItm ? itmTint : undefined }}>{compact(r.put?.volume)}</td>
                  <td style={{ background: putItm ? itmTint : undefined }}>{compact(r.put?.open_interest)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
