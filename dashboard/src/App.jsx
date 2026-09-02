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
import { NavLink, Route, Routes } from 'react-router-dom'
import { BatchProvider } from './lib/batchContext'
import BatchSelector from './components/BatchSelector'
import Footer from './components/Footer'
import PipelineBoard from './components/PipelineBoard'
import CaseDetailPage from './pages/CaseDetailPage'
import MetricsPage from './pages/MetricsPage'

const NAV = [
  { to: '/', label: 'Pipeline', end: true },
  { to: '/metrics', label: 'Metrics' },
]

export default function App() {
  return (
    <BatchProvider>
      <div className="flex min-h-screen flex-col">
        <header className="border-b border-line px-6 py-4 flex items-center gap-4">
          <h1 className="text-xl font-semibold tracking-tight">
            वापसी <span className="text-muted font-normal">· Revenue Recovery AI Agent</span>
          </h1>
          <BatchSelector />
          <nav className="ml-auto flex gap-1">
            {NAV.map(({ to, label, end }) => (
              <NavLink
                key={to}
                to={to}
                end={end}
                className={({ isActive }) =>
                  `border-b-2 px-3 py-1.5 text-sm transition-colors ${
                    isActive
                      ? 'border-accent text-accent font-medium'
                      : 'border-transparent text-muted hover:border-line hover:text-white'
                  }`
                }
              >
                {label}
              </NavLink>
            ))}
          </nav>
        </header>

        <main className="flex-1 p-6">
          <Routes>
            <Route path="/" element={<PipelineBoard />} />
            <Route path="/cases/:caseId" element={<CaseDetailPage />} />
            <Route path="/metrics" element={<MetricsPage />} />
          </Routes>
        </main>

        <Footer />
      </div>
    </BatchProvider>
  )
}
