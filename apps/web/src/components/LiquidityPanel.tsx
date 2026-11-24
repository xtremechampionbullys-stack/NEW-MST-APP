'use client'

import { compact, num } from '@/lib/format'
import type { LiquidityMap } from '@/lib/types'
import type { OverlayLayers } from './CandleChart'

/**
 * The numbers behind the chart overlay: per-zone counters, the level ladder,
 * and the resting-liquidity pools.
 *
 * The chart can only fit one counter on each zone. Everything it leaves out is
 * here — and so is the statement of what "orders" means, in the column header
 * rather than a footnote, because that word is the one most likely to be read
 * as something this app does not have (an order book).
 */

const LAYERS: { id: keyof OverlayLayers; label: string }[] = [
  { id: 'zones', label: 'Order blocks' },
  { id: 'levels', label: 'Support / resistance' },
  { id: 'liquidity', label: 'Liquidity' },
  { id: 'profile', label: 'Volume profile' },
]

export function LayerToggles({
  layers, onChange,
}: { layers: OverlayLayers; onChange: (next: OverlayLayers) => void }) {
  return (
    <div className="tabs tabs-wrap" role="group" aria-label="Indicator layers">
      {LAYERS.map((l) => (
        <button key={l.id} className="tab" aria-pressed={layers[l.id]} aria-selected={layers[l.id]}
          onClick={() => onChange({ ...layers, [l.id]: !layers[l.id] })}>
          {l.label}
        </button>
      ))}
    </div>
  )
}

const price = (v: number | null | undefined) => (v === null || v === undefined ? '—' : num(v, 2))
const dist = (v: number | null) => (v === null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(1)}%`)
const day = (t: number) => new Date(t * 1000).toISOString().slice(0, 10)

export function LiquidityPanel({ data }: { data: LiquidityMap }) {
  const s = data.summary
  if (!s) return <div className="card-body tiny muted">{data.note}</div>

  const ordersNote = data.order_count_source === null
    ? 'this provider reports no trade count'
    : data.order_count_source === 'generated'
      ? 'generated trade prints — synthetic provider'
      : 'executed trades, from the provider'

  return (
    <>
      <div className="legend" style={{ paddingBottom: 10 }}>
        <span><span className="swatch" style={{ background: 'var(--status-good)' }} /> bullish block / support</span>
        <span><span className="swatch" style={{ background: 'var(--status-critical)' }} /> bearish block / resistance</span>
        <span><span className="swatch dash" /> resting liquidity (equal highs / lows)</span>
        <span><span className="swatch line" style={{ background: 'var(--series-4)' }} /> point of control</span>
      </div>

      <div className="tiles">
        <div className="tile">
          <div className="k">Major resistance</div>
          <div className="v num neg">{price(s.major_resistance)}</div>
          <div className="n">nearest any: {price(s.nearest_resistance)}</div>
        </div>
        <div className="tile">
          <div className="k">Last close</div>
          <div className="v num">{price(data.spot)}</div>
          <div className="n">ATR(14) {price(data.atr)}</div>
        </div>
        <div className="tile">
          <div className="k">Major support</div>
          <div className="v num pos">{price(s.major_support)}</div>
          <div className="n">nearest any: {price(s.nearest_support)}</div>
        </div>
        <div className="tile">
          <div className="k">Live order blocks</div>
          <div className="v num">{s.bullish_zones + s.bearish_zones}</div>
          <div className="n">
            {s.bullish_zones} bullish · {s.bearish_zones} bearish · {s.zones_broken} broken since
          </div>
        </div>
        {data.profile && (
          <div className="tile">
            <div className="k">Point of control</div>
            <div className="v num">{price(data.profile.poc)}</div>
            <div className="n">
              value area {price(data.profile.value_area_low)} – {price(data.profile.value_area_high)}
            </div>
          </div>
        )}
      </div>

      <div style={{ overflowX: 'auto' }}>
        <table className="table">
          <thead>
            <tr>
              <th>Zone</th>
              <th>Range</th>
              <th>Status</th>
              <th title="Separate returns into the zone after it was confirmed">Touches</th>
              <th title="Bars whose range overlapped the zone">Bars</th>
              <th title={`Trade count prorated into the zone — ${ordersNote}`}>Orders</th>
              <th title="Bar volume prorated by the share of each bar's range inside the zone">Volume</th>
              <th title="Estimated from where each bar closed in its range">Buy / sell (est.)</th>
              <th>Delta (est.)</th>
              <th title="Volume of the block candle relative to the average bar">Block vol</th>
              <th>From price</th>
            </tr>
          </thead>
          <tbody>
            {data.order_blocks.map((z) => {
              const buyPct = z.volume > 0 ? (z.buy_volume / z.volume) * 100 : 50
              return (
                <tr key={z.id}>
                  <td>
                    <strong>{z.id}</strong>{' '}
                    <span className={z.side === 'bullish' ? 'pos' : 'neg'}>
                      {z.side === 'bullish' ? '▲ demand' : '▼ supply'}
                    </span>
                    <div className="tiny muted">formed {day(z.formed_t)}</div>
                  </td>
                  <td>{num(z.bottom, 2)} – {num(z.top, 2)}</td>
                  <td>{z.status === 'fresh' ? 'untested' : 'tested'}</td>
                  <td><strong>{z.touches}</strong></td>
                  <td>{z.bars_in_zone}</td>
                  <td><strong>{z.orders === null ? '—' : compact(z.orders)}</strong></td>
                  <td>{compact(z.volume)}</td>
                  <td>
                    <div className="row" style={{ gap: 6, justifyContent: 'flex-end' }}>
                      <span>{buyPct.toFixed(0)}%</span>
                      <span className="split-bar" role="img"
                        aria-label={`${buyPct.toFixed(0)} percent buy, ${(100 - buyPct).toFixed(0)} percent sell, estimated`}>
                        <span style={{ width: `${buyPct}%` }} />
                        <span style={{ width: `${100 - buyPct}%` }} />
                      </span>
                      <span>{(100 - buyPct).toFixed(0)}%</span>
                    </div>
                  </td>
                  <td className={z.delta >= 0 ? 'pos' : 'neg'}>
                    {z.delta >= 0 ? '+' : ''}{compact(z.delta)}
                  </td>
                  <td>{z.formation_rel_volume === null ? '—' : `${z.formation_rel_volume.toFixed(2)}×`}</td>
                  <td>{dist(z.distance_pct)}</td>
                </tr>
              )
            })}
            {data.order_blocks.length === 0 && (
              <tr><td colSpan={11} className="muted" style={{ textAlign: 'left' }}>
                No live order blocks in this window — every zone that formed has since been closed through.
              </td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="grid-2" style={{ padding: 14 }}>
        <div>
          <div className="tiny muted" style={{ marginBottom: 6 }}>SUPPORT &amp; RESISTANCE</div>
          <table className="table">
            <thead>
              <tr><th>Level</th><th>Price</th><th>Touches</th><th>Strength</th><th>From price</th></tr>
            </thead>
            <tbody>
              {data.levels.map((lv) => (
                <tr key={`${lv.kind}-${lv.price}`}>
                  <td>
                    <span className={lv.kind === 'support' ? 'pos' : 'neg'}>
                      {lv.kind === 'support' ? 'Support' : 'Resistance'}
                    </span>
                    {lv.major && <span className="chip accent" style={{ marginLeft: 6 }}>major</span>}
                  </td>
                  <td><strong>{num(lv.price, 2)}</strong></td>
                  <td>{lv.touches}</td>
                  <td>{lv.strength}</td>
                  <td>{dist(lv.distance_pct)}</td>
                </tr>
              ))}
              {data.levels.length === 0 && (
                <tr><td colSpan={5} className="muted" style={{ textAlign: 'left' }}>
                  No price was defended twice in this window.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
        <div>
          <div className="tiny muted" style={{ marginBottom: 6 }}>RESTING LIQUIDITY</div>
          <table className="table">
            <thead>
              <tr><th>Pool</th><th>Price</th><th>Equal pivots</th><th>Since</th><th>From price</th></tr>
            </thead>
            <tbody>
              {data.liquidity.map((p) => (
                <tr key={`${p.side}-${p.price}`}>
                  <td>{p.side === 'buy' ? 'Buy-side · above highs' : 'Sell-side · below lows'}</td>
                  <td><strong>{num(p.price, 2)}</strong></td>
                  <td>{p.touches}</td>
                  <td>{day(p.first_t)}</td>
                  <td>{dist(p.distance_pct)}</td>
                </tr>
              ))}
              {data.liquidity.length === 0 && (
                <tr><td colSpan={5} className="muted" style={{ textAlign: 'left' }}>
                  No untouched equal highs or lows in this window.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card-body tiny muted" style={{ borderTop: '1px solid var(--border)' }}>
        <strong>What the counters are.</strong> {data.limits} Orders column: {ordersNote}.{' '}
        {data.synthetic && data.note}
      </div>
    </>
  )
}
