import { BATCHES } from '../lib/batches'
import { useBatch } from '../lib/batchContext'
import { toneClasses } from '../lib/tones'

// Lives in the header (App.jsx) so the choice is visible and reachable from
// every screen. Shared BatchProvider state means switching here immediately
// re-filters the Pipeline board and re-fetches the Metrics snapshot.
export default function BatchSelector() {
  const { batchId, setBatchId } = useBatch()
  const tone = toneClasses('accent')

  return (
    <div className="flex items-center gap-1 rounded-md border border-line bg-panel p-1">
      {BATCHES.map((b) => {
        const active = b.id === batchId
        return (
          <button
            key={b.id}
            type="button"
            onClick={() => setBatchId(b.id)}
            aria-pressed={active}
            className={`rounded px-2.5 py-1 text-xs font-medium transition-colors ${
              active ? `${tone.bg} ${tone.text}` : 'text-muted hover:text-white'
            }`}
          >
            {b.label}
          </button>
        )
      })}
    </div>
  )
}
