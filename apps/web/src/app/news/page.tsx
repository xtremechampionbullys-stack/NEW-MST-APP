'use client'

import { useMemo, useState } from 'react'
import { Shell } from '@/components/Shell'
import { LiveBadge, useLive } from '@/components/live'
import { TickerTape, TvPanel } from '@/components/TradingView'
import { api } from '@/lib/api'
import type { NewsFeedResponse, RankedNewsItem } from '@/lib/types'

const DEFAULT_WATCHLIST = 'SPY,QQQ,NVDA,AAPL,TSLA,AMD,META,COIN'

const BROADCASTS = [
  { name: 'Bloomberg Television', label: 'Business & markets', embed: 'https://www.youtube-nocookie.com/embed/iyOq8DhaMYw?rel=0', href: 'https://www.bloomberg.com/live/stream' },
  { name: 'TopstepTV', label: 'Futures desk', embed: 'https://www.youtube-nocookie.com/embed/live_stream?channel=UCTDYMpdFhZU6Ivf3F2AEVUQ&rel=0', href: 'https://www.topstep.tv/' },
]

function tradingViewSymbol(symbol: string) {
  const exchange: Record<string, string> = { SPY: 'AMEX', QQQ: 'NASDAQ', DIA: 'AMEX', IWM: 'AMEX', VIX: 'CBOE' }
  return `${exchange[symbol] ?? 'NASDAQ'}:${symbol}`
}

function isMacro(item: RankedNewsItem) {
  return /fed|fomc|cpi|inflation|jobs|payroll|gdp|treasury|yield|rate|central bank|dollar|economic/i.test(`${item.title} ${item.summary}`)
}

function isFutures(item: RankedNewsItem) {
  return /futures|s&p|nasdaq|russell|dow|oil|crude|gold|vix|treasury|yield|commodity/i.test(`${item.title} ${item.summary}`)
}

export default function NewsDeskPage() {
  const [watchlist, setWatchlist] = useState(DEFAULT_WATCHLIST)
  const [draft, setDraft] = useState(DEFAULT_WATCHLIST)
  const symbols = watchlist.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean)
  const feed = useLive<NewsFeedResponse>(() => api.watchlistNews(symbols, 60), [watchlist], 60_000)
  const items = feed.data?.items ?? []
  const macro = useMemo(() => items.filter(isMacro), [items])
  const futures = useMemo(() => items.filter(isFutures), [items])
  const breaking = useMemo(() => items.filter((item) => ['high', 'elevated'].includes(item.scoring.band)), [items])
  const attention = useMemo(() => {
    const counts = new Map<string, number>()
    for (const item of items) for (const ticker of item.tickers.length ? item.tickers : [item.symbol]) if (ticker) counts.set(ticker, (counts.get(ticker) ?? 0) + 1)
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8)
  }, [items])

  return <Shell actions={<form className="row news-symbol-form" onSubmit={(event) => { event.preventDefault(); setWatchlist(draft) }}>
    <input className="input" value={draft} onChange={(event) => setDraft(event.target.value.toUpperCase())} aria-label="News watchlist symbols" />
    <button className="btn" type="submit">Update desk</button>
  </form>}>
    <div className="page news-desk">
      <header className="news-hero">
        <div><span className="eyebrow">MarketShark live desk</span><h1 className="type-heading-lg">News, broadcasts &amp; market attention</h1><p className="type-body">One place for the live market conversation. Article impact is measured against price and volume after publication; broadcast and social sources remain visibly separate.</p></div>
        <LiveBadge state={feed} label="news feed" />
      </header>
      <section className="card tv-tape-panel" aria-label="Live watchlist ticker tape">
        <div className="card-head"><div><h2>Watchlist pulse</h2><span className="sub">live exchange context for the symbols on this desk</span></div><span className="tv-source"><i aria-hidden /> TradingView live</span></div>
        <div className="card-body flush"><TickerTape symbols={symbols.map((symbol) => ({ proName: tradingViewSymbol(symbol), title: symbol }))} /></div>
      </section>
      <section className="news-broadcasts" aria-label="Live market broadcasts">
        {BROADCASTS.map((broadcast) => <article className="broadcast-card" key={broadcast.name}>
          <div className="broadcast-title"><div><span className="pulse-dot" aria-hidden /> <strong>{broadcast.name}</strong><span>{broadcast.label}</span></div><a href={broadcast.href} target="_blank" rel="noreferrer">Open source ↗</a></div>
          <div className="broadcast-frame"><iframe src={broadcast.embed} title={`${broadcast.name} live broadcast`} allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowFullScreen /></div>
        </article>)}
      </section>
      {feed.error && <div className="err">News feed refresh failed: {feed.error}</div>}
      <section className="news-grid-primary">
        <NewsPanel title="Breaking market impact" subtitle="High and elevated measured reaction" items={breaking} empty="No articles have a completed high-impact reaction measurement yet." />
        <NewsPanel title="Watchlist flow" subtitle={symbols.join(' · ')} items={items} empty="No watchlist articles are available from the configured provider." />
      </section>
      <section className="news-grid-secondary">
        <NewsPanel title="Economics & policy" subtitle="Macro-sensitive headlines from the watchlist feed" items={macro} empty="No macro keywords in the current feed." compact />
        <NewsPanel title="Futures & cross-asset" subtitle="Index, rates, energy, metals and volatility" items={futures} empty="No futures or cross-asset keywords in the current feed." compact />
        <aside className="card attention-card"><div className="card-head"><div><h2>Ticker attention</h2><span className="sub">Headline mentions today · not social posts</span></div></div><div className="attention-list">
          {attention.length ? attention.map(([symbol, count], index) => <div className="attention-row" key={symbol}><span className="attention-rank">{index + 1}</span><strong>{symbol}</strong><div className="attention-meter"><i style={{ width: `${Math.max(12, (count / attention[0][1]) * 100)}%` }} /></div><span>{count}</span></div>) : <p className="empty-copy">Waiting for article coverage.</p>}
        </div><div className="social-note"><strong>Social feed:</strong> not connected. This panel intentionally reports publisher-headline attention only; connect a licensed Reddit/X/Stocktwits source before labelling it social volume.</div></aside>
      </section>
      <section className="grid-2 tv-news-grid" aria-label="Live market calendar and symbol news">
        <TvPanel title={`${symbols[0] ?? 'SPY'} live news`} label="Symbol news timeline"
          subtitle="TradingView publisher headlines for the first symbol in your desk"
          kind="timeline" height={470}
          config={{ feedMode: 'symbol', symbol: tradingViewSymbol(symbols[0] ?? 'SPY'), isTransparent: true, displayMode: 'regular', width: '100%', height: 470 }} />
        <TvPanel title="Economic calendar" label="Economic calendar"
          subtitle="scheduled releases and central-bank events that can move a whole book"
          kind="events" height={470}
          config={{ isTransparent: true, width: '100%', height: 470, importanceFilter: '0,1', countryFilter: 'us,eu,gb,jp' }} />
      </section>
      <p className="news-method type-caption">{feed.data?.method ?? 'Impact scores appear after a completed reaction window. New headlines are shown, but are not treated as low impact simply because they are new.'}</p>
    </div>
  </Shell>
}

function NewsPanel({ title, subtitle, items, empty, compact = false }: { title: string; subtitle: string; items: RankedNewsItem[]; empty: string; compact?: boolean }) {
  return <section className={`card desk-panel ${compact ? 'compact' : ''}`}><div className="card-head"><div><h2>{title}</h2><span className="sub">{subtitle}</span></div><span className="chip">{items.length}</span></div><div className="desk-feed">{items.slice(0, compact ? 5 : 8).map((item) => <NewsItem item={item} key={item.id} />)}{!items.length && <p className="empty-copy">{empty}</p>}</div></section>
}

function NewsItem({ item }: { item: RankedNewsItem }) {
  const score = item.scoring.impact
  return <article className="desk-story"><div className="story-meta"><span className={`impact-dot ${item.scoring.band}`} /> <span>{item.symbol || item.tickers[0] || 'Market'}</span><span>{item.publisher}</span><span>{new Date(item.published).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</span></div><a href={item.url} target="_blank" rel="noreferrer">{item.title}</a>{item.summary && <p>{item.summary}</p>}<div className="story-foot"><span>{score == null ? 'reaction pending' : `${item.scoring.band} impact · ${score.toFixed(0)}`}</span>{item.media_type === 'video' && <span>video</span>}</div></article>
}
