import { CalendarClock } from 'lucide-react'
import { EmptyPanel } from './AttemptsList'
import { formatDate, formatDateTime, formatINR } from '../lib/format'
import { toneClasses } from '../lib/tones'

const STATUS_TONE = { pending: 'atrisk', kept: 'recovered', broken: 'lost' }

// Right-rail panel: every promise-to-pay for this case. `source` (voice |
// text | inferred, app/promises/tracker.py) is the differentiator the demo
// wants visible -- shown as a small badge when present.
export default function PromisesList({ promises }) {
  if (!promises || promises.length === 0) {
    return <EmptyPanel icon={CalendarClock} text="No promises made." />
  }

  const ordered = [...promises].sort((a, b) => new Date(b.created_at) - new Date(a.created_at))

  return (
    <div className="flex flex-col gap-2">
      {ordered.map((p) => {
        const tone = toneClasses(STATUS_TONE[p.status] || 'muted')
        return (
          <div key={p.id} className="rounded-lg border border-line bg-panel p-3">
            <div className="flex items-center justify-between">
              <span className="font-mono text-sm text-white">{formatINR(p.promised_amount)}</span>
              <span className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${tone.bg} ${tone.border} ${tone.text}`}>
                {p.status}
              </span>
            </div>
            <div className="mt-1 text-xs text-muted">promised by {formatDate(p.promised_date)}</div>
            {p.source && (
              <span className="mt-1.5 inline-block rounded bg-line px-1.5 py-0.5 font-mono text-[11px] text-muted">
                via {p.source}
              </span>
            )}
            {p.resolved_at && (
              <div className="mt-1 text-xs text-muted">resolved {formatDateTime(p.resolved_at)}</div>
            )}
          </div>
        )
      })}
    </div>
  )
}
