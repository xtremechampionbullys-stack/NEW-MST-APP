'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'

const LINKS = [
  { href: '/', label: 'Strategy Lab' },
  { href: '/dashboard', label: 'Dashboard' },
  { href: '/backtest', label: 'Backtest' },
  { href: '/research', label: 'Research' },
  { href: '/charting', label: 'Charting' },
  { href: '/footprint', label: 'Order Flow' },
]

export function Nav() {
  const path = usePathname()
  return (
    <nav className="nav">
      {LINKS.map((l) => (
        <Link key={l.href} href={l.href} aria-current={path === l.href ? 'page' : undefined}>
          {l.label}
        </Link>
      ))}
    </nav>
  )
}

export function Brand() {
  return (
    <div className="brand">
      <svg className="brand-mark" viewBox="0 0 24 24" aria-hidden>
        <path d="M2 15c4-1 6-4 8-8 1.6 3.2 3.4 5 6 6l6-4-3 8c-3.4 2.6-9 3-13 1l-4-3z" fill="var(--series-1)" />
        <circle cx="16.5" cy="8.5" r="1.1" fill="var(--surface-1)" />
      </svg>
      <div>
        MarketShark Trader
        <small>Options Strategy Lab</small>
      </div>
    </div>
  )
}
