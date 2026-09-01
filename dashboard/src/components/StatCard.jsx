import { toneClasses } from '../lib/tones'

// One metric: label, big value, an optional hint line underneath (e.g. a
// count backing a percentage). `loading` renders a pulsing skeleton instead
// of a value, so the stat row never flashes "₹0" while the initial fetch is
// still in flight.
export default function StatCard({ label, value, tone = 'accent', icon: Icon, hint, loading }) {
  const t = toneClasses(tone)
  return (
    <div className="rounded-lg border border-line bg-panel p-4">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium uppercase tracking-wide text-muted">{label}</span>
        {Icon && (
          <span className={`flex h-7 w-7 items-center justify-center rounded-md ${t.bg}`}>
            <Icon className={`h-4 w-4 ${t.text}`} strokeWidth={2} />
          </span>
        )}
      </div>

      {loading ? (
        <div className="mt-2 h-8 w-24 animate-pulse rounded bg-line" />
      ) : (
        <div className="mt-1 font-mono text-2xl font-semibold text-white">{value}</div>
      )}

      {hint && !loading && <div className="mt-1 text-xs text-muted">{hint}</div>}
    </div>
  )
}
