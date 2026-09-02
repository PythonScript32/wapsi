import { useEffect, useState } from 'react'
import { fetchBatchResults } from '../lib/metricsApi'
import { useBatch } from '../lib/batchContext'
import HeadlineMetrics from '../components/HeadlineMetrics'
import SafetyInvariants from '../components/SafetyInvariants'
import RecoveryByReasonChart from '../components/RecoveryByReasonChart'
import GateBlockTable from '../components/GateBlockTable'
import ExceptionList from '../components/ExceptionList'

// Screen 3 (PRD §12): headline (lift is the hero number), safety invariants,
// recovery-by-reason vs naive, gate-block table, exception list. Reads
// data/results_{set}.json verbatim (shipped as a static asset -- see
// lib/metricsApi.js) for whichever batch is selected in the header -- every
// number here comes straight from app/metrics/compute()'s own output, never
// recomputed in the browser, so it matches that batch's CLI summary exactly.
export default function MetricsPage() {
  const { batchId, batch } = useBatch()
  const [state, setState] = useState({ status: 'loading' })

  useEffect(() => {
    let cancelled = false
    setState({ status: 'loading' })

    fetchBatchResults(batchId)
      .then((data) => {
        if (!cancelled) setState({ status: 'ready', data })
      })
      .catch((err) => {
        if (!cancelled) setState({ status: 'error', message: err.message })
      })

    return () => {
      cancelled = true
    }
  }, [batchId])

  if (state.status === 'loading') return <LoadingState />
  if (state.status === 'error') return <ErrorState batch={batch} message={state.message} />

  const { data } = state

  return (
    <div className="flex flex-col gap-8">
      <div>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted">
          {batch.description}
        </h2>
        <HeadlineMetrics data={data} />
      </div>

      <SafetyInvariants data={data} />

      <section>
        <h3 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted">
          Recovery by reason — वापसी vs naive baseline
        </h3>
        <RecoveryByReasonChart recoveryByReason={data.recovery_by_reason} />
      </section>

      <section>
        <h3 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted">Gate blocks</h3>
        <GateBlockTable gateBlockCounts={data.gate_block_counts} />
      </section>

      <section>
        <h3 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted">
          Exception list — unrecovered cases by reason
        </h3>
        <ExceptionList worstThreeReasons={data.worst_three_reasons} exceptionList={data.exception_list} />
      </section>
    </div>
  )
}

function LoadingState() {
  return (
    <div className="flex flex-col gap-8">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="h-28 animate-pulse rounded-lg border border-line bg-panel" />
        ))}
      </div>
      <div className="h-24 animate-pulse rounded-lg border border-line bg-panel" />
      <div className="h-80 animate-pulse rounded-lg border border-line bg-panel" />
    </div>
  )
}

function ErrorState({ batch, message }) {
  return (
    <div className="rounded-lg border border-lost/30 bg-lost/10 p-6 text-sm text-lost">
      <p className="font-medium">Couldn&apos;t load batch results.</p>
      <p className="mt-1 text-muted">{message}</p>
      <p className="mt-2 text-muted">
        Run a batch, then restart the dashboard so it picks up the fresh file (
        <code className="font-mono text-xs">python -m app.detection.batch_scanner --set {batch.id}</code>).
      </p>
    </div>
  )
}
