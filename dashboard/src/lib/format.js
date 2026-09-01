// Shared formatting helpers -- kept out of the components so StatCard,
// CaseCard, etc. stay small and focused on rendering, not string munging.

export function formatINR(amount) {
  const n = Number(amount)
  if (!Number.isFinite(n)) return '₹0'
  return `₹${n.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`
}

export function formatPct(fraction, decimals = 1) {
  if (fraction === null || fraction === undefined || !Number.isFinite(Number(fraction))) return '—'
  return `${(Number(fraction) * 100).toFixed(decimals)}%`
}

// Relative "time in state" -- e.g. "3m", "2h", "5d" -- read off updated_at
// (the timestamp app.db.repository.update_case bumps on every state change).
export function formatRelativeTime(iso) {
  if (!iso) return '—'
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return '—'
  const diffMs = Date.now() - then
  const diffMin = Math.round(diffMs / 60000)
  if (diffMin < 1) return 'just now'
  if (diffMin < 60) return `${diffMin}m ago`
  const diffHr = Math.round(diffMin / 60)
  if (diffHr < 24) return `${diffHr}h ago`
  const diffDay = Math.round(diffHr / 24)
  return `${diffDay}d ago`
}

// Precise timestamp for the audit trail -- an explainability screen needs
// the exact moment, not just "2h ago". e.g. "12 Sep, 14:32:05".
export function formatDateTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const datePart = d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })
  const timePart = d.toLocaleTimeString('en-IN', { hour12: false })
  return `${datePart}, ${timePart}`
}

// Short date only -- promised_date is a plain Postgres `date` (e.g.
// "2026-09-20", no time/timezone). Parsed as UTC-midnight and then
// re-formatted in the viewer's local zone (new Date("2026-09-20")) can shift
// a day backward west of UTC, so this builds the Date from local
// year/month/day components instead.
export function formatDate(value) {
  if (!value) return '—'
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value)
  const d = match
    ? new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]))
    : new Date(value)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' })
}
