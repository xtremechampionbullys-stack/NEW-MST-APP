'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { usePathname, useRouter } from 'next/navigation'
import { Settings } from '@/components/Settings'
import { SymbolSearch } from '@/components/SymbolSearch'
import { api } from '@/lib/api'

/**
 * The workspace shell.
 *
 * Modelled on the Codex desktop chrome, and the governing principle there is
 * restraint: flat surfaces, one type weight, 30px rows, no pills, no halos, a
 * grouped rail with small grey section labels, and an account block at the
 * bottom.
 *
 * The previous version had a glowing active item and ambient colour washes.
 * That is what made it read as amateur — decoration competing with the data.
 * Professional application chrome is quiet and gets out of the way; the charts
 * are what should carry visual weight. So the active item is now a 2px bar and
 * a slightly lighter row, and nothing else.
 */

const GROUPS: { label: string | null; items: { href: string; label: string; icon: string }[] }[] = [
  {
    label: null,
    items: [
      { href: '/?workspace=1', label: 'New chat', icon: 'M4 5h16v11H9l-5 4z' },
    ],
  },
  {
    label: 'Workspace',
    items: [
      { href: '/lab', label: 'Strategy Lab', icon: 'M5 19h14M7 19V9m5 10V5m5 14v-7' },
      { href: '/charting', label: 'Charting', icon: 'M4 20V4m0 16h16M8 16l4-6 3 3 5-7' },
      { href: '/portfolio', label: 'Portfolio', icon: 'M4 8h16v11H4zM9 8V5h6v3M4 13h16' },
      { href: '/algo', label: 'Algo Gateway', icon: 'M13 2 4.5 12.5h5L11 22l8.5-10.5h-5z' },
      { href: '/dashboard', label: 'Dashboard', icon: 'M4 13h5l2 6 3-14 2 8h4' },
    ],
  },
  {
    label: 'Analysis',
    items: [
      { href: '/news', label: 'News Desk', icon: 'M5 5h14v14H5zM8 9h8M8 12h8M8 15h5' },
      { href: '/research', label: 'Research', icon: 'M10.5 4a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13zM20 20l-4.9-4.9' },
      { href: '/backtest', label: 'Backtest', icon: 'M4 4v16h16M8 15l3-4 3 2 4-6' },
      { href: '/harness', label: 'Trading Harness', icon: 'M12 3v18M5 8l7-5 7 5M5 16l7 5 7-5M3 12h18' },
    ],
  },
]

const COLLAPSE_KEY = 'marketshark.rail'

export function Shell({
  children,
  symbol,
  onSymbol,
  actions,
  wide = true,
}: {
  children: React.ReactNode
  symbol?: string
  onSymbol?: (s: string) => void
  actions?: React.ReactNode
  /** false centres the content column at the reading width. */
  wide?: boolean
}) {
  const path = usePathname()
  const router = useRouter()
  const [collapsed, setCollapsed] = useState(false)
  const [dataStatus, setDataStatus] = useState<{
    mode: 'live' | 'demo'; live_ready: boolean; message: string
  } | null>(null)

  useEffect(() => {
    try {
      setCollapsed(window.localStorage.getItem(COLLAPSE_KEY) === '1')
    } catch {
      /* storage disabled */
    }
  }, [])

  useEffect(() => {
    let active = true
    api.marketProviders().then((status) => {
      if (active) setDataStatus(status)
    }).catch(() => {
      // The rest of the shell remains usable if the API is starting up.
    })
    return () => { active = false }
  }, [])

  const toggle = () => {
    setCollapsed((v) => {
      const next = !v
      try {
        window.localStorage.setItem(COLLAPSE_KEY, next ? '1' : '0')
      } catch {
        /* storage disabled */
      }
      return next
    })
  }

  const pickSymbol = (s: string) => {
    onSymbol?.(s)
    // Mirrored into the URL so a view is linkable and survives a reload.
    const url = new URL(window.location.href)
    url.searchParams.set('symbol', s)
    router.replace(url.pathname + url.search, { scroll: false })
  }

  return (
    <div className={`shell-root ${collapsed ? 'is-collapsed' : ''}`}>
      {/*
        * One header band across the whole window.
        *
        * It used to be two elements — a head inside the rail and a top bar over
        * the content — sharing a height and a bottom divider. That is not the
        * same as one piece: the rail's right border ran straight up through it,
        * cutting the band in two at the exact place the eye reads first. The
        * header is now a single grid cell spanning both columns, and the
        * vertical divider starts underneath it.
        */}
      <header className="app-header">
        <div className="header-brand">
          <button className="rail-title" onClick={toggle}
            title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}>
            <svg className="brand-mark" viewBox="0 0 24 24" aria-hidden>
              <path d="M2 15c4-1 6-4 8-8 1.6 3.2 3.4 5 6 6l6-4-3 8c-3.4 2.6-9 3-13 1l-4-3z"
                fill="var(--accent-brand)" />
            </svg>
            <span className="rail-label">MarketShark</span>
            <svg className="rail-label rail-caret" width="10" height="10" viewBox="0 0 24 24"
              fill="none" stroke="currentColor" strokeWidth="3" aria-hidden>
              <path d="m6 9 6 6 6-6" />
            </svg>
          </button>
        </div>

        <div className="header-bar">
          {onSymbol && <SymbolSearch value={symbol} onSelect={pickSymbol} />}
          <div className="topbar-actions">{actions}</div>
          <Settings />
        </div>
      </header>

      <aside className="rail-nav">
        <nav className="rail-groups">
          {GROUPS.map((g, gi) => (
            <div className="rail-group" key={g.label ?? `g${gi}`}>
              {g.label && <div className="rail-group-label rail-label">{g.label}</div>}
              {g.items.map((n) => (
                <Link
                  key={n.href}
                  href={n.href}
                  className={`rail-link ${n.href === '/' ? 'rail-link-new' : ''}`}
                  aria-current={path === n.href ? 'page' : undefined}
                  title={n.label}
                >
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                    strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                    <path d={n.icon} />
                  </svg>
                  <span className="rail-label">{n.label}</span>
                </Link>
              ))}
            </div>
          ))}
        </nav>

        <div className="rail-foot">
          <div className="rail-account">
            <span className="avatar" aria-hidden>MS</span>
            <span className="rail-label account-name">
              {dataStatus?.mode === 'live' && dataStatus.live_ready ? 'Live market data' :
                dataStatus?.mode === 'live' ? 'Live setup required' :
                  dataStatus?.mode === 'demo' ? 'Demo data' : 'Checking data…'}
              <small>{dataStatus?.mode === 'live' && dataStatus.live_ready
                ? 'provider connected'
                : dataStatus?.mode === 'live'
                  ? 'add a licensed data provider'
                  : dataStatus?.mode === 'demo'
                    ? 'generated, not market data'
                    : 'connecting to API'}</small>
            </span>
          </div>
        </div>
      </aside>

      <div className="shell-main">
        {dataStatus?.mode === 'live' && !dataStatus.live_ready && (
          <div className="live-setup-bar" role="status">
            <strong>Live market data is not connected.</strong>
            <span>{dataStatus.message}</span>
          </div>
        )}
        <main className={wide ? 'workspace' : 'workspace is-reading'}>{children}</main>
      </div>
    </div>
  )
}

/** Reads `?symbol=` on mount so a shared link opens on the right name. */
export function useUrlSymbol(fallback: string) {
  const [symbol, setSymbol] = useState(fallback)
  useEffect(() => {
    const q = new URLSearchParams(window.location.search).get('symbol')
    if (q) setSymbol(q.toUpperCase())
  }, [])
  return [symbol, setSymbol] as const
}
