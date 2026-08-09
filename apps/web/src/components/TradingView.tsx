'use client'

import { useEffect, useRef, useState } from 'react'

/**
 * TradingView embed widgets.
 *
 * One component for all of them, because they share an identical contract: a
 * `<script>` whose body is a JSON config, appended inside a container div. The
 * differences are the script filename and the config keys. The Advanced Chart
 * follows this current public embedding path too; the older `tv.js` global
 * constructor is unreliable across React's mount/unmount lifecycle.
 *
 * On what TradingView does and does not offer for options — this matters,
 * because "TradingView options features" is a reasonable thing to ask for and
 * an unreasonable thing to promise. TradingView's public embed library covers
 * charts, quotes, screeners, calendars, technicals and news. **There is no
 * embeddable options-chain or options-greeks widget.** Options chains exist
 * inside tradingview.com's own platform, not in the widget set anyone can drop
 * into a page. So the options surface on the Charting page is ours — the chain,
 * the greeks and the payoff come from this app's engine, sitting next to
 * TradingView's chart rather than pretending to come from it.
 *
 * Every widget reports load failure rather than leaving a grey rectangle: these
 * are third-party scripts, and a blank panel is indistinguishable from a broken
 * page.
 */

/*
 * The widget catalogue.
 *
 * Every filename below was verified by fetching it from the CDN, not recalled.
 * That mattered: six plausible-looking names return 404 —
 * `embed-widget-company-profile.js` (the Company Profile widget is actually
 * served by `symbol-profile`), `embed-widget-top-stories.js` (that is
 * `timeline`), `embed-widget-market-data.js`, `embed-widget-ticker-tag.js`,
 * `embed-widget-forex-heatmap.js` and `embed-widget-crypto-mkt-screener.js`.
 * A guessed filename fails silently at runtime as an empty panel, which is
 * exactly the failure mode this file exists to prevent, so guessing was not an
 * option.
 */
export type WidgetKind =
  // charts
  | 'advanced-chart'
  | 'symbol-overview'
  | 'mini-symbol-overview'
  // watchlists / quotes
  | 'market-overview'
  | 'market-quotes'
  | 'hotlists'
  | 'ticker-tape'
  | 'tickers'
  | 'single-quote'
  // symbol detail
  | 'symbol-info'
  | 'technical-analysis'
  | 'financials'
  | 'symbol-profile'
  // screeners, heatmaps, calendars, news
  | 'screener'
  | 'stock-heatmap'
  | 'etf-heatmap'
  | 'crypto-coins-heatmap'
  | 'forex-cross-rates'
  | 'events'
  | 'timeline'

const SCRIPT: Record<WidgetKind, string> = {
  'advanced-chart': 'embed-widget-advanced-chart.js',
  'symbol-overview': 'embed-widget-symbol-overview.js',
  'mini-symbol-overview': 'embed-widget-mini-symbol-overview.js',
  'market-overview': 'embed-widget-market-overview.js',
  'market-quotes': 'embed-widget-market-quotes.js',
  hotlists: 'embed-widget-hotlists.js',
  'ticker-tape': 'embed-widget-ticker-tape.js',
  tickers: 'embed-widget-tickers.js',
  'single-quote': 'embed-widget-single-quote.js',
  'symbol-info': 'embed-widget-symbol-info.js',
  'technical-analysis': 'embed-widget-technical-analysis.js',
  financials: 'embed-widget-financials.js',
  'symbol-profile': 'embed-widget-symbol-profile.js',
  screener: 'embed-widget-screener.js',
  'stock-heatmap': 'embed-widget-stock-heatmap.js',
  'etf-heatmap': 'embed-widget-etf-heatmap.js',
  'crypto-coins-heatmap': 'embed-widget-crypto-coins-heatmap.js',
  'forex-cross-rates': 'embed-widget-forex-cross-rates.js',
  events: 'embed-widget-events.js',
  timeline: 'embed-widget-timeline.js',
}

export function widgetTheme(): 'light' | 'dark' {
  if (typeof document === 'undefined') return 'dark'
  return document.documentElement.getAttribute('data-theme') === 'eggshell' ? 'light' : 'dark'
}

/**
 * Tracks the app theme so widgets can be rebuilt when it changes.
 *
 * The embed bakes its theme into the config at construction and exposes no way
 * to change it afterwards, so a widget mounted in dark mode stays dark forever
 * — a black rectangle sitting in the middle of the eggshell theme. The only
 * fix available is to tear the widget down and build a new one, which is what
 * including this value in the effect's dependencies does.
 */
export function useWidgetTheme(): 'light' | 'dark' {
  const [theme, setTheme] = useState<'light' | 'dark'>('dark')
  useEffect(() => {
    const read = () => setTheme(widgetTheme())
    read()
    const obs = new MutationObserver(read)
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => obs.disconnect()
  }, [])
  return theme
}

export function TvWidget({
  kind,
  config,
  height = 400,
  label,
  className,
}: {
  kind: WidgetKind
  config: Record<string, unknown>
  height?: number | string
  label?: string
  /** Applies the MarketShark frame without reaching inside TradingView's iframe. */
  className?: string
}) {
  const host = useRef<HTMLDivElement>(null)
  const theme = useWidgetTheme()
  // Three states, not two. The old version had only `blocked`, which meant the
  // six seconds before the timeout fired showed an unlabelled void — visually
  // identical to the failure it was about to report. A void that turns into an
  // error after six seconds reads as a hang; a "loading" line that turns into
  // an error reads as a load that failed.
  const [state, setState] = useState<'loading' | 'ready' | 'blocked'>('loading')
  const [attempt, setAttempt] = useState(0)
  const key = JSON.stringify(config)

  useEffect(() => {
    const el = host.current
    if (!el) return
    el.innerHTML = ''
    setState('loading')

    const container = document.createElement('div')
    container.className = 'tradingview-widget-container'
    container.style.height = '100%'
    container.style.width = '100%'
    const inner = document.createElement('div')
    inner.className = 'tradingview-widget-container__widget'
    // TradingView reserves a small attribution line beneath its iframe.
    inner.style.height = 'calc(100% - 32px)'
    inner.style.width = '100%'
    container.appendChild(inner)

    // Third-party script: it can be blocked by a network policy, an ad
    // blocker, or simply be offline. `script.onload` fires when the file
    // arrives, but the widget builds its iframe afterwards — so the iframe,
    // not the load event, is what proves the thing actually rendered. Poll for
    // it. Fifteen seconds allows for a cold third-party CDN fetch before we
    // expose a retry rather than misclassifying a slow but healthy load.
    let settled = false
    const poll = window.setInterval(() => {
      if (settled) return
      if (inner.querySelector('iframe')) {
        settled = true
        window.clearInterval(poll)
        setState('ready')
      }
    }, 250)
    const timer = window.setTimeout(() => {
      if (settled) return
      settled = true
      window.clearInterval(poll)
      setState(inner.querySelector('iframe') ? 'ready' : 'blocked')
    }, 15000)
    el.appendChild(container)
    const script = document.createElement('script')
    script.src = `https://s3.tradingview.com/external-embedding/${SCRIPT[kind]}`
    script.async = true
    script.type = 'text/javascript'
    script.innerHTML = JSON.stringify({ ...config, theme, locale: 'en' })
    script.onerror = () => {
      settled = true
      window.clearInterval(poll)
      setState('blocked')
    }
    container.appendChild(script)
    return () => {
      window.clearTimeout(timer)
      window.clearInterval(poll)
      el.innerHTML = ''
    }
  }, [kind, key, theme, attempt])

  return (
    <div className={`tv-widget${className ? ` ${className}` : ''}`} style={{ position: 'relative', height }}>
      {/* The host stays mounted in every state. Unmounting it on failure would
        * destroy the container the poll is watching, and remounting on retry
        * would re-run the effect from scratch — so the overlay sits on top
        * instead of replacing it. */}
      <div ref={host} style={{ height: '100%' }} />
      {state !== 'ready' && (
        <div className="tv-overlay">
          {state === 'loading' ? (
            <span className="tiny muted">
              <span className="pulse-dot" /> loading {label ?? 'TradingView widget'} from
              s3.tradingview.com…
            </span>
          ) : typeof height === 'number' && height < 140 ? (
            // A short widget — the tape strip is 78px — cannot fit the full
            // explanation, and a paragraph clipped mid-sentence is worse than a
            // line that fits. Say the essential thing on one line instead.
            <span className="tiny muted">
              {label ?? 'This widget'} did not load — <code>s3.tradingview.com</code> is
              unreachable from this network.{' '}
              <button className="btn ghost tiny" type="button" onClick={() => setAttempt((n) => n + 1)}>
                Retry
              </button>
            </span>
          ) : (
            <div className="empty-state">
              <strong>{label ?? 'TradingView widget'} did not load.</strong>
              <p>
                It is fetched from <code>s3.tradingview.com</code> at runtime, so it needs a
                context that allows third-party scripts. The usual causes, in order:
                you are viewing this inside a sandboxed preview pane (chat previews block
                 external scripts — open the app in a regular browser tab and it renders),
                 an ad blocker or network policy is filtering the TradingView domain, or you
                 are offline. Nothing here caches it, and every other panel keeps working.
               </p>
              <button className="btn" type="button" onClick={() => setAttempt((n) => n + 1)}>
                Retry widget
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * A native MarketShark card around a TradingView embed.
 *
 * TradingView owns the data inside the iframe; this keeps the surrounding
 * hierarchy, spacing, source label and liquid-glass treatment consistently
 * ours. It also makes the third-party data source explicit rather than making
 * it look like a quote generated by the strategy engine.
 */
export function TvPanel({
  title,
  subtitle,
  kind,
  config,
  height = 440,
  label,
  className,
}: {
  title: string
  subtitle: string
  kind: WidgetKind
  config: Record<string, unknown>
  height?: number | string
  label?: string
  className?: string
}) {
  return (
    <section className={`card tv-panel${className ? ` ${className}` : ''}`}>
      <div className="card-head">
        <div>
          <h2>{title}</h2>
          <span className="sub">{subtitle}</span>
        </div>
        <span className="tv-source"><i aria-hidden /> TradingView live</span>
      </div>
      <div className="card-body flush">
        <TvWidget kind={kind} config={config} height={height} label={label ?? title} />
      </div>
    </section>
  )
}

/** The scrolling quote strip TradingView ships. Live, and clearly theirs. */
export function TickerTape({ symbols }: { symbols: { proName: string; title: string }[] }) {
  return (
    <TvWidget
      kind="ticker-tape"
      height={78}
      label="Ticker tape"
      config={{
        symbols,
        showSymbolLogo: true,
        isTransparent: true,
        displayMode: 'adaptive',
        colorTheme: widgetTheme(),
      }}
    />
  )
}
