'use client'

import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Live data plumbing.
 *
 * `useLive` re-fetches on an interval and reports four things the UI shows: the
 * data, whether a fetch is in flight, when the last successful one landed, and
 * the last error. All four matter — a "live" badge with no timestamp cannot be
 * distinguished from a badge on a feed that died twenty minutes ago, which is
 * the exact failure it is supposed to rule out.
 *
 * Three behaviours worth stating:
 *
 * - **Polling pauses when the tab is hidden.** A background tab hammering a
 *   rate-limited provider burns the budget that the visible tab needs.
 * - **A failed poll does not clear the data.** The last good values stay on
 *   screen with the error beside them; blanking a table on one dropped request
 *   loses information the user was reading.
 * - **Errors are surfaced, not swallowed.** A silently stalled feed that still
 *   says "live" is worse than a visible error.
 */

export interface LiveState<T> {
  data: T | null
  error: string | null
  loading: boolean
  updatedAt: Date | null
  paused: boolean
  setPaused: (p: boolean) => void
  refresh: () => void
}

export function useLive<T>(
  fetcher: () => Promise<T>,
  deps: unknown[],
  intervalMs = 15000,
): LiveState<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)
  const [paused, setPaused] = useState(false)
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const run = useCallback(async () => {
    setLoading(true)
    try {
      const v = await fetcherRef.current()
      setData(v)
      setUpdatedAt(new Date())
      setError(null)
    } catch (e) {
      // Keep the last good data on screen. A dropped poll is not a reason to
      // blank a table the user is reading.
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    run()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => {
    if (paused || intervalMs <= 0) return
    const id = window.setInterval(() => {
      if (document.visibilityState === 'visible') run()
    }, intervalMs)
    return () => window.clearInterval(id)
  }, [paused, intervalMs, run])

  return { data, error, loading, updatedAt, paused, setPaused, refresh: run }
}

/** Live badge: state, age, and a pause control. */
export function LiveBadge({
  state,
  label = 'live',
}: {
  state: Pick<LiveState<unknown>, 'loading' | 'updatedAt' | 'paused' | 'setPaused' | 'refresh' | 'error'>
  label?: string
}) {
  const [, tick] = useState(0)
  useEffect(() => {
    const id = window.setInterval(() => tick((t) => t + 1), 1000)
    return () => window.clearInterval(id)
  }, [])

  const age = state.updatedAt ? Math.floor((Date.now() - state.updatedAt.getTime()) / 1000) : null

  return (
    <span className="live-badge">
      <button
        className="live-toggle"
        onClick={() => state.setPaused(!state.paused)}
        aria-pressed={!state.paused}
        title={state.paused ? 'Resume auto-refresh' : 'Pause auto-refresh'}
      >
        <span className={`pulse-dot ${state.paused ? 'is-paused' : ''} ${state.error ? 'is-error' : ''}`} aria-hidden />
        {state.paused ? 'paused' : label}
      </button>
      <span className="live-age">
        {state.error ? (
          <span className="neg" title={state.error}>last refresh failed</span>
        ) : age == null ? (
          'loading…'
        ) : age < 2 ? (
          'just now'
        ) : (
          `${age}s ago`
        )}
      </span>
      <button className="live-refresh" onClick={state.refresh} aria-label="Refresh now" disabled={state.loading}>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          strokeWidth="2" strokeLinecap="round" aria-hidden>
          <path d="M20 11a8 8 0 1 0-2.3 5.7M20 5v6h-6" />
        </svg>
      </button>
    </span>
  )
}

/**
 * Compact symbol input for a single card.
 *
 * Sections that are scoped to one name get their own input rather than
 * inheriting only the global one — comparing two symbols side by side is the
 * common case, and a single global symbol makes that impossible.
 */
export function SymbolField({
  value,
  onChange,
  label = 'Symbol',
  width = 92,
}: {
  value: string
  onChange: (s: string) => void
  label?: string
  width?: number
}) {
  const [draft, setDraft] = useState(value)
  useEffect(() => setDraft(value), [value])

  const commit = () => {
    const s = draft.trim().toUpperCase()
    if (s && s !== value) onChange(s)
    else setDraft(value)
  }

  return (
    <input
      className="input sym-field"
      style={{ width }}
      value={draft}
      aria-label={label}
      onChange={(e) => setDraft(e.target.value.toUpperCase())}
      onKeyDown={(e) => {
        if (e.key === 'Enter') commit()
        if (e.key === 'Escape') setDraft(value)
      }}
      onBlur={commit}
    />
  )
}
