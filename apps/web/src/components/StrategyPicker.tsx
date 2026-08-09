'use client'

import { useMemo, useState } from 'react'
import type { Category, TemplateMeta } from '@/lib/types'

interface Props {
  categories: Category[]
  templates: TemplateMeta[]
  selected: string
  onSelect: (id: string) => void
}

export function StrategyPicker({ categories, templates, selected, onSelect }: Props) {
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState<Record<string, boolean>>({})

  const grouped = useMemo(() => {
    const q = query.trim().toLowerCase()
    const filtered = q
      ? templates.filter(
          (t) =>
            t.name.toLowerCase().includes(q) ||
            t.summary.toLowerCase().includes(q) ||
            t.tags.some((tag) => tag.includes(q)),
        )
      : templates
    const map = new Map<string, TemplateMeta[]>()
    for (const t of filtered) {
      const list = map.get(t.category) ?? []
      list.push(t)
      map.set(t.category, list)
    }
    return map
  }, [templates, query])

  const selectedTemplate = templates.find((t) => t.id === selected)

  return (
    <div className="card">
      <div className="card-head">
        <h2>Strategies</h2>
        <span className="sub">{templates.length}</span>
      </div>
      <div style={{ padding: '10px 14px' }}>
        <input
          className="input"
          type="search"
          placeholder="Search condor, wheel, LEAPS…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Search strategies"
        />
      </div>
      <div style={{ maxHeight: 480, overflowY: 'auto' }}>
        {categories.map((cat) => {
          const list = grouped.get(cat.id)
          if (!list?.length) return null
          const isOpen =
            open[cat.id] ?? (!!query || list.some((t) => t.id === selected) || cat.id === selectedTemplate?.category)
          return (
            <div className="cat-group" key={cat.id}>
              <button
                className="cat-head"
                onClick={() => setOpen((o) => ({ ...o, [cat.id]: !isOpen }))}
                aria-expanded={isOpen}
              >
                <span>{cat.label}</span>
                <span>{isOpen ? '−' : `+${list.length}`}</span>
              </button>
              {isOpen &&
                list.map((t) => (
                  <button
                    className="strat"
                    key={t.id}
                    aria-current={t.id === selected}
                    onClick={() => onSelect(t.id)}
                  >
                    <span className="name">
                      {t.name}
                      {!t.risk_defined && (
                        <span
                          className="tiny"
                          style={{ color: 'var(--status-serious)', marginLeft: 6 }}
                          title="Undefined risk"
                        >
                          ⚠ undefined risk
                        </span>
                      )}
                    </span>
                    <span className="desc">{t.summary}</span>
                  </button>
                ))}
            </div>
          )
        })}
      </div>
    </div>
  )
}
