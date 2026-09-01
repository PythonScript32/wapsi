import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { supabase } from '../lib/supabase'
import CaseHeader from '../components/CaseHeader'
import AuditTrail from '../components/AuditTrail'
import AttemptsList from '../components/AttemptsList'
import MessageThread from '../components/MessageThread'
import PromisesList from '../components/PromisesList'

// Screen 2 (PRD §12) -- what proves "explainable". Header: customer, amount,
// reason, current state. Body: the full audit trail as a vertical timeline.
// Right rail: attempts, messages (with voice transcripts), promises. A judge
// should be able to read this top to bottom and understand every rupee that
// moved, and why, without touching code.
export default function CaseDetailPage() {
  const { caseId } = useParams()
  const [state, setState] = useState({ status: 'loading' })

  useEffect(() => {
    let cancelled = false
    setState({ status: 'loading' })

    async function load() {
      const [caseRes, auditRes, attemptsRes, outreachRes, promisesRes] = await Promise.all([
        // Explicit columns, not select('*') -- cases.latent is synthetic
        // ground truth the pipeline itself is never allowed to read (see
        // app/detection/batch_scanner.py); it shouldn't reach the browser.
        supabase
          .from('cases')
          .select('id, source, customer_ref, customer_phone, amount, currency, reason_category, state, attempts_made, opted_out, recovered_amount, recovered_at, created_at, updated_at')
          .eq('id', caseId)
          .maybeSingle(),
        supabase.from('audit_log').select('*').eq('case_id', caseId).order('ts', { ascending: true }),
        supabase.from('payment_attempts').select('*').eq('case_id', caseId),
        supabase.from('outreach').select('*').eq('case_id', caseId).order('sent_at', { ascending: true }),
        supabase.from('promises').select('*').eq('case_id', caseId),
      ])

      if (cancelled) return

      const firstError = [caseRes, auditRes, attemptsRes, outreachRes, promisesRes].find((r) => r.error)?.error
      if (firstError) {
        setState({ status: 'error', message: firstError.message })
        return
      }
      if (!caseRes.data) {
        setState({ status: 'not-found' })
        return
      }

      setState({
        status: 'ready',
        case: caseRes.data,
        audit: auditRes.data || [],
        attempts: attemptsRes.data || [],
        outreach: outreachRes.data || [],
        promises: promisesRes.data || [],
      })
    }
    load()

    return () => {
      cancelled = true
    }
  }, [caseId])

  return (
    <div className="flex flex-col gap-4">
      <Link to="/" className="inline-flex w-fit items-center gap-1.5 text-sm text-muted hover:text-white">
        <ArrowLeft className="h-4 w-4" /> Back to pipeline
      </Link>

      {state.status === 'loading' && <LoadingState />}
      {state.status === 'error' && <ErrorState message={state.message} />}
      {state.status === 'not-found' && <NotFoundState caseId={caseId} />}

      {state.status === 'ready' && (
        <>
          <CaseHeader case={state.case} />

          <div className="grid grid-cols-1 gap-6 xl:grid-cols-[1fr_360px]">
            <section>
              <h3 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted">Audit trail</h3>
              <AuditTrail rows={state.audit} />
            </section>

            <aside className="flex flex-col gap-6">
              <RailSection title="Payment attempts">
                <AttemptsList attempts={state.attempts} />
              </RailSection>
              <RailSection title="Messages">
                <MessageThread
                  outreach={state.outreach}
                  replyEvents={state.audit.filter((r) => r.event_type === 'REPLY_RECEIVED')}
                />
              </RailSection>
              <RailSection title="Promises">
                <PromisesList promises={state.promises} />
              </RailSection>
            </aside>
          </div>
        </>
      )}
    </div>
  )
}

function RailSection({ title, children }) {
  return (
    <div>
      <h3 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted">{title}</h3>
      {children}
    </div>
  )
}

function LoadingState() {
  return (
    <div className="flex flex-col gap-4">
      <div className="h-32 animate-pulse rounded-lg border border-line bg-panel" />
      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[1fr_360px]">
        <div className="flex flex-col gap-3">
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-20 animate-pulse rounded-lg border border-line bg-panel" />
          ))}
        </div>
        <div className="flex flex-col gap-3">
          {[0, 1].map((i) => (
            <div key={i} className="h-24 animate-pulse rounded-lg border border-line bg-panel" />
          ))}
        </div>
      </div>
    </div>
  )
}

function ErrorState({ message }) {
  return (
    <div className="rounded-lg border border-lost/30 bg-lost/10 p-6 text-sm text-lost">
      Couldn&apos;t load this case: {message}. Check VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY in
      dashboard/.env.
    </div>
  )
}

function NotFoundState({ caseId }) {
  return (
    <div className="rounded-lg border border-line bg-panel p-8 text-center text-muted">
      No case found with id <span className="font-mono text-white">{caseId}</span>.
    </div>
  )
}
