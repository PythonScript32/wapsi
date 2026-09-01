import { Percent, Target, TrendingUp, CalendarCheck } from 'lucide-react'
import StatCard from './StatCard'
import { formatPct } from '../lib/format'

// Headline row (PRD §12 Screen 3): recovery rate, LIFT vs naive (the hero
// number -- rendered largest via StatCard's `hero` variant), ceiling
// capture, kept-promise rate WITH its denominator so a small sample (~11
// resolved promises on the dev set) is never hidden behind a bare percentage.
export default function HeadlineMetrics({ data }) {
  const lift = data.recovery_lift
  const liftKnown = lift !== null && lift !== undefined
  const liftPositive = liftKnown && lift >= 0

  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <StatCard
        label="Recovery Rate" tone="accent" icon={Percent}
        value={formatPct(data.recovery_rate_count)}
        hint={`${data.recovered_count}/${data.total_cases} cases`}
      />
      <StatCard
        label="Lift vs Naive" tone={liftPositive ? 'recovered' : 'lost'} icon={TrendingUp} hero
        value={liftKnown ? `${liftPositive ? '+' : ''}${formatPct(lift)}` : '—'}
        hint="vs. a blind immediate retry, no timing intelligence"
      />
      <StatCard
        label="Ceiling Capture" tone="promise" icon={Target}
        value={formatPct(data.ceiling_capture)}
        hint="recovered ÷ theoretical max"
      />
      <StatCard
        label="Kept-Promise Rate" tone="atrisk" icon={CalendarCheck}
        value={formatPct(data.kept_promise_rate)}
        hint={`${data.kept_promise_kept_count}/${data.kept_promise_resolved_count} resolved promises`}
      />
    </div>
  )
}
