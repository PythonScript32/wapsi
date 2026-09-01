import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { reasonLabel } from '../lib/caseStates'
import { CHART_COLORS } from '../lib/chartColors'
import { formatPct } from '../lib/format'

// Grouped bar chart: recovery-by-reason, ours vs naive, one pair of bars per
// reason category. Both series come straight from the snapshot
// (recovery_by_reason[reason].rate / .naive_rate, computed by
// app/metrics/compute.py) -- never recomputed here.
export default function RecoveryByReasonChart({ recoveryByReason }) {
  const rows = Object.entries(recoveryByReason || {})
    .map(([reason, row]) => ({
      reason,
      label: reasonLabel(reason),
      ours: row.rate * 100,
      naive: row.naive_rate * 100,
      count: row.count,
    }))
    .sort((a, b) => b.count - a.count)

  if (rows.length === 0) {
    return (
      <div className="rounded-lg border border-line bg-panel p-8 text-center text-sm text-muted">
        No per-reason data in this snapshot.
      </div>
    )
  }

  return (
    <div className="rounded-lg border border-line bg-panel p-4">
      <ResponsiveContainer width="100%" height={320}>
        <BarChart data={rows} margin={{ top: 8, right: 8, left: 0, bottom: 8 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={CHART_COLORS.line} vertical={false} />
          <XAxis
            dataKey="label" stroke={CHART_COLORS.muted} tick={{ fill: CHART_COLORS.muted, fontSize: 12 }}
            interval={0} angle={-20} textAnchor="end" height={60}
          />
          <YAxis
            stroke={CHART_COLORS.muted} tick={{ fill: CHART_COLORS.muted, fontSize: 12 }}
            tickFormatter={(v) => `${v}%`} width={44}
          />
          <Tooltip
            contentStyle={{ background: CHART_COLORS.panel, border: `1px solid ${CHART_COLORS.line}`, borderRadius: 8 }}
            labelStyle={{ color: '#fff' }}
            formatter={(value) => formatPct(value / 100)}
          />
          <Legend wrapperStyle={{ color: CHART_COLORS.muted, fontSize: 12 }} />
          <Bar dataKey="ours" name="वापसी" fill={CHART_COLORS.accent} radius={[4, 4, 0, 0]} />
          <Bar dataKey="naive" name="Naive baseline" fill={CHART_COLORS.muted} radius={[4, 4, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
