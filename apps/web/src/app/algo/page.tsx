'use client'

import { useState } from 'react'
import Link from 'next/link'
import { Shell } from '@/components/Shell'
import { LiveBadge, useLive } from '@/components/live'
import { api } from '@/lib/api'
import type { AlgoStrategy, GateConfig } from '@/lib/types'

/**
 * The algo gateway console — OpenAlgo's operating surface, on the paper book.
 *
 * Everything on this page is the part of an algo platform that sits in front
 * of the venue: keys, the risk gate, webhook strategies, the approval queue,
 * the audit trail, latency. The venue behind it is the paper book, and the
 * page says so in the header rather than letting the vocabulary of a live
 * gateway imply live routing.
 */

/* ================================================================ keys */

function KeysCard() {
  const keys = useLive(() => api.algoKeys(), [], 30000)
  const [name, setName] = useState('')
  const [fresh, setFresh] = useState<{ name: string; key: string } | null>(null)
  const [busy, setBusy] = useState(false)

  const create = async () => {
    if (!name.trim()) return
    setBusy(true)
    try {
      const r = await api.algoCreateKey(name.trim())
      setFresh({ name: name.trim(), key: r.key })
      setName('')
      keys.refresh()
    } finally { setBusy(false) }
  }

  return (
    <div className="card">
      <div className="card-head">
        <div>
          <h2>API keys</h2>
          <span className="sub">
            programmatic orders require one, in the <code>X-API-Key</code> header
          </span>
        </div>
      </div>
      <div className="card-body">
        <div className="row" style={{ gap: 8 }}>
          <input className="input" placeholder="key name — e.g. tv-bot" value={name}
            style={{ width: 220 }} onChange={(e) => setName(e.target.value)} />
          <button className="btn" onClick={create} disabled={busy || !name.trim()}>
            Create key
          </button>
        </div>

        {fresh && (
          <div className="key-reveal">
            <strong>{fresh.name}</strong>
            <code>{fresh.key}</code>
            <span className="tiny muted">
              Copy it now — it is stored only as a hash and never shown again.
            </span>
            <button className="btn ghost tiny" onClick={() => setFresh(null)}>Dismiss</button>
          </div>
        )}

        {(keys.data?.keys ?? []).length > 0 && (
          <table className="table" style={{ marginTop: 10 }}>
            <thead><tr><th style={{ textAlign: 'left' }}>Name</th><th>Prefix</th>
              <th>Last used</th><th>Status</th><th /></tr></thead>
            <tbody>
              {keys.data!.keys.map((k) => (
                <tr key={k.id}>
                  <td style={{ textAlign: 'left' }}>{k.name}</td>
                  <td><code>{k.prefix}…</code></td>
                  <td className="tiny">{k.last_used ? new Date(k.last_used).toLocaleTimeString() : 'never'}</td>
                  <td>{k.revoked ? <span className="chip">revoked</span>
                    : <span className="chip accent">active</span>}</td>
                  <td>
                    {!k.revoked && (
                      <button className="btn ghost tiny"
                        onClick={async () => { await api.algoRevokeKey(k.id); keys.refresh() }}>
                        Revoke
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}

/* ================================================================ gate */

function GateCard() {
  const gate = useLive(() => api.algoGate(), [], 15000)
  const [draft, setDraft] = useState<GateConfig | null>(null)
  const cfg = draft ?? gate.data?.config ?? null

  const save = async () => {
    if (!draft) return
    await api.algoSetGate(draft)
    setDraft(null)
    gate.refresh()
  }

  const num = (key: keyof GateConfig, label: string, step: number) => (
    <label className="field" key={key}>
      <span>{label}</span>
      <input className="input" type="number" step={step}
        value={cfg ? Number(cfg[key]) : 0}
        onChange={(e) => cfg && setDraft({ ...cfg, [key]: Number(e.target.value) })} />
    </label>
  )

  return (
    <div className="card">
      <div className="card-head">
        <div>
          <h2>Risk gate</h2>
          <span className="sub">
            every gateway order passes through it — order, smart, basket, split,
            webhook and approved alike; there is no bypass parameter
          </span>
        </div>
        <div className="row" style={{ gap: 8 }}>
          {gate.data && (
            <span className="chip">
              {gate.data.orders_today} orders today · realized ${gate.data.realized_today.toFixed(2)}
            </span>
          )}
          <LiveBadge state={gate} />
        </div>
      </div>
      <div className="card-body">
        {cfg && (
          <>
            <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}>
              {num('max_order_value', 'Max order value ($)', 1000)}
              {num('max_position_value', 'Max position / symbol ($)', 1000)}
              {num('max_orders_per_day', 'Max orders / day', 1)}
              {num('daily_loss_limit', 'Daily loss halt ($)', 100)}
              <label className="field"><span>Semi-auto approval</span>
                <button className="btn" aria-pressed={cfg.require_approval}
                  onClick={() => setDraft({ ...cfg, require_approval: !cfg.require_approval })}>
                  {cfg.require_approval ? 'On — orders queue for approval' : 'Off — orders fill immediately'}
                </button>
              </label>
            </div>
            {draft && (
              <div className="row" style={{ gap: 8, marginTop: 8 }}>
                <button className="btn primary" onClick={save}>Apply limits</button>
                <button className="btn ghost" onClick={() => setDraft(null)}>Discard</button>
              </div>
            )}
          </>
        )}
        {(gate.data?.recent_rejections ?? []).length > 0 && (
          <div style={{ marginTop: 10 }}>
            {gate.data!.recent_rejections.slice(-5).reverse().map((r, i) => (
              <div className="insight" key={`${r.at}-${i}`}>
                <span className="icon" style={{ color: 'var(--status-serious)' }} aria-hidden>⛔</span>
                <div className="d"><strong>{r.symbol}</strong> via {r.source}: {r.reason}</div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

/* ========================================================== strategies */

function StrategyRow({ s, onChanged }: { s: AlgoStrategy; onChanged: () => void }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="strategy-row">
      <div className="row" style={{ gap: 8, alignItems: 'center' }}>
        <span className={`dot ${s.active ? 'on' : 'off'}`} aria-hidden />
        <strong>{s.name}</strong>
        <span className="chip">{s.symbol} × {s.quantity}</span>
        <span className="chip">{s.mode}</span>
        <span className="tiny muted">{s.signal_count} signals</span>
        <div className="row" style={{ gap: 6, marginLeft: 'auto' }}>
          <button className="btn ghost tiny" onClick={() => setOpen((v) => !v)}>
            {open ? 'Hide' : 'Signals'}
          </button>
          <button className="btn ghost tiny"
            onClick={async () => { await api.algoToggleStrategy(s.id, !s.active); onChanged() }}>
            {s.active ? 'Pause' : 'Resume'}
          </button>
          <button className="btn ghost tiny"
            onClick={async () => { await api.algoDeleteStrategy(s.id); onChanged() }}>
            Delete
          </button>
        </div>
      </div>
      {open && (
        <div className="signal-log">
          {s.signals.length === 0 && <span className="tiny muted">No signals yet.</span>}
          {[...s.signals].reverse().map((sig, i) => (
            <div className="tiny" key={`${sig.at}-${i}`}>
              <code>{new Date(sig.at).toLocaleTimeString()}</code>{' '}
              {sig.action} → <strong>{sig.result}</strong>
              {sig.reason ? <span className="muted"> · {sig.reason}</span> : null}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function StrategiesCard() {
  const list = useLive(() => api.algoStrategies(), [], 15000)
  const [form, setForm] = useState({ name: '', symbol: 'SPY', quantity: 10,
    mode: 'both' as 'long' | 'short' | 'both' })
  const [created, setCreated] = useState<AlgoStrategy | null>(null)

  const create = async () => {
    if (!form.name.trim()) return
    const s = await api.algoCreateStrategy({ ...form, name: form.name.trim() })
    setCreated(s)
    list.refresh()
  }

  return (
    <div className="card">
      <div className="card-head">
        <div>
          <h2>Webhook strategies</h2>
          <span className="sub">
            a TradingView alert (or anything that can POST JSON) becomes an order
            — through the gate, logged per strategy
          </span>
        </div>
        <LiveBadge state={list} />
      </div>
      <div className="card-body">
        <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
          <input className="input" placeholder="strategy name" style={{ width: 180 }}
            value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <input className="input" style={{ width: 90, textTransform: 'uppercase' }}
            value={form.symbol}
            onChange={(e) => setForm({ ...form, symbol: e.target.value.toUpperCase() })} />
          <input className="input" type="number" min={1} style={{ width: 90 }}
            value={form.quantity}
            onChange={(e) => setForm({ ...form, quantity: Math.max(1, Number(e.target.value) || 1) })} />
          <div className="tabs">
            {(['long', 'short', 'both'] as const).map((m) => (
              <button key={m} className="tab" aria-selected={form.mode === m}
                onClick={() => setForm({ ...form, mode: m })}>{m}</button>
            ))}
          </div>
          <button className="btn" onClick={create} disabled={!form.name.trim()}>
            Create webhook
          </button>
        </div>

        {created && (
          <div className="key-reveal">
            <strong>{created.name} — point your alert here</strong>
            <code>POST {typeof window !== 'undefined' ? window.location.origin : ''}{created.webhook_path}</code>
            <span className="tiny">TradingView alert message (webhooks cannot set
              headers, so the secret travels in the body):</span>
            <code>{JSON.stringify(created.tradingview_alert_example)}</code>
            <span className="tiny muted">
              The secret is shown once. Actions: BUY, SELL, EXIT (flattens the position).
            </span>
            <button className="btn ghost tiny" onClick={() => setCreated(null)}>Dismiss</button>
          </div>
        )}

        <div style={{ marginTop: 8 }}>
          {(list.data?.strategies ?? []).map((s) => (
            <StrategyRow key={s.id} s={s} onChanged={() => list.refresh()} />
          ))}
          {(list.data?.strategies ?? []).length === 0 && !created && (
            <p className="tiny muted">No strategies yet — create one to get a webhook URL.</p>
          )}
        </div>
      </div>
    </div>
  )
}

/* ============================================================ approvals */

function PendingCard() {
  const pending = useLive(() => api.algoPending(), [], 8000)
  const rows = pending.data?.pending ?? []
  if (!rows.length) return null
  return (
    <div className="card">
      <div className="card-head">
        <div>
          <h2>Awaiting approval</h2>
          <span className="sub">
            semi-auto mode — fills price at the quote when approved, not when queued
          </span>
        </div>
      </div>
      <div className="card-body">
        {rows.map((o) => (
          <div className="row" key={o.id} style={{ gap: 10, padding: '6px 0', alignItems: 'center' }}>
            <strong>{o.side} {o.quantity} {o.symbol}</strong>
            <span className="tiny muted">{o.source} · queued {new Date(o.queued_at).toLocaleTimeString()}</span>
            <div className="row" style={{ gap: 6, marginLeft: 'auto' }}>
              <button className="btn"
                onClick={async () => { await api.algoApprove(o.id); pending.refresh() }}>
                Approve
              </button>
              <button className="btn ghost"
                onClick={async () => { await api.algoRejectPending(o.id); pending.refresh() }}>
                Reject
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

/* ============================================================ telemetry */

function TelemetryCards() {
  const status = useLive(() => api.algoStatus(), [], 10000)
  const traffic = useLive(() => api.algoTraffic(), [], 15000)
  return (
    <div className="grid-2">
      <div className="card">
        <div className="card-head">
          <div><h2>Event log</h2>
            <span className="sub">fills, rejections, queue and config changes — the audit trail</span></div>
          <LiveBadge state={status} />
        </div>
        <div className="card-body flush">
          <div className="event-log">
            {(status.data?.events ?? []).length === 0 && (
              <p className="tiny muted" style={{ padding: 12 }}>Nothing yet.</p>
            )}
            {[...(status.data?.events ?? [])].reverse().map((e, i) => (
              <div className={`event-row k-${e.kind}`} key={`${e.at}-${i}`}>
                <code>{new Date(e.at).toLocaleTimeString()}</code>
                <span className="chip">{e.kind}</span>
                <span>{e.detail}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <div><h2>API latency</h2>
            <span className="sub">handler time inside this process — broker round-trips are not in these numbers</span></div>
          <LiveBadge state={traffic} />
        </div>
        <div className="card-body flush">
          <table className="table">
            <thead><tr><th style={{ textAlign: 'left' }}>Endpoint</th>
              <th>Calls</th><th>p50</th><th>p95</th><th>Max</th></tr></thead>
            <tbody>
              {(traffic.data?.endpoints ?? []).slice(0, 10).map((e) => (
                <tr key={e.path}>
                  <td style={{ textAlign: 'left' }}><code>{e.path}</code></td>
                  <td className="num">{e.calls}</td>
                  <td className="num">{e.p50_ms}ms</td>
                  <td className="num">{e.p95_ms}ms</td>
                  <td className="num">{e.max_ms}ms</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

/* ================================================================ page */

export default function AlgoPage() {
  const status = useLive(() => api.algoStatus(), [], 15000)
  return (
    <Shell actions={
      <div className="row" style={{ gap: 8, marginLeft: 'auto' }}>
        <span className="chip">venue: paper</span>
        <LiveBadge state={status} />
      </div>
    }>
      <div className="page">
        <div className="banner">
          <span aria-hidden>▲</span>
          <span>
            <strong>Every order routed through this gateway fills into the paper
            book</strong> — the same one the <Link href="/lab">Strategy Lab</Link> and{' '}
            <Link href="/portfolio">Portfolio</Link> use. The gateway is the part in
            front of a venue: keys, the risk gate, approval, audit. Pointing it at a
            live broker is a routing change behind the same interface, gated on a
            sandbox-tested adapter.
          </span>
        </div>

        <PendingCard />
        <GateCard />
        <div className="grid-2">
          <KeysCard />
          <StrategiesCard />
        </div>
        <TelemetryCards />
      </div>
    </Shell>
  )
}
