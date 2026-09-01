import { CreditCard } from 'lucide-react'
import { formatDateTime } from '../lib/format'
import { toneClasses } from '../lib/tones'

const RESULT_TONE = { success: 'recovered', pending: 'atrisk', failed: 'lost' }

// Right-rail panel: every payment_attempts row for this case, newest first.
// idempotency_key is shown in full -- it's the one thing on this screen
// that proves "we never double-charged," so it's not worth truncating.
export default function AttemptsList({ attempts }) {
  if (!attempts || attempts.length === 0) {
    return <EmptyPanel icon={CreditCard} text="No payment attempts yet." />
  }

  const ordered = [...attempts].sort((a, b) => (b.attempt_no || 0) - (a.attempt_no || 0))

  return (
    <div className="flex flex-col gap-2">
      {ordered.map((a) => {
        const tone = toneClasses(RESULT_TONE[a.result] || 'muted')
        return (
          <div key={a.id} className="rounded-lg border border-line bg-panel p-3">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-white">Attempt #{a.attempt_no}</span>
              <span className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${tone.bg} ${tone.border} ${tone.text}`}>
                {a.result || 'unknown'}
              </span>
            </div>
            {a.strategy && <div className="mt-1 text-xs text-muted">strategy: {a.strategy}</div>}
            <div className="mt-1 text-xs text-muted">
              {a.executed_at ? `executed ${formatDateTime(a.executed_at)}` : a.scheduled_for ? `scheduled ${formatDateTime(a.scheduled_for)}` : null}
            </div>
            {a.failure_reason && <div className="mt-1 text-xs text-lost">{a.failure_reason}</div>}
            <div className="mt-2 truncate font-mono text-[11px] text-muted" title={a.idempotency_key}>
              key: {a.idempotency_key}
            </div>
          </div>
        )
      })}
    </div>
  )
}

export function EmptyPanel({ icon: Icon, text }) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-lg border border-line bg-panel p-6 text-center">
      <Icon className="h-5 w-5 text-muted" />
      <p className="text-xs text-muted">{text}</p>
    </div>
  )
}
