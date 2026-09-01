import ReasonChip from './ReasonChip'
import { formatINR } from '../lib/format'
import { stateLabel, stateTone } from '../lib/caseStates'
import { toneClasses } from '../lib/tones'

// Case detail header: customer, amount, reason, current state (coloured).
export default function CaseHeader({ case: c }) {
  const tone = toneClasses(stateTone(c.state))
  return (
    <div className="rounded-lg border border-line bg-panel p-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold text-white">{c.customer_ref || 'Unknown customer'}</h2>
          <p className="mt-0.5 font-mono text-xs text-muted">{c.id}</p>
        </div>
        <span className={`rounded-full border px-3 py-1 text-sm font-medium ${tone.bg} ${tone.border} ${tone.text}`}>
          {stateLabel(c.state)}
        </span>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-6">
        <div>
          <div className="text-xs uppercase tracking-wide text-muted">Amount</div>
          <div className="font-mono text-2xl font-semibold text-white">{formatINR(c.amount)}</div>
        </div>
        <div>
          <div className="text-xs uppercase tracking-wide text-muted">Reason</div>
          <div className="mt-1.5"><ReasonChip reason={c.reason_category} /></div>
        </div>
        <div>
          <div className="text-xs uppercase tracking-wide text-muted">Attempts</div>
          <div className="mt-1 text-sm text-white">{c.attempts_made ?? 0}</div>
        </div>
        {c.state === 'RECOVERED' && (
          <div>
            <div className="text-xs uppercase tracking-wide text-muted">Recovered</div>
            <div className="mt-1 font-mono text-sm text-recovered">{formatINR(c.recovered_amount)}</div>
          </div>
        )}
      </div>
    </div>
  )
}
