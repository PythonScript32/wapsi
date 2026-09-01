import { useState } from 'react'
import { Link } from 'react-router-dom'
import { ChevronDown, ChevronRight } from 'lucide-react'
import ReasonChip from './ReasonChip'
import { formatINR, formatPct } from '../lib/format'

// Unrecovered cases grouped by reason, with rupees lost and the dominant
// blocking gate/state -- straight from worst_three_reasons
// (app/metrics/compute.py), the same honesty artifact the CLI's own
// "-- EXCEPTIONS --" section prints. Each row expands to the individual
// cases behind it (from the snapshot's exception_list), linking into the
// Case detail screen.
export default function ExceptionList({ worstThreeReasons, exceptionList }) {
  const [expanded, setExpanded] = useState(null)

  if (!worstThreeReasons || worstThreeReasons.length === 0) {
    return (
      <div className="rounded-lg border border-line bg-panel p-8 text-center text-sm text-muted">
        No unrecovered cases — every case in this batch recovered.
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-3">
      {worstThreeReasons.map((row) => {
        const isOpen = expanded === row.reason_category
        const cases = (exceptionList || []).filter((c) => c.reason_category === row.reason_category)
        return (
          <div key={row.reason_category} className="rounded-lg border border-line bg-panel">
            <button
              type="button"
              onClick={() => setExpanded(isOpen ? null : row.reason_category)}
              className="flex w-full flex-wrap items-center justify-between gap-3 p-4 text-left"
            >
              <div className="flex items-center gap-3">
                {isOpen ? <ChevronDown className="h-4 w-4 text-muted" /> : <ChevronRight className="h-4 w-4 text-muted" />}
                <ReasonChip reason={row.reason_category} />
                <span className="text-sm text-muted">{row.count} unrecovered</span>
              </div>
              <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-sm">
                <span className="text-muted">
                  recovery rate <span className="font-mono text-white">{formatPct(row.recovery_rate)}</span>
                </span>
                <span className="text-muted">
                  ₹ lost <span className="font-mono text-lost">{formatINR(row.rupees_lost)}</span>
                </span>
                <span className="rounded bg-line px-2 py-0.5 font-mono text-xs text-muted">
                  dominant: {row.dominant_failure_mode}
                </span>
              </div>
            </button>

            {isOpen && (
              <div className="border-t border-line p-3">
                {cases.length === 0 ? (
                  <p className="px-2 py-1 text-xs text-muted">No individual case details in this snapshot.</p>
                ) : (
                  <div className="flex flex-col gap-1">
                    {cases.map((c) => (
                      <Link
                        key={c.case_id}
                        to={`/cases/${c.case_id}`}
                        className="flex items-center justify-between gap-3 rounded-md px-2 py-1.5 text-sm hover:bg-line"
                      >
                        <span className="truncate font-mono text-xs text-muted">{c.case_id}</span>
                        <span className="shrink-0 text-xs text-muted">{c.state} · {c.attempts_made} attempts</span>
                        <span className="shrink-0 font-mono text-white">{formatINR(c.amount)}</span>
                      </Link>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
