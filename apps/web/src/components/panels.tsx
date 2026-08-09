'use client'

import { money, num, pct, signedMoney } from '@/lib/format'
import type { AnalyzeResponse, Insight, LegRow, Param, Severity } from '@/lib/types'

/* ------------------------------------------------------------------ tiles */

export function MetricTiles({ data }: { data: AnalyzeResponse }) {
  const a = data.analysis
  const credit = a.is_credit

  const maxProfit = a.max_profit_unbounded ? 'Unlimited' : money(a.max_profit, 0)
  const maxLoss = a.max_loss_unbounded ? 'Unlimited' : money(a.max_loss, 0)

  return (
    <div className="tiles">
      <div className="tile">
        <div className="k">{credit ? 'Net credit' : 'Net debit'}</div>
        <div className="v">{money(Math.abs(a.net_debit), 0)}</div>
        <div className="n">{a.contracts} option leg{a.contracts === 1 ? '' : 's'} · {money(a.commission, 2)} fees</div>
      </div>
      <div className="tile">
        <div className="k">Max profit</div>
        <div className={`v ${a.max_profit_unbounded || (a.max_profit ?? 0) > 0 ? 'pos' : ''}`}>{maxProfit}</div>
        <div className="n">{a.risk_reward ? `${num(a.risk_reward, 2)}:1 reward/risk` : 'no capped reward'}</div>
      </div>
      <div className="tile">
        <div className="k">Max loss</div>
        <div className="v neg">{maxLoss}</div>
        <div className="n">{a.risk_defined ? 'risk defined' : 'undefined risk'}</div>
      </div>
      <div className="tile">
        <div className="k">Prob. of profit</div>
        <div className="v">{pct(a.probability_of_profit, 1)}</div>
        <div className="n">{a.measure === 'risk-neutral' ? 'market-implied' : 'your drift'}</div>
      </div>
      <div className="tile">
        <div className="k">Expected value</div>
        <div className={`v ${a.expected_value >= 0 ? 'pos' : 'neg'}`}>{signedMoney(a.expected_value, 0)}</div>
        <div className="n">CVaR 5%: {money(a.cvar_5pct, 0)}</div>
      </div>
      <div className="tile">
        <div className="k">Breakeven{a.breakevens.length === 1 ? '' : 's'}</div>
        <div className="v" style={{ fontSize: a.breakevens.length > 1 ? 15 : 19 }}>
          {a.breakevens.length ? a.breakevens.map((b) => b.toFixed(2)).join(' / ') : '—'}
        </div>
        <div className="n">spot {a.spot.toFixed(2)}</div>
      </div>
      <div className="tile">
        <div className="k">Capital</div>
        <div className="v">{money(a.capital.estimated_buying_power, 0)}</div>
        <div className="n">{a.return_on_capital ? `${pct(a.return_on_capital, 1)} max return` : 'Reg-T estimate'}</div>
      </div>
      <div className="tile">
        <div className="k">Expected move</div>
        <div className="v" style={{ fontSize: 15 }}>
          {a.expected_move_1sd.low.toFixed(1)} – {a.expected_move_1sd.high.toFixed(1)}
        </div>
        <div className="n">±{pct(a.expected_move_pct, 1)} · {a.dte.toFixed(0)} DTE · IV {pct(a.blended_iv, 1)}</div>
      </div>
    </div>
  )
}

/* ----------------------------------------------------------------- greeks */

const GREEK_HELP: Record<string, string> = {
  delta: 'Share-equivalent exposure. +100 behaves like 100 long shares.',
  gamma: 'How fast delta changes. Positive = position improves as it moves.',
  theta: 'Dollars per day from time decay. Positive means time pays you.',
  vega: 'Dollars per 1 point of implied volatility.',
  rho: 'Dollars per 1 point of interest rates.',
}

export function GreeksPanel({ data }: { data: AnalyzeResponse }) {
  const g = data.analysis.net_greeks
  const rows: [string, number, number][] = [
    ['Delta', g.delta, 0],
    ['Gamma', g.gamma, 3],
    ['Theta', g.theta, 2],
    ['Vega', g.vega, 2],
    ['Rho', g.rho, 2],
  ]
  return (
    <table className="table">
      <thead>
        <tr>
          <th>Greek</th>
          <th>Net</th>
          <th style={{ textAlign: 'left', paddingLeft: 16 }}>What it means here</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([label, value, dp]) => (
          <tr key={label}>
            <td>{label}</td>
            <td className={value >= 0 ? 'pos' : 'neg'}>{num(value, dp)}</td>
            <td className="tiny muted" style={{ textAlign: 'left', paddingLeft: 16 }}>
              {GREEK_HELP[label.toLowerCase()]}
            </td>
          </tr>
        ))}
        <tr>
          <td>Dollar delta</td>
          <td className={g.dollar_delta >= 0 ? 'pos' : 'neg'}>{money(g.dollar_delta, 0)}</td>
          <td className="tiny muted" style={{ textAlign: 'left', paddingLeft: 16 }}>
            Notional directional exposure at the current price.
          </td>
        </tr>
      </tbody>
    </table>
  )
}

/* -------------------------------------------------------------------- legs */

export function LegTable({ legs }: { legs: LegRow[] }) {
  return (
    <div className="scroll">
      <table className="table">
        <thead>
          <tr>
            <th>Leg</th>
            <th>Qty</th>
            <th>Entry</th>
            <th>Theo</th>
            <th>IV</th>
            <th>Δ</th>
            <th>Θ</th>
            <th>Vega</th>
          </tr>
        </thead>
        <tbody>
          {legs.map((l, i) => (
            <tr key={i}>
              <td>
                <span
                  className="swatch"
                  style={{ background: l.quantity > 0 ? 'var(--series-1)' : 'var(--series-2)' }}
                  aria-hidden
                />
                {l.label}
              </td>
              <td className={l.quantity > 0 ? 'pos' : 'neg'}>{l.quantity > 0 ? '+' : ''}{l.quantity}</td>
              <td>{num(l.entry_price, 2)}</td>
              <td>{num(l.theo, 2)}</td>
              <td>{l.kind === 'stock' ? '—' : pct(l.iv, 1)}</td>
              <td>{num(l.delta, 1)}</td>
              <td>{num(l.theta, 2)}</td>
              <td>{num(l.vega, 2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/* ---------------------------------------------------------------- insights */

const SEVERITY_STYLE: Record<Severity, { color: string; icon: string; label: string }> = {
  critical: { color: 'var(--status-critical)', icon: '✕', label: 'Critical' },
  warning: { color: 'var(--status-serious)', icon: '!', label: 'Warning' },
  caution: { color: 'var(--status-warning)', icon: '▲', label: 'Caution' },
  info: { color: 'var(--status-good)', icon: '✓', label: 'Note' },
}

export function InsightList({ insights }: { insights: Insight[] }) {
  if (!insights.length) {
    return <div className="card-body muted tiny">No flags raised for this structure.</div>
  }
  return (
    <div>
      {insights.map((ins, i) => {
        const s = SEVERITY_STYLE[ins.severity]
        return (
          <div className="insight" key={i}>
            {/* Icon + label, never color alone -- status hue is a reinforcement. */}
            <span className="icon" style={{ color: s.color }} aria-hidden>
              {s.icon}
            </span>
            <div>
              <div className="t">
                {ins.title} <span className="tiny muted">· {s.label}</span>
              </div>
              <div className="d">{ins.detail}</div>
            </div>
          </div>
        )
      })}
    </div>
  )
}

/* ------------------------------------------------------------- explanation */

export function ExplainPanel({ data }: { data: AnalyzeResponse }) {
  const t = data.template
  if (!t) {
    return (
      <div className="card-body muted tiny">
        Custom structure — pick a template to see the walkthrough, or keep building legs by hand.
      </div>
    )
  }
  return (
    <div className="card-body explain">
      <p style={{ color: 'var(--text-primary)', fontSize: 13 }}>{t.summary}</p>
      <h3>How it works</h3>
      <p>{t.mechanics}</p>
      <h3>When to use it</h3>
      <p>{t.when_to_use}</p>
      <h3>What can go wrong</h3>
      <p>{t.risk_notes}</p>
      <h3>Managing it</h3>
      <p>{t.management}</p>
      <div className="row" style={{ flexWrap: 'wrap', gap: 6, marginTop: 14 }}>
        {t.tags.map((tag) => (
          <span className="chip" key={tag}>{tag}</span>
        ))}
      </div>
    </div>
  )
}

/* ------------------------------------------------------------- param input */

export function ParamControls({
  params,
  values,
  onChange,
}: {
  params: Param[]
  values: Record<string, number | string>
  onChange: (key: string, value: number | string) => void
}) {
  return (
    <div>
      {params.map((p) => {
        const v = values[p.key] ?? p.default

        if (p.type === 'select' && p.options) {
          return (
            <div className="field" key={p.key}>
              <label htmlFor={`p-${p.key}`}>{p.label}</label>
              <select
                id={`p-${p.key}`}
                className="select"
                value={String(v)}
                onChange={(e) => onChange(p.key, e.target.value)}
              >
                {p.options.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
              {p.help && <div className="help">{p.help}</div>}
            </div>
          )
        }

        const nv = Number(v)
        const isDelta = p.type === 'delta'
        return (
          <div className="field" key={p.key}>
            <label htmlFor={`p-${p.key}`}>
              <span>{p.label}</span>
              <span className="value">{isDelta ? nv.toFixed(2) : nv}</span>
            </label>
            <input
              id={`p-${p.key}`}
              type="range"
              min={p.min ?? 0}
              max={p.max ?? 100}
              step={p.step ?? 1}
              value={nv}
              onChange={(e) => onChange(p.key, Number(e.target.value))}
            />
            {p.help && <div className="help">{p.help}</div>}
          </div>
        )
      })}
    </div>
  )
}

/* ------------------------------------------------------- vol sensitivity */

export function VolSensitivity({ data }: { data: AnalyzeResponse }) {
  const rows = data.analysis.vol_sensitivity
  const max = Math.max(...rows.map((r) => Math.abs(r.delta_pl)), 1)
  return (
    <table className="table">
      <thead>
        <tr>
          <th>IV shift</th>
          <th>P/L now</th>
          <th>Change</th>
          <th style={{ width: 120 }} />
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const w = (Math.abs(r.delta_pl) / max) * 100
          return (
            <tr key={r.shift}>
              <td>{r.shift > 0 ? '+' : ''}{(r.shift * 100).toFixed(0)} pts</td>
              <td>{money(r.pl, 0)}</td>
              <td className={r.delta_pl >= 0 ? 'pos' : 'neg'}>{signedMoney(r.delta_pl, 0)}</td>
              <td>
                <div style={{ display: 'flex', justifyContent: r.delta_pl >= 0 ? 'flex-start' : 'flex-end', height: 8 }}>
                  <div
                    style={{
                      width: `${w}%`,
                      background: r.delta_pl >= 0 ? 'var(--status-good)' : 'var(--status-critical)',
                      borderRadius: 2,
                      opacity: 0.8,
                    }}
                  />
                </div>
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}
