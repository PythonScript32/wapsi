import { useEffect, useState } from 'react'
import { fetchBatchResults } from '../lib/metricsApi'
import HeadlineMetrics from '../components/HeadlineMetrics'
import SafetyInvariants from '../components/SafetyInvariants'
import RecoveryByReasonChart from '../components/RecoveryByReasonChart'
import GateBlockTable from '../components/GateBlockTable'
import ExceptionList from '../components/ExceptionList'

const SET_NAME = 'dev'

// Screen 3 (PRD §12): headline (lift is the hero number), safety invariants,
// recovery-by-reason vs naive, gate-block table, exception list. Reads
// data/results_{set}.json verbatim (via the FastAPI backend's
// GET /batch/results) -- every number here comes straight from
// app/metrics/compute()'s own output, never recomputed in the browser.
export default function MetricsPage() {
  const [state, setState] = useState({ status: 'loading' })

  useEffect(() => {
    let cancelled = false
    setState({ status: 'loading' })

    fetchBatchResults(SET_NAME)
      .then((data) => {
        if (!cancelled) setState({ status: 'ready', data })
      })
      .catch((err) => {
        if (!cancelled) setState({ status: 'error', message: err.message })
      })

    return () => {
      cancelled = true
    }
  }, [])

  if (state.status === 'loading') return <LoadingState />
  if (state.status === 'error') return <ErrorState message={state.message} />

  const { data } = state

  return (
    <div className="flex flex-col gap-8">
      <div>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted">
          Batch results · {SET_NAME}
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

function ErrorState({ message }) {
  return (
    <div className="rounded-lg border border-lost/30 bg-lost/10 p-6 text-sm text-lost">
      <p className="font-medium">Couldn&apos;t load batch results: {message}</p>
      <p className="mt-2 text-muted">
        Make sure the backend is running (<code className="font-mono text-xs">uvicorn app.main:app --reload --port 8000</code>)
        and a batch has been run (<code className="font-mono text-xs">python -m app.detection.batch_scanner --set {SET_NAME}</code>).
      </p>
    </div>
  )
}
