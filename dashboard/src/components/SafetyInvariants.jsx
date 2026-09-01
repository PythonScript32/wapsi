import { CheckCircle2, XCircle } from 'lucide-react'
import { toneClasses } from '../lib/tones'

const INVARIANTS = [
  { key: 'double_charge_incidents', label: 'Double-charge incidents' },
  { key: 'post_opt_out_contacts', label: 'Post-opt-out contacts' },
  { key: 'actions_without_audit', label: 'Actions without audit' },
  { key: 'over_cap_discounts', label: 'Over-cap discounts' },
]

// The four Sec 4.3 safety invariants, as prominent PASS/FAIL badges. Every
// one is computed from the actual pipeline output (app/metrics/compute.py),
// never asserted away -- these being visibly zero is the strongest trust
// signal on the whole dashboard, so it gets its own banner, not a buried row.
export default function SafetyInvariants({ data }) {
  const allPass = INVARIANTS.every((inv) => (data[inv.key] || 0) === 0)
  const bannerTone = toneClasses(allPass ? 'recovered' : 'lost')

  return (
    <div className={`rounded-lg border-2 ${bannerTone.border} ${bannerTone.bg} p-4`}>
      <div className="mb-3 flex items-center justify-between">
        <span className="text-sm font-semibold uppercase tracking-wide text-white">Safety Invariants</span>
        <span className={`text-xs font-medium ${bannerTone.text}`}>
          {allPass ? 'All invariants hold' : 'A safety invariant failed — investigate before demoing'}
        </span>
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {INVARIANTS.map((inv) => {
          const value = data[inv.key] ?? 0
          const pass = value === 0
          const tone = toneClasses(pass ? 'recovered' : 'lost')
          const Icon = pass ? CheckCircle2 : XCircle
          return (
            <div key={inv.key} className={`flex items-center gap-2 rounded-lg border bg-panel p-3 ${tone.border}`}>
              <Icon className={`h-5 w-5 shrink-0 ${tone.text}`} />
              <div>
                <div className={`text-sm font-bold ${tone.text}`}>{pass ? 'PASS' : `FAIL (${value})`}</div>
                <div className="text-xs text-muted">{inv.label}</div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
