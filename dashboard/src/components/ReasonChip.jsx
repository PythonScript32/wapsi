import { reasonLabel, reasonTone } from '../lib/caseStates'
import { toneClasses } from '../lib/tones'

// Coloured pill per reason_category. `reason` may be null (a case that
// hasn't been diagnosed yet) -- rendered as a neutral "Unclassified" chip
// rather than an empty gap, so a DETECTED-column card doesn't look broken.
export default function ReasonChip({ reason }) {
  const tone = toneClasses(reasonTone(reason))
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium ${tone.bg} ${tone.border} ${tone.text}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${tone.dot}`} />
      {reasonLabel(reason)}
    </span>
  )
}
