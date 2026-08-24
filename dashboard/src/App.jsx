/**
 * वापसी (Wapsi) dashboard shell.
 *
 * Three screens (see PRD §12 for the full design spec):
 *   1. Pipeline    — live kanban of cases by state (Supabase Realtime)
 *   2. Case detail — the audit trail, top to bottom, for one case
 *   3. Metrics     — batch results, lift vs naive baseline, exception list
 *
 * Build order: Pipeline -> Case detail -> Metrics.
 */
import { useState } from 'react'

const TABS = ['Pipeline', 'Case detail', 'Metrics']

export default function App() {
  const [tab, setTab] = useState('Pipeline')

  return (
    <div className="min-h-screen">
      <header className="border-b border-line px-6 py-4 flex items-center gap-4">
        <h1 className="text-xl font-semibold tracking-tight">
          वापसी <span className="text-muted font-normal">· Revenue Recovery</span>
        </h1>
        <nav className="ml-auto flex gap-1">
          {TABS.map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-3 py-1.5 rounded-md text-sm ${
                tab === t ? 'bg-panel text-white' : 'text-muted hover:text-white'
              }`}
            >
              {t}
            </button>
          ))}
        </nav>
      </header>

      <main className="p-6">
        {/* TODO: render <PipelineBoard/>, <CaseDetail/>, <MetricsPage/> */}
        <div className="rounded-lg border border-line bg-panel p-8 text-muted">
          {tab} — wire this up to Supabase (see src/lib/supabase.js).
        </div>
      </main>
    </div>
  )
}
