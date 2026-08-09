'use client'

import Link from 'next/link'
import { useEffect, useState } from 'react'
import { Shell, useUrlSymbol } from '@/components/Shell'

type Experiment = {
  id: string
  kind: string
  template_id?: string | null
  status: string
  hypothesis?: string | null
  updated_at: string
  evidence_count: number
}

type Journey = { id: string; name: string; symbol?: string | null; thesis?: string | null; status: string; updated_at: string }
type Overview = {
  counts: { journeys: number; experiments: number; evidence: number; paper_ready: number }
  journeys: Journey[]
  experiments: Experiment[]
  execution_policy: string
}

const LABEL: Record<string, string> = {
  backtest: 'Backtest', dojo: 'Dojo validation', forward_test: 'Monte Carlo forward test',
  paper_review: 'Paper review', research: 'Research note',
}

export default function HarnessPage() {
  const [symbol, setSymbol] = useUrlSymbol('SPY')
  const [overview, setOverview] = useState<Overview | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    fetch('/api/harness/overview')
      .then(async (response) => {
        if (!response.ok) throw new Error(`Harness API returned ${response.status}`)
        return response.json() as Promise<Overview>
      })
      .then(setOverview)
      .catch((cause) => setError(cause instanceof Error ? cause.message : 'Harness data could not load.'))
  }, [])

  return <Shell symbol={symbol} onSymbol={setSymbol} wide={false}>
    <section className="harness-page">
      <header className="harness-head"><div><span className="section-label">ADAPTIVE STRATEGY PROTOCOL</span><h1>Trading Harness</h1><p>Every measured test is retained as evidence. No result can promote itself into a trade.</p></div><Link className="btn primary" href="/?workspace=1">Ask the agent</Link></header>
      {error && <div className="harness-error">{error}</div>}
      {!overview && !error && <div className="harness-loading">Loading the strategy ledger…</div>}
      {overview && <>
        <div className="harness-policy"><span>CONTROL POLICY</span><b>{overview.execution_policy}</b></div>
        <div className="harness-counts"><Metric value={overview.counts.journeys} label="Strategy journeys" /><Metric value={overview.counts.experiments} label="Measured experiments" /><Metric value={overview.counts.evidence} label="Evidence records" /><Metric value={overview.counts.paper_ready} label="Paper-ready candidates" /></div>
        <div className="harness-grid">
          <article className="harness-panel harness-protocol"><div className="panel-title"><span>PROTOCOL</span><b>How a strategy earns trust</b></div><ol><li><i>01</i><span><strong>Frame the thesis</strong><small>Record the symbol, premise, structure and assumptions.</small></span></li><li><i>02</i><span><strong>Measure the rules</strong><small>Backtest, Dojo search, forward simulation and paper review produce evidence.</small></span></li><li><i>03</i><span><strong>Challenge the result</strong><small>Separate training from validation; preserve caveats and failed variants.</small></span></li><li><i>04</i><span><strong>Make a deliberate decision</strong><small>Hold, reject, or explicitly make a candidate paper-ready.</small></span></li></ol></article>
          <article className="harness-panel"><div className="panel-title"><span>RECENT JOURNEYS</span><b>What the harness is tracking</b></div>{overview.journeys.length ? <div className="harness-list">{overview.journeys.map((journey) => <div key={journey.id}><span className="journey-symbol">{journey.symbol || '—'}</span><span><strong>{journey.name}</strong><small>{journey.thesis || 'No thesis text recorded yet.'}</small></span><em>{journey.status}</em></div>)}</div> : <Empty copy="Run a backtest, Dojo search, or forward test from the agent to start a strategy journey." />}</article>
        </div>
        <article className="harness-panel harness-experiments"><div className="panel-title"><span>EVIDENCE QUEUE</span><b>Latest measured work</b></div>{overview.experiments.length ? <div className="experiment-table"><div className="experiment-head"><span>TEST</span><span>METHOD</span><span>EVIDENCE</span><span>STATUS</span></div>{overview.experiments.map((experiment) => <div className="experiment-row" key={experiment.id}><span><strong>{experiment.template_id?.replaceAll('_', ' ') || LABEL[experiment.kind] || experiment.kind}</strong><small>{experiment.hypothesis || 'Recorded from a MarketShark tool run.'}</small></span><span>{LABEL[experiment.kind] || experiment.kind}</span><span>{experiment.evidence_count} record{experiment.evidence_count === 1 ? '' : 's'}</span><em className={`harness-status is-${experiment.status}`}>{experiment.status.replaceAll('_', ' ')}</em></div>)}</div> : <Empty copy="Evidence will appear here only after a tool returns a measurable result." />}</article>
      </>}
    </section>
  </Shell>
}

function Metric({ value, label }: { value: number; label: string }) { return <div className="harness-metric"><strong>{value}</strong><span>{label}</span></div> }
function Empty({ copy }: { copy: string }) { return <p className="harness-empty">{copy}</p> }
