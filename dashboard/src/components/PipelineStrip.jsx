import { CASE_STATES, stateLabel, stateTone } from '../lib/caseStates'
import { toneClasses } from '../lib/tones'

// The full ten-stage lifecycle, always visible, ~40px tall. The board below
// only renders columns that actually have cases (see PipelineBoard.jsx) --
// this strip is what keeps the rest of the story on screen instead of
// silently dropping it.
export default function PipelineStrip({ counts, loading }) {
  return (
    <div className="flex h-10 flex-wrap items-center gap-2">
      {CASE_STATES.map((state) => {
        const count = counts ? counts.get(state) ?? 0 : 0
        const populated = !loading && count > 0
        const tone = toneClasses(populated ? stateTone(state) : 'muted')
        return (
          <span
            key={state}
            className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs ${tone.border} ${
              populated ? `${tone.bg} ${tone.text}` : 'bg-panel/40 text-muted'
            }`}
          >
            {stateLabel(state)}
            <span className="font-mono font-semibold">{loading ? '—' : count}</span>
          </span>
        )
      })}
    </div>
  )
}
