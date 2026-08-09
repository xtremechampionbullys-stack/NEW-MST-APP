'use client'

import { useEffect, useState } from 'react'
import { api } from '@/lib/api'
import type { SavedStrategy } from '@/lib/types'

/**
 * Save and reload a strategy setup.
 *
 * What gets stored is the template plus its parameters -- not the computed
 * analytics. Reloading re-prices against today's chain, which is the point: a
 * saved condor should show you what that structure costs *now*, not a frozen
 * snapshot of what it cost when you saved it.
 *
 * The panel says plainly whether the backend is persistent. Letting someone
 * build a library that silently evaporates on restart would be worse than not
 * offering the feature.
 */

interface Props {
  symbol: string
  templateId: string | null
  templateName: string
  params: Record<string, number | string>
  spot: number | null
  onLoad: (saved: SavedStrategy) => void
}

export function SavedStrategies({ symbol, templateId, templateName, params, spot, onLoad }: Props) {
  const [items, setItems] = useState<SavedStrategy[]>([])
  const [backend, setBackend] = useState<string>('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = async () => {
    try {
      const res = await api.listSaved()
      setItems(res.strategies)
      setBackend(res.backend)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  useEffect(() => {
    refresh()
  }, [])

  const save = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!templateId) return
    setBusy(true)
    try {
      await api.saveStrategy({
        symbol,
        name: name.trim() || `${templateName} · ${symbol}`,
        template_id: templateId,
        params,
        entry_spot: spot,
      })
      setName('')
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (id: string) => {
    try {
      await api.deleteSaved(id)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  return (
    <div className="card">
      <div className="card-head">
        <h2>Saved setups</h2>
        <span className="sub">
          {backend === 'sql' ? 'persistent' : backend ? 'session only' : ''}
        </span>
      </div>

      <div style={{ padding: '10px 14px 4px' }}>
        <form onSubmit={save} className="row" style={{ gap: 6 }}>
          <input
            className="input"
            placeholder={`${templateName} · ${symbol}`}
            value={name}
            onChange={(e) => setName(e.target.value)}
            aria-label="Name for this setup"
          />
          <button className="btn primary" type="submit" disabled={busy || !templateId}>
            Save
          </button>
        </form>
        {backend === 'memory' && (
          <div className="help" style={{ marginTop: 6 }}>
            No database connected — saved setups last until the API restarts. Set
            DATABASE_URL to keep them.
          </div>
        )}
        {error && <div className="help" style={{ color: 'var(--status-critical)' }}>{error}</div>}
      </div>

      {items.length === 0 ? (
        <div className="card-body muted tiny">Nothing saved yet.</div>
      ) : (
        <div style={{ maxHeight: 220, overflowY: 'auto' }}>
          {items.map((s) => (
            <div key={s.id} className="row" style={{ gap: 6, padding: '6px 14px' }}>
              <button
                className="strat"
                style={{ padding: 0, flex: 1 }}
                onClick={() => onLoad(s)}
                title="Load this setup and re-price it against today's chain"
              >
                <span className="name">{s.name}</span>
                <span className="desc">
                  {s.symbol}
                  {s.entry_spot ? ` · saved at ${s.entry_spot.toFixed(2)}` : ''}
                  {s.created_at ? ` · ${s.created_at.slice(0, 10)}` : ''}
                </span>
              </button>
              <button
                className="btn ghost tiny"
                onClick={() => remove(s.id)}
                aria-label={`Delete ${s.name}`}
                style={{ padding: '2px 7px' }}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
