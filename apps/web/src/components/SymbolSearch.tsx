'use client'

import { useEffect, useRef, useState } from 'react'
import { api } from '@/lib/api'
import { num } from '@/lib/format'

/**
 * Global ticker search.
 *
 * Opens on ⌘K / Ctrl-K, matches against the API's own symbol index, and shows a
 * live quote for the highlighted row so the choice is confirmed before it is
 * committed. Fully keyboard-driven: arrows move, Enter selects, Escape closes.
 *
 * One honesty detail. The synthetic provider resolves *any* ticker — it
 * generates a series from a seed rather than looking anything up — so a typo
 * returns a confident quote for a company that does not exist. Rows that came
 * from the index are marked as known; a free-typed symbol that is not in the
 * index is offered with an explicit warning rather than silently accepted.
 */

interface Hit {
  symbol: string
  description: string
  known: boolean
  exchange?: string
}

/** A common stock/ETF ticker, optionally qualified for TradingView. */
function manualHit(value: string): Hit | null {
  const input = value.trim().toUpperCase().replace(/\s+/g, '')
  const match = input.match(/^(?:([A-Z0-9._-]+):)?([A-Z0-9._-]{1,15})$/)
  if (!match) return null
  const [, exchange, symbol] = match
  return {
    symbol,
    exchange,
    description: exchange ? `Use ${exchange}:${symbol}` : 'Use ticker directly in TradingView',
    known: false,
  }
}

export function SymbolSearch({
  value,
  onSelect,
}: {
  value?: string
  onSelect: (symbol: string, exchange?: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<Hit[]>([])
  const [cursor, setCursor] = useState(0)
  const [preview, setPreview] = useState<{ symbol: string; last: number; change_pct: number | null } | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const boxRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setOpen(true)
      }
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  useEffect(() => {
    if (!open) return
    inputRef.current?.focus()
    const onDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  // Debounced, and every response is checked against the query that is current
  // when it lands — otherwise a slow early request overwrites a fast later one
  // and the list shows results for a prefix the user has already typed past.
  useEffect(() => {
    if (!open) return
    let cancelled = false
    const q = query
    const t = setTimeout(() => {
      api
        .searchSymbols(q, 12)
        .then((rows) => {
          if (cancelled) return
          const known: Hit[] = rows.map((r) => ({
            symbol: r.symbol,
            description: r.description,
            exchange: r.exchange,
            known: true,
          }))
          const typed = manualHit(q)
          if (typed && !known.some((k) => k.symbol === typed.symbol && k.exchange === typed.exchange)) {
            known.unshift(typed)
          }
          setHits(known)
          setCursor(0)
        })
        .catch(() => setHits(manualHit(q) ? [manualHit(q)!] : []))
    }, 140)
    return () => {
      cancelled = true
      clearTimeout(t)
    }
  }, [query, open])

  useEffect(() => {
    const hit = hits[cursor]
    if (!hit) return setPreview(null)
    let cancelled = false
    api
      .quote(hit.symbol)
      .then((q) => !cancelled && setPreview({ symbol: q.symbol, last: q.last, change_pct: q.change_pct ?? null }))
      .catch(() => !cancelled && setPreview(null))
    return () => {
      cancelled = true
    }
  }, [hits, cursor])

  const commit = (symbol: string, exchange?: string) => {
    onSelect(symbol.toUpperCase(), exchange?.toUpperCase())
    setOpen(false)
    setQuery('')
  }

  return (
    <div className="symsearch" ref={boxRef}>
      <button className="symsearch-trigger" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
          <circle cx="11" cy="11" r="7" />
          <path d="m20 20-3.5-3.5" strokeLinecap="round" />
        </svg>
        <span className="symsearch-value">{value || 'Search'}</span>
        <kbd>⌘K</kbd>
      </button>

      {open && (
        <div className="symsearch-panel" role="dialog" aria-label="Symbol search">
          <input
            ref={inputRef}
            className="symsearch-input"
            placeholder="Ticker or name…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'ArrowDown') {
                e.preventDefault()
                setCursor((c) => Math.min(c + 1, hits.length - 1))
              } else if (e.key === 'ArrowUp') {
                e.preventDefault()
                setCursor((c) => Math.max(c - 1, 0))
              } else if (e.key === 'Enter' && hits[cursor]) {
                commit(hits[cursor].symbol, hits[cursor].exchange)
              }
            }}
          />

          <div className="symsearch-list">
            {hits.map((h, i) => (
              <button
                key={h.symbol}
                className="symsearch-row"
                aria-selected={i === cursor}
                onMouseEnter={() => setCursor(i)}
                onClick={() => commit(h.symbol, h.exchange)}
              >
                <span className="sym">{h.symbol}</span>
                <span className="desc">{h.description}</span>
                {!h.known && <span className="chip warnchip">unverified</span>}
                {i === cursor && preview?.symbol === h.symbol && (
                  <span className={`quote ${(preview.change_pct ?? 0) >= 0 ? 'pos' : 'neg'}`}>
                    {num(preview.last, 2)}
                    {preview.change_pct != null && ` ${preview.change_pct >= 0 ? '+' : ''}${preview.change_pct.toFixed(2)}%`}
                  </span>
                )}
              </button>
            ))}
            {!hits.length && <div className="symsearch-empty">No matches.</div>}
          </div>

          {hits[cursor] && !hits[cursor].known && (
            <div className="symsearch-warn">
              <strong>{hits[cursor].exchange ? `${hits[cursor].exchange}:` : ''}{hits[cursor].symbol}</strong>{' '}
              will be sent directly to TradingView. App-side analytics use your configured data
              provider, which is currently labelled whenever it is synthetic.
            </div>
          )}
        </div>
      )}
    </div>
  )
}
