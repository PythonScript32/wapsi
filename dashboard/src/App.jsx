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
import { Route, Routes } from 'react-router-dom'
import { BatchProvider } from './lib/batchContext'
import Header from './components/Header'
import Footer from './components/Footer'
import PipelineBoard from './components/PipelineBoard'
import CaseDetailPage from './pages/CaseDetailPage'
import MetricsPage from './pages/MetricsPage'

export default function App() {
  return (
    <BatchProvider>
      <div className="flex min-h-screen flex-col">
        <Header />

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
