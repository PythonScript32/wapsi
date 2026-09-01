import { Link } from 'react-router-dom'
import { Clock } from 'lucide-react'
import ReasonChip from './ReasonChip'
import { formatINR, formatRelativeTime } from '../lib/format'

// Compact case tile: customer, ₹ amount (mono), reason chip, time-in-state.
// `justMoved` briefly rings the card when Realtime reports its state
// changed -- the animate-between-columns beat the demo opens on.
export default function CaseCard({ case: c, justMoved }) {
  return (
    <Link
      to={`/cases/${c.id}`}
      className={`block rounded-lg border bg-panel p-3 transition hover:border-accent/50 ${
        justMoved ? 'border-accent shadow-[0_0_0_1px_theme(colors.accent)] animate-card-pop' : 'border-line'
      }`}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="truncate text-sm font-medium text-white">{c.customer_ref || 'Unknown customer'}</span>
        <span className="shrink-0 font-mono text-sm text-white">{formatINR(c.amount)}</span>
      </div>

      <div className="mt-2 flex items-center justify-between gap-2">
        <ReasonChip reason={c.reason_category} />
        <span className="flex items-center gap-1 text-xs text-muted">
          <Clock className="h-3 w-3" />
          {formatRelativeTime(c.updated_at)}
        </span>
      </div>
    </Link>
  )
}
