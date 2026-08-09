'use client'

import { useEffect, useRef, useState } from 'react'

/**
 * Theme selection.
 *
 * Applied by setting `data-theme` on <html>, which is the only thing any theme
 * changes — every colour in the app resolves through a custom property, so
 * there is no per-component branching and no second stylesheet to keep in step.
 *
 * The swatches are drawn from the live computed values of the theme they
 * represent rather than hard-coded hexes. A swatch that is a copy of the
 * palette is a swatch that will eventually be a stale copy of the palette.
 */

export const THEMES = [
  {
    id: 'dark-blue',
    label: 'Dark blue',
    blurb: 'Near-black with a blue cast. The default.',
  },
  {
    id: 'light-black',
    label: 'Light black',
    blurb: 'Neutral dark grey, a step lighter. Easier in a bright room.',
  },
  {
    id: 'eggshell',
    label: 'Eggshell',
    blurb: 'Warm off-white. Reads as paper rather than as a screen.',
  },
  {
    id: 'glass',
    label: 'Charcoal glass',
    blurb: 'Neutral translucent layers with soft dimensional depth.',
  },
  {
    id: 'midnight',
    label: 'Midnight blue',
    blurb: 'Darker blue-black glass with a quiet cyan-violet atmosphere.',
  },
] as const

export type ThemeId = (typeof THEMES)[number]['id']

const STORAGE_KEY = 'marketshark.theme'

export function applyTheme(id: ThemeId) {
  document.documentElement.setAttribute('data-theme', id)
  try {
    window.localStorage.setItem(STORAGE_KEY, id)
  } catch {
    /* private mode / storage disabled — the theme still applies for this session */
  }
}

export function readStoredTheme(): ThemeId {
  try {
    const v = window.localStorage.getItem(STORAGE_KEY)
    if (THEMES.some((t) => t.id === v)) return v as ThemeId
  } catch {
    /* ignore */
  }
  return 'glass'
}

/** Reads the real token values for a theme by probing a detached element. */
function swatchFor(id: ThemeId): string[] {
  if (typeof document === 'undefined') return []
  const probe = document.createElement('div')
  probe.setAttribute('data-theme', id)
  probe.style.cssText = 'position:absolute;visibility:hidden;pointer-events:none'
  document.body.appendChild(probe)
  const cs = getComputedStyle(probe)
  const tokens = ['--surface-1', '--series-1', '--series-2', '--status-good', '--status-critical']
  const out = tokens.map((t) => cs.getPropertyValue(t).trim()).filter(Boolean)
  probe.remove()
  return out
}

export function Settings() {
  const [open, setOpen] = useState(false)
  const [theme, setTheme] = useState<ThemeId>('glass')
  const [swatches, setSwatches] = useState<Record<string, string[]>>({})
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    setTheme(readStoredTheme())
  }, [])

  useEffect(() => {
    if (!open) return
    setSwatches(Object.fromEntries(THEMES.map((t) => [t.id, swatchFor(t.id)])))
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const pick = (id: ThemeId) => {
    setTheme(id)
    applyTheme(id)
  }

  return (
    <div className="settings" ref={ref}>
      <button
        className="btn ghost"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        title="Settings"
      >
        <svg width="14" height="14" viewBox="0 0 24 24" aria-hidden fill="none"
          stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <circle cx="12" cy="12" r="3" />
          <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1.08-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9c.2.61.78 1.03 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
        </svg>
        <span style={{ marginLeft: 6 }}>Settings</span>
      </button>

      {open && (
        <div className="popover" role="dialog" aria-label="Settings">
          <div className="popover-head">Theme</div>
          <div className="theme-grid">
            {THEMES.map((t) => (
              <button
                key={t.id}
                className="theme-option"
                aria-pressed={theme === t.id}
                onClick={() => pick(t.id)}
              >
                <span className="theme-swatch" data-theme={t.id}>
                  {(swatches[t.id] ?? []).map((c, i) => (
                    <span key={i} style={{ background: c }} />
                  ))}
                </span>
                <span className="theme-meta">
                  <span className="theme-name">{t.label}</span>
                  <span className="theme-blurb">{t.blurb}</span>
                </span>
              </button>
            ))}
          </div>
          <div className="popover-foot">
            Every theme is validated for contrast and colour-vision separation by{' '}
            <code>tools/palette_check.py</code>. Profit and loss differ in lightness as
            well as hue, so the pair survives red-green colour blindness.
          </div>
        </div>
      )}
    </div>
  )
}

/**
 * Applies the saved theme before first paint.
 *
 * This runs as a blocking inline script in <head> on purpose. Setting the
 * attribute from a React effect would paint one frame of the default theme
 * first — a white flash on every navigation for anyone using a dark theme, and
 * the reverse for anyone on eggshell. Rendered via dangerouslySetInnerHTML
 * because that is the only way to emit a synchronous inline script from the
 * app router; the content is a constant with no interpolated input.
 */
export function ThemeScript() {
  const js = `(function(){try{var t=localStorage.getItem(${JSON.stringify(STORAGE_KEY)})||'glass';` +
    `var ok=${JSON.stringify(THEMES.map((t) => t.id))};` +
    `if(ok.indexOf(t)>-1)document.documentElement.setAttribute('data-theme',t);}catch(e){}})()`
  return <script dangerouslySetInnerHTML={{ __html: js }} />
}
