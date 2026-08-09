'use client'

import { ChangeEvent, KeyboardEvent, useEffect, useRef, useState } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { MarketStrip } from '@/components/MarketStrip'
import { Shell, useUrlSymbol } from '@/components/Shell'
import { api } from '@/lib/api'
import type { AgentCapabilities, AgentTurn } from '@/lib/types'

/**
 * The agent workspace.
 *
 * Three things this page does that a chat UI usually does not, all for the same
 * reason — an agent that claims work it did not do is worse than no agent:
 *
 * 1. **Tool calls are shown.** Every turn lists what actually ran, with
 *    arguments. If the reply says a backtest happened, the call is on screen.
 * 2. **Actions are performed, not described.** A navigate action moves the
 *    workspace; an open_position action carries the symbol, template and
 *    params into the Strategy Lab.
 * 3. **Non-matches are visible.** When the router does not understand, the turn
 *    is marked as such rather than being dressed up as an answer.
 */

/*
 * Entry cards. Each one sends a real utterance the router matches — no card is
 * decorative, and none of them opens a menu that then does nothing. The colour
 * on the icon is categorical only; the label carries the meaning.
 */
const CARDS = [
  {
    label: 'Build a position',
    hint: 'price it, see the greeks',
    send: 'set up a 16 delta iron condor on NVDA 45 dte',
    color: 'var(--series-1)',
    icon: 'M4 18h4V9H4zm6 0h4V4h-4zm6 0h4v-6h-4z',
  },
  {
    label: 'Suggest a structure',
    hint: 'ranked against the surface',
    send: 'what should I trade on AAPL if it goes nowhere',
    color: '#a06cf0',
    icon: 'M12 3v3m0 12v3M3 12h3m12 0h3M5.6 5.6l2.1 2.1m8.6 8.6 2.1 2.1m0-12.8-2.1 2.1m-8.6 8.6-2.1 2.1',
  },
  {
    label: 'Test it on history',
    hint: 'rules replay with the caveats',
    send: 'backtest a short strangle on SPY',
    color: 'var(--accent-brand)',
    icon: 'M3 3v18h18M7 15l4-5 3 3 5-7',
  },
  {
    label: 'Check the models',
    hint: 'and their measured skill',
    send: 'which way is NVDA going',
    color: 'var(--status-warning)',
    icon: 'M12 20v-6m0 0V4m0 10 4-4m-4 4-4-4',
  },
]

const SUGGESTIONS = [
  'when does AAPL report',
  'run the dojo on NVDA',
  'volatility forecast for TSLA',
  'simulate this forward on NVDA',
]

interface Message {
  role: 'user' | 'agent'
  text: string
  turn?: AgentTurn
  attachments?: string[]
}

interface TextAttachment {
  name: string
  content: string
}

type SpeechRecognitionInstance = {
  continuous: boolean
  interimResults: boolean
  lang: string
  start: () => void
  stop: () => void
  onresult: ((event: { results: ArrayLike<{ 0: { transcript: string } }> }) => void) | null
  onend: (() => void) | null
  onerror: (() => void) | null
}

type SpeechRecognitionConstructor = new () => SpeechRecognitionInstance

const TEXT_ATTACHMENT = /\.(txt|md|markdown|csv|json)$/i
const MAX_ATTACHMENT_BYTES = 16_000

function AgentWorkspace() {
  const [symbol, setSymbol] = useUrlSymbol('NVDA')
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [autoRun, setAutoRun] = useState(true)
  const [caps, setCaps] = useState<AgentCapabilities | null>(null)
  const [model, setModel] = useState('')
  const [attachments, setAttachments] = useState<TextAttachment[]>([])
  const [attachmentError, setAttachmentError] = useState('')
  const [listening, setListening] = useState(false)
  const [toolsOpen, setToolsOpen] = useState(false)
  const streamRef = useRef<HTMLDivElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const recognitionRef = useRef<SpeechRecognitionInstance | null>(null)
  const router = useRouter()

  useEffect(() => {
    api.agentCapabilities().then((next) => {
      setCaps(next)
      setModel(next.models?.[0] ?? '')
    }).catch(() => {})
  }, [])

  useEffect(() => {
    streamRef.current?.scrollTo({ top: streamRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, busy])

  useEffect(() => () => recognitionRef.current?.stop(), [])

  const attachFiles = async (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? [])
    event.target.value = ''
    if (!files.length) return

    const invalid = files.find((file) => !TEXT_ATTACHMENT.test(file.name) || file.size > MAX_ATTACHMENT_BYTES)
    if (invalid) {
      setAttachmentError('Attach text, Markdown, CSV, or JSON files up to 16 KB.')
      return
    }

    try {
      const next = await Promise.all(files.map(async (file) => ({
        name: file.name,
        content: (await file.text()).slice(0, MAX_ATTACHMENT_BYTES),
      })))
      setAttachments((current) => [...current, ...next].slice(0, 4))
      setAttachmentError('')
    } catch {
      setAttachmentError('That attachment could not be read in this browser.')
    }
  }

  const toggleVoice = () => {
    if (listening) {
      recognitionRef.current?.stop()
      return
    }
    const speechWindow = window as Window & {
      SpeechRecognition?: SpeechRecognitionConstructor
      webkitSpeechRecognition?: SpeechRecognitionConstructor
    }
    const ctor = (speechWindow.SpeechRecognition ?? speechWindow.webkitSpeechRecognition) as
      | SpeechRecognitionConstructor
      | undefined
    if (!ctor) {
      setAttachmentError('Voice dictation is not available in this browser.')
      return
    }
    const recognition = new ctor()
    recognition.continuous = false
    recognition.interimResults = false
    recognition.lang = 'en-US'
    recognition.onresult = (event) => {
      const transcript = Array.from(event.results).map((result) => result[0].transcript).join(' ')
      setInput((value) => `${value}${value && !value.endsWith(' ') ? ' ' : ''}${transcript}`)
    }
    recognition.onend = () => setListening(false)
    recognition.onerror = () => {
      setListening(false)
      setAttachmentError('Voice dictation ended before a transcript was available.')
    }
    recognitionRef.current = recognition
    setAttachmentError('')
    setListening(true)
    recognition.start()
  }

  const send = async (text: string) => {
    const msg = text.trim()
    if ((!msg && attachments.length === 0) || busy) return
    const names = attachments.map((attachment) => attachment.name)
    const displayed = msg || `Review ${names.join(', ')}.`
    const attachmentContext = attachments
      .map((attachment) => `\n\n[Attached file: ${attachment.name}]\n${attachment.content}`)
      .join('')
    setMessages((m) => [...m, { role: 'user', text: displayed, attachments: names }])
    setInput('')
    setAttachments([])
    setBusy(true)
    try {
      const turn = await api.agentChat({ message: `${displayed}${attachmentContext}`, symbol,
        session_id: sessionId ?? undefined, model: model || undefined })
      setMessages((m) => [...m, { role: 'agent', text: turn.reply, turn }])
      if (turn.session_id) setSessionId(turn.session_id)
      if (turn.symbol) setSymbol(turn.symbol)

      // Perform the actions rather than reporting them. With auto-run off the
      // action still appears on the turn as a button — the agent never does
      // something the user turned off, and never hides that it wanted to.
      if (autoRun) {
        for (const a of turn.actions ?? []) {
          if (a.type === 'navigate' && a.to) {
            router.push(a.to + (a.symbol ? `?symbol=${a.symbol}` : ''))
          }
        }
      }
    } catch (e) {
      setMessages((m) => [
        ...m,
        { role: 'agent', text: `That request failed: ${e instanceof Error ? e.message : String(e)}` },
      ])
    } finally {
      setBusy(false)
    }
  }

  const onInputKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      send(input)
    }
  }

  return (
    <Shell symbol={symbol} onSymbol={setSymbol} wide>
      <section className="agent-workspace">
      <div className="agent-stream" ref={streamRef}>
        {messages.length === 0 && (
          <div className="agent-intro">
            <MarketStrip />
            <h1 className="type-heading-lg">What do you want to look at?</h1>
            <p className="type-body">
              I run the same engines the rest of the workspace does and show you the
              real output — not a description of it. Every turn lists the tools that
              actually ran.
            </p>
            {caps && (
              <p className="type-caption agent-engine">
                <span className="pulse-dot" aria-hidden /> {caps.engine} engine
                {caps.model_configured ? ' · model key configured' : ' · no model key needed'}
                {' · '}cannot place live orders
              </p>
            )}
            <div className="entry-cards">
              {CARDS.map((c) => (
                <button key={c.label} className="entry-card" onClick={() => send(c.send)}>
                  <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke={c.color}
                    strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                    <path d={c.icon} />
                  </svg>
                  <span className="entry-label">{c.label}</span>
                  <span className="entry-hint">{c.hint}</span>
                </button>
              ))}
            </div>

            <div className="agent-suggestions">
              {SUGGESTIONS.map((s) => (
                <button key={s} className="suggestion" onClick={() => send(s)}>
                  {s}
                </button>
              ))}
            </div>
            {caps && <p className="type-caption agent-note">{caps.note}</p>}
          </div>
        )}

        {messages.map((m, i) =>
          m.role === 'user' ? (
            <div className="msg-user" key={i}>
              <span>{m.text}</span>
              {m.attachments?.length ? (
                <span className="message-attachments">
                  {m.attachments.map((name) => <span className="attachment-chip" key={name}>↗ {name}</span>)}
                </span>
              ) : null}
            </div>
          ) : (
            <AgentMessage key={i} m={m} />
          ),
        )}

        {busy && (
          <div className="msg-agent">
            <span className="thinking" aria-live="polite">
              <span /> <span /> <span />
            </span>
          </div>
        )}
      </div>

      <form
        className="composer-wrap"
        onSubmit={(e) => {
          e.preventDefault()
          send(input)
        }}
      >
        <div className="composer-box">
          <input ref={fileRef} className="composer-file" type="file" multiple accept=".txt,.md,.markdown,.csv,.json,text/plain,text/markdown,text/csv,application/json" onChange={attachFiles} />
          <button type="button" className="composer-icon" onClick={() => fileRef.current?.click()} aria-label="Attach a text file" title="Attach text, Markdown, CSV, or JSON">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="m21.4 11.6-8.9 8.9a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5" /></svg>
          </button>
          <textarea
            className="composer-input"
            placeholder={`Ask about ${symbol} — build, backtest, simulate, forecast…`}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onInputKeyDown}
            aria-label="Message the agent"
          />
          <button type="button" className={`composer-icon ${listening ? 'is-listening' : ''}`} onClick={toggleVoice} aria-pressed={listening} aria-label={listening ? 'Stop voice dictation' : 'Start voice dictation'} title="Voice dictation">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3M8 21h8" /></svg>
          </button>
          <button className="composer-send" type="submit" disabled={busy || (!input.trim() && attachments.length === 0)} aria-label="Send">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
              strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d="M12 19V5M5 12l7-7 7 7" />
            </svg>
          </button>
        </div>
        {attachments.length > 0 && (
          <div className="composer-attachments" aria-label="Attachments ready to send">
            {attachments.map((attachment) => (
              <span className="attachment-chip" key={attachment.name}>
                {attachment.name}
                <button type="button" onClick={() => setAttachments((files) => files.filter((file) => file.name !== attachment.name))} aria-label={`Remove ${attachment.name}`}>×</button>
              </span>
            ))}
          </div>
        )}
        <div className="composer-bar">
          <label className="composer-model" title={caps?.model_configured ? `Configured ${caps.provider ?? 'AI'} models run server-side.` : 'Add an AI provider key on the server to enable a live model.'}>
            <span className="pulse-dot" aria-hidden />
            <select value={model || 'rules'} aria-label="Agent model" disabled={!caps?.model_configured}
              onChange={(event) => setModel(event.target.value)}>
              {caps?.model_configured
                ? (caps.models ?? []).map((choice) => <option key={choice} value={choice}>{choice}</option>)
                : <option value="rules">Rules engine</option>}
            </select>
          </label>
          <button
            type="button"
            className="composer-pill toggle"
            aria-pressed={autoRun}
            onClick={() => setAutoRun((v) => !v)}
            title="When on, navigation actions are performed immediately. When off they appear as buttons on the turn."
          >
            {autoRun ? 'Auto-run actions' : 'Confirm actions'}
          </button>
          <div className="composer-tools">
            <button type="button" className="composer-pill toggle" aria-expanded={toolsOpen} onClick={() => setToolsOpen((open) => !open)}>
              Tools
            </button>
            {toolsOpen && (
              <div className="composer-menu">
                <button type="button" onClick={() => { setMessages([]); setSessionId(null); setToolsOpen(false) }}>New conversation</button>
                <button type="button" onClick={() => { setInput(''); setAttachments([]); setToolsOpen(false) }}>Clear draft</button>
                <span>Text attachments are sent as context.</span>
              </div>
            )}
          </div>
          <span className="composer-foot type-caption">
            Builds, simulates and paper-trades — it cannot place a live order.
          </span>
        </div>
        {attachmentError && <p className="composer-status type-caption" role="status">{attachmentError}</p>}
      </form>
      </section>
    </Shell>
  )
}

function AgentMessage({ m }: { m: Message }) {
  const t = m.turn
  const router = useRouter()
  return (
    <div className="msg-agent">
      {t && !t.understood && <span className="chip warnchip">not understood</span>}
      <div className="msg-body">{renderMarkdown(m.text)}</div>

      {t?.tool_calls?.length ? (
        <div className="tool-calls">
          {t.tool_calls.map((c, i) => (
            <details key={i} className="code-block">
              <summary className="code-header">
                <span>ran {c.tool}</span>
                <span className="type-caption">arguments</span>
              </summary>
              <pre className="type-code">{JSON.stringify(c.args, null, 2)}</pre>
            </details>
          ))}
        </div>
      ) : null}

      {t?.actions?.length ? (
        <div className="agent-actions">
          {t.actions.map((a, i) =>
            a.type === 'open_position' ? (
              <button
                key={i}
                className="btn primary"
                onClick={() =>
                  router.push(
                    `/lab?symbol=${a.symbol}&template=${a.template}` +
                      (a.params ? `&params=${encodeURIComponent(JSON.stringify(a.params))}` : ''),
                  )
                }
              >
                Open in Strategy Lab
              </button>
            ) : (
              <span key={i} className="chip">
                {a.type}
                {a.to ? ` → ${a.to}` : ''}
              </span>
            ),
          )}
        </div>
      ) : null}
    </div>
  )
}

/**
 * Minimal markdown: bold and paragraphs, which is all the agent emits.
 * A full parser here would be a dependency and an injection surface for
 * strings that come back from an API — this handles what is actually sent.
 */
function renderMarkdown(text: string) {
  return text.split('\n').map((line, i) => {
    if (!line.trim()) return <br key={i} />
    const parts = line.split(/(\*\*[^*]+\*\*)/g)
    return (
      <p key={i}>
        {parts.map((p, j) =>
          p.startsWith('**') && p.endsWith('**') ? <strong key={j}>{p.slice(2, -2)}</strong> : p,
        )}
      </p>
    )
  })
}

function LandingPage() {
  return (
    <div className="public-site trader-landing">
      <div className="public-orb public-orb-one" aria-hidden />
      <div className="public-orb public-orb-two" aria-hidden />
      <header className="public-nav">
        <Link className="public-brand" href="/" aria-label="MarketShark home">
          <span className="public-mark"><svg viewBox="0 0 24 24" aria-hidden><path d="M3 15c4-1 6-4 8-8 1.6 3.2 3.5 5 6.1 6L22 9l-3.2 8C15.2 20 8.7 20.3 5 18z" /></svg></span>
          <span>MarketShark <small>TRADER</small></span>
        </Link>
        <nav className="public-nav-links" aria-label="Primary navigation">
          <a href="#platform">PLATFORM</a>
          <a href="#features">FEATURES</a>
          <a href="#workflow">WORKFLOW</a>
          <Link href="/affiliate">Partners</Link>
        </nav>
        <div className="public-nav-actions">
          <Link className="public-text-link" href="/login">Log in</Link>
          <Link className="public-button public-button-sm" href="/?workspace=1">Open terminal</Link>
        </div>
      </header>

      <main>
        <section className="landing-hero">
          <div className="landing-copy">
            <div className="public-eyebrow"><span /> OPTIONS INTELLIGENCE · ONE WORKSPACE</div>
            <h1>Find the signal.<br /><em>Keep the edge.</em></h1>
            <p>MarketShark brings real ticker search, live charting, options research, strategy design, news intelligence, and disciplined trade planning into one focused workspace.</p>
            <div className="landing-actions">
              <Link className="public-button" href="/?workspace=1">Launch MarketShark <span>↗</span></Link>
              <a className="public-button public-button-quiet" href="#workflow">Explore the system <span>↓</span></a>
            </div>
            <div className="landing-trust">
              <span><b>Options</b> strategy lab</span><i />
              <span><b>Live</b> market views</span><i />
              <span><b>Paper</b> workflows only</span>
            </div>
          </div>

          <div className="hero-terminal" aria-label="MarketShark terminal preview">
            <div className="terminal-top"><span className="terminal-live"><i /> MARKET OPEN</span><span>Workspace / NVDA</span><span className="terminal-dot">•••</span></div>
            <div className="terminal-body">
              <div className="terminal-sidebar"><span className="selected" /><span /><span /><span /><span /></div>
              <div className="terminal-chart">
                <div className="terminal-title"><div><small>NASDAQ · NVIDIA</small><strong>NVDA <b>+2.41%</b></strong></div><span>180.31</span></div>
                <svg viewBox="0 0 580 225" preserveAspectRatio="none" role="img" aria-label="Illustrative market chart">
                  <defs><linearGradient id="chart-fill" x1="0" x2="0" y1="0" y2="1"><stop stopColor="#4ce1ca" stopOpacity=".3"/><stop offset="1" stopColor="#4ce1ca" stopOpacity="0"/></linearGradient></defs>
                  <path className="chart-grid" d="M0 38H580M0 94H580M0 150H580M0 206H580" /><path className="chart-area" d="M0 195 L25 179 49 187 72 154 98 163 121 137 147 151 173 121 196 142 220 110 245 122 270 84 294 105 316 77 341 92 364 52 391 73 415 44 440 69 465 31 487 50 511 21 536 41 558 17 580 28V225H0Z" /><path className="chart-line" d="M0 195 L25 179 49 187 72 154 98 163 121 137 147 151 173 121 196 142 220 110 245 122 270 84 294 105 316 77 341 92 364 52 391 73 415 44 440 69 465 31 487 50 511 21 536 41 558 17 580 28" /></svg>
                <div className="terminal-chart-foot"><span>09:30</span><span>11:00</span><span>13:00</span><span>15:30</span></div>
              </div>
            </div>
            <div className="terminal-insight"><span className="insight-icon">✦</span><p><small>MARKETSHARK INTELLIGENCE</small><b>Volatility is pricing a 6.8% move. Your iron condor range remains inside the expected move.</b></p><span className="insight-arrow">↗</span></div>
          </div>
        </section>

        <section className="landing-strip" aria-label="Platform summary">
          <div><strong>01</strong><span>Research the tape</span><small>Charting and current market context</small></div>
          <div><strong>02</strong><span>Design the position</span><small>Clear structures, payoff and risk</small></div>
          <div><strong>03</strong><span>Test the thesis</span><small>Replay, forecast and paper workflows</small></div>
        </section>

        <section className="landing-section" id="platform">
          <div className="section-kicker">ONE CONNECTED WORKSPACE</div>
          <div className="section-heading"><h2>Calm surfaces.<br />Serious tools.</h2><p>Everything is made to keep your decision-making focused: less noise, more context, and a clean path from observation to a documented trade idea.</p></div>
          <div className="feature-grid">
            <article className="feature-card feature-card-wide"><span className="feature-index">01</span><div><h3>Options intelligence</h3><p>Build, visualize and compare multi-leg structures with payoff, greeks, volatility and expiration context in view.</p></div><div className="feature-visual payoff-visual"><span /><span /><span /><i /></div></article>
            <article className="feature-card"><span className="feature-index">02</span><div><h3>News Desk</h3><p>Watchlist, macro and futures context organized around what moves a trading day.</p></div><div className="feature-visual news-visual"><i /><i /><i /></div></article>
            <article className="feature-card"><span className="feature-index">03</span><div><h3>AI research assistant</h3><p>Ask, inspect the underlying tools, and keep every workflow transparent.</p></div><div className="feature-visual assistant-visual"><span>What is the expected move?</span><i>Calculated from options data</i></div></article>
          </div>
          <div className="capability-section" id="features">
            <div className="capability-heading"><span>THE NEW MARKETSHARK STACK</span><p>Every surface is connected to the same research process—move from a live symbol to a tested position without losing context.</p></div>
            <div className="capability-grid">
              <article><span>01</span><h3>Live symbol search</h3><p>Look up the ticker you actually trade, then carry it through charts, research and strategy tools.</p></article>
              <article><span>02</span><h3>TradingView charting</h3><p>Interactive market charts built around the symbol in focus, with the same clean terminal language.</p></article>
              <article><span>03</span><h3>Options strategy lab</h3><p>Shape multi-leg positions, inspect payoff and greeks, and compare risk before entering a paper workflow.</p></article>
              <article><span>04</span><h3>News Desk</h3><p>Watchlist headlines, market-moving stories, economics, futures context and live market broadcasts in one desk.</p></article>
              <article><span>05</span><h3>Backtest &amp; paper workflows</h3><p>Replay a thesis, understand the caveats, and keep live execution outside the product until you choose otherwise.</p></article>
              <article><span>06</span><h3>Research copilot</h3><p>Use transparent, tool-backed analysis with conversation history, strategy context, and visible tool calls.</p></article>
            </div>
          </div>
        </section>

        <section className="workflow-section" id="workflow">
          <div><div className="section-kicker">BUILT FOR A DELIBERATE PROCESS</div><h2>A workspace that<br /><em>thinks with you.</em></h2></div>
          <div className="workflow-list">
            <div><b>01</b><span><strong>Observe</strong><small>Price, volatility, news and event risk in one place.</small></span></div>
            <div><b>02</b><span><strong>Frame</strong><small>Translate an opinion into a position with explicit risk.</small></span></div>
            <div><b>03</b><span><strong>Review</strong><small>Test assumptions and retain the reasoning behind every idea.</small></span></div>
          </div>
        </section>

        <section className="learning-section" aria-labelledby="learning-heading">
          <div className="learning-intro">
            <div className="section-kicker">ADAPTIVE TRADING HARNESS</div>
            <h2 id="learning-heading">A research process that<br /><em>gets sharper over time.</em></h2>
            <p>MarketShark is designed to turn your repeated research into a structured feedback loop. It captures the context behind an idea, measures what happened, and surfaces the next improvement for your review.</p>
            <p className="learning-note"><span>✦</span> It does not self-authorize live orders. Strategy changes remain visible, testable, and subject to your approval.</p>
          </div>
          <div className="learning-loop" aria-label="MarketShark adaptive research loop">
            <div className="loop-line loop-line-one" /><div className="loop-line loop-line-two" />
            <article className="loop-node loop-node-one"><span>01</span><i>⌁</i><h3>Capture context</h3><p>Symbols, thesis, setup, volatility, catalysts, and the tools used are retained with each research session.</p></article>
            <article className="loop-node loop-node-two"><span>02</span><i>⌬</i><h3>Measure outcomes</h3><p>Backtests, paper workflows, forecasts, and observed price behavior create a traceable scorecard.</p></article>
            <article className="loop-node loop-node-three"><span>03</span><i>✦</i><h3>Find patterns</h3><p>The harness compares recurring conditions, identifies weak assumptions, and keeps useful strategy notes in memory.</p></article>
            <article className="loop-node loop-node-four"><span>04</span><i>↗</i><h3>Propose the next test</h3><p>It turns evidence into a suggested adjustment—then asks you to review, backtest, and approve it.</p></article>
            <div className="loop-core"><span>MARKETSHARK</span><b>Research<br />Harness</b><small>HUMAN-IN-THE-LOOP</small></div>
          </div>
          <div className="learning-output">
            <div><small>DAILY RESEARCH LAYER</small><strong>Market, volatility, news, events and watchlist context feed a documented research record.</strong></div>
            <div><small>STRATEGY MEMORY</small><strong>Explicit preferences and saved approaches stay available for future sessions—not hidden in a black box.</strong></div>
            <div><small>IMPROVEMENT GATE</small><strong>Every proposed refinement can be inspected, tested in history, paper-traded, and accepted or rejected.</strong></div>
          </div>
        </section>

        <section className="dojo-section" aria-labelledby="dojo-heading">
          <div className="dojo-heading"><div><div className="section-kicker">STRATEGY DOJO · BACKTESTING</div><h2 id="dojo-heading">Before a strategy evolves,<br /><em>it has to survive the Dojo.</em></h2></div><p>The Dojo is the test harness behind the improvement loop. It searches permitted rule variations, keeps the base strategy intact for comparison, and validates candidates on data they did not train against.</p></div>
          <div className="dojo-board">
            <div className="dojo-steps">
              <article><b>01</b><div><h3>Define the tradeable rules</h3><p>Start with an explicit position, entry conditions, expiration, exits, sizing assumptions, and the parameters the harness is allowed to vary.</p></div></article>
              <article><b>02</b><div><h3>Run historical variants</h3><p>The Dojo tests many bounded variations against the same history instead of quietly changing the strategy you chose.</p></div></article>
              <article><b>03</b><div><h3>Separate training from validation</h3><p>Rules are selected on one window, then checked on a later out-of-sample window to expose overfitting before a result is trusted.</p></div></article>
              <article><b>04</b><div><h3>Review the evidence</h3><p>MarketShark returns the winning candidate, the baseline, and the failure cases. “No edge found” is a useful answer—not a reason to force a trade.</p></div></article>
            </div>
            <aside className="dojo-scorecard">
              <div className="dojo-score-head"><span>DOJO RUN</span><b>VALIDATION FIRST</b></div>
              <div className="dojo-track"><span>BASE RULES</span><i /><small>CONTROL</small></div>
              <div className="dojo-track accent"><span>SEARCH SPACE</span><i /><small>VARIANTS</small></div>
              <div className="dojo-track violet"><span>OUT-OF-SAMPLE</span><i /><small>CHECK</small></div>
              <div className="dojo-result"><span>HARNESS RESULT</span><strong>Propose, compare, then decide.</strong><small>A modelled options backtest is labeled as such; true P&amp;L requires historical option quotes.</small></div>
            </aside>
          </div>
          <div className="dojo-principles"><div><span>↗</span><p><b>Walk-forward validation</b> preserves a future period the rule never saw while it was being selected.</p></div><div><span>◌</span><p><b>Honest backtest modes</b> distinguish modelled rules testing from a true option-quote P&amp;L replay.</p></div><div><span>✓</span><p><b>Human approval gate</b> a successful test becomes a reviewable candidate, never an automatic live trade.</p></div><Link href="/?workspace=1">OPEN THE WORKSPACE <span>↗</span></Link></div>
        </section>

        <section className="landing-cta">
          <div><div className="section-kicker">THE MARKETSHARK WORKSPACE</div><h2>Trade the process,<br /><em>not the noise.</em></h2><p>Open the terminal and build your next research workflow.</p></div>
          <Link className="public-button" href="/?workspace=1">Open MarketShark <span>↗</span></Link>
        </section>
      </main>
      <footer className="public-footer"><div className="public-brand"><span className="public-mark"><svg viewBox="0 0 24 24" aria-hidden><path d="M3 15c4-1 6-4 8-8 1.6 3.2 3.5 5 6.1 6L22 9l-3.2 8C15.2 20 8.7 20.3 5 18z" /></svg></span><span>MarketShark <small>TRADER</small></span></div><span>Research tools for deliberate traders.</span><span>© {new Date().getFullYear()} MarketShark</span></footer>
    </div>
  )
}

export default function Home() {
  const [workspace, setWorkspace] = useState(false)
  useEffect(() => {
    setWorkspace(new URLSearchParams(window.location.search).get('workspace') === '1')
  }, [])
  return workspace ? <AgentWorkspace /> : <LandingPage />
}
