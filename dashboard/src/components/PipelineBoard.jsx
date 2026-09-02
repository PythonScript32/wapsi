import { useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, CheckCircle2, ChevronRight, Percent, TrendingUp, Wallet } from 'lucide-react'
import { supabase, subscribeToCases } from '../lib/supabase'
import { CASE_STATES, TERMINAL_STATES, stateLabel, stateTone } from '../lib/caseStates'
import { formatINR, formatPct } from '../lib/format'
import { toneClasses } from '../lib/tones'
import { useBatch } from '../lib/batchContext'
import StatCard from './StatCard'
import CaseCard from './CaseCard'
import PipelineStrip from './PipelineStrip'

// Deliberately NOT select('*') -- cases.latent is synthetic ground truth
// the pipeline itself is never allowed to read (see app/detection/
// batch_scanner.py); the dashboard shouldn't pull it into the browser
// either, even though RLS permits it row-wise.
const CASE_COLUMNS = 'id, customer_ref, amount, reason_category, state, recovered_amount, updated_at'

// How long a card keeps its "just changed" ring after a Realtime event.
const HIGHLIGHT_MS = 1500

export default function PipelineBoard() {
  const { batchId, batch } = useBatch()
  const [cases, setCases] = useState(null) // null = initial fetch still in flight
  const [error, setError] = useState(null)
  const [justMoved, setJustMoved] = useState(() => new Set())
  const casesById = useRef(new Map())
  const scrollRef = useRef(null)
  // The "scroll ->" hint dismisses the first time the user scrolls the board
  // themselves; canScrollRight is remeasured on every layout change so the
  // edge fade only shows while there's actually more to see.
  const [hasScrolled, setHasScrolled] = useState(false)
  const [canScrollRight, setCanScrollRight] = useState(false)

  useEffect(() => {
    let cancelled = false
    // Switching batches starts a fresh board -- stale rows from the other
    // batch must not linger while the new one loads.
    casesById.current = new Map()
    setCases(null)
    setError(null)
    setHasScrolled(false)
    // Rows a Realtime event has already delivered before the initial fetch
    // resolves -- the fetch must not clobber those with a now-stale read.
    const seenViaRealtime = new Set()

    function flash(id) {
      setJustMoved((prev) => new Set(prev).add(id))
      setTimeout(() => {
        setJustMoved((prev) => {
          const next = new Set(prev)
          next.delete(id)
          return next
        })
      }, HIGHLIGHT_MS)
    }

    const channel = subscribeToCases(batchId, (payload) => {
      const { eventType, new: newRow, old: oldRow } = payload

      if (eventType === 'DELETE') {
        casesById.current.delete(oldRow.id)
        seenViaRealtime.add(oldRow.id)
      } else {
        const previous = casesById.current.get(newRow.id)
        casesById.current.set(newRow.id, newRow)
        seenViaRealtime.add(newRow.id)
        if (!previous || previous.state !== newRow.state) flash(newRow.id)
      }

      if (!cancelled) setCases(Array.from(casesById.current.values()))
    })

    async function loadInitial() {
      const { data, error: fetchError } = await supabase
        .from('cases')
        .select(CASE_COLUMNS)
        .eq('batch_id', batchId)
        .order('updated_at', { ascending: false })
        .limit(1000)

      if (cancelled) return
      if (fetchError) {
        setError(fetchError.message)
        return
      }
      for (const row of data || []) {
        if (!seenViaRealtime.has(row.id)) casesById.current.set(row.id, row)
      }
      setCases(Array.from(casesById.current.values()))
    }
    loadInitial()

    return () => {
      cancelled = true
      supabase.removeChannel(channel)
    }
  }, [batchId])

  function measureScrollAffordance() {
    const el = scrollRef.current
    if (!el) return
    setCanScrollRight(el.scrollWidth - el.clientWidth - el.scrollLeft > 4)
  }

  // After a completed batch, every case sits in a terminal column (Recovered
  // / Escalated / Closed Lost); empty lifecycle columns aren't rendered at
  // all (see the board below), so this mostly matters on narrow viewports --
  // auto-scroll so the populated columns are what a first-time viewer
  // actually sees, rather than whatever fits first.
  useEffect(() => {
    if (!cases) return
    const el = scrollRef.current
    if (!el) return
    const id = requestAnimationFrame(() => {
      const allTerminal = cases.length > 0 && cases.every((c) => TERMINAL_STATES.has(c.state))
      if (allTerminal) {
        el.scrollTo({ left: el.scrollWidth - el.clientWidth, behavior: 'smooth' })
      }
      measureScrollAffordance()
    })
    return () => cancelAnimationFrame(id)
  }, [cases])

  useEffect(() => {
    window.addEventListener('resize', measureScrollAffordance)
    return () => window.removeEventListener('resize', measureScrollAffordance)
  }, [])

  const stats = useMemo(() => {
    if (!cases) return null
    const atRisk = cases.reduce((sum, c) => sum + Number(c.amount || 0), 0)
    const recoveredCases = cases.filter((c) => c.state === 'RECOVERED')
    const recovered = recoveredCases.reduce((sum, c) => sum + Number(c.recovered_amount || c.amount || 0), 0)
    const active = cases.filter((c) => !TERMINAL_STATES.has(c.state)).length
    const rate = cases.length ? recoveredCases.length / cases.length : null
    // Value recovery rate differs from case-count recovery rate (48.0% vs
    // 44.7% on the holdout set) -- showing both is more honest than either
    // alone, since a case-count rate can look better by recovering many
    // small amounts while missing the big ones.
    const valueRate = atRisk ? recovered / atRisk : null
    return {
      atRisk, recovered, active, rate, valueRate,
      recoveredCount: recoveredCases.length, total: cases.length,
    }
  }, [cases])

  const columns = useMemo(() => {
    const byState = new Map(CASE_STATES.map((s) => [s, []]))
    for (const c of cases || []) {
      const bucket = byState.has(c.state) ? c.state : 'DETECTED'
      byState.get(bucket).push(c)
    }
    for (const list of byState.values()) {
      list.sort((a, b) => new Date(b.updated_at) - new Date(a.updated_at))
    }
    return byState
  }, [cases])

  // Feeds the always-visible PipelineStrip -- null (not zeros) while still
  // loading, so the strip can show "—" instead of a misleading "0".
  const counts = useMemo(() => {
    if (!cases) return null
    return new Map(CASE_STATES.map((s) => [s, columns.get(s)?.length || 0]))
  }, [cases, columns])

  if (error) {
    return (
      <div className="rounded-lg border border-lost/30 bg-lost/10 p-6 text-sm text-lost">
        Couldn&apos;t load the pipeline: {error}. Check VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY in
        dashboard/.env.
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-6">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">
        Batch · {batch.label}
      </h2>

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5">
        <StatCard
          label="₹ At Risk" tone="atrisk" icon={Wallet} loading={!cases}
          value={stats ? formatINR(stats.atRisk) : ''}
        />
        <StatCard
          label="₹ Recovered" tone="recovered" icon={CheckCircle2} loading={!cases}
          value={stats ? formatINR(stats.recovered) : ''}
        />
        <StatCard
          label="Recovery Rate" tone="accent" icon={TrendingUp} loading={!cases}
          value={stats ? formatPct(stats.rate) : ''}
          hint={stats ? `${stats.recoveredCount}/${stats.total} cases` : undefined}
        />
        <StatCard
          label="Money Recovered Rate" tone="recovered" icon={Percent} loading={!cases}
          value={stats ? formatPct(stats.valueRate) : ''}
          hint={stats ? `${formatINR(stats.recovered)} / ${formatINR(stats.atRisk)}` : undefined}
        />
        <StatCard
          label="Active Cases" tone="promise" icon={AlertTriangle} loading={!cases}
          value={stats ? String(stats.active) : ''}
        />
      </div>

      <PipelineStrip counts={counts} loading={!cases} />

      {cases && cases.length === 0 ? (
        <EmptyBoard />
      ) : (
        <div className="relative">
          <div
            ref={scrollRef}
            onScroll={() => {
              setHasScrolled(true)
              measureScrollAffordance()
            }}
            className="flex gap-4 overflow-x-auto pb-2"
          >
            {CASE_STATES.map((state) => {
              const stateCases = cases ? columns.get(state) : null
              // A stage with 0 cases isn't rendered at all -- PipelineStrip
              // above is what keeps it part of the visible story. Still
              // showing a skeleton for every stage while the initial fetch
              // is in flight (stateCases === null), since we don't yet know
              // which ones are actually empty.
              if (stateCases !== null && stateCases.length === 0) {
                return null
              }
              return <Column key={state} state={state} cases={stateCases} justMoved={justMoved} />
            })}
          </div>

          {canScrollRight && (
            <div className="pointer-events-none absolute inset-y-0 right-0 flex w-16 items-center justify-end bg-gradient-to-l from-ink to-transparent pb-2">
              {!hasScrolled && (
                <span className="mr-1 flex animate-pulse items-center gap-0.5 rounded-full border border-line bg-panel px-2 py-1 text-[11px] font-medium text-accent shadow-lg">
                  Scroll
                  <ChevronRight className="h-3 w-3" />
                </span>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function Column({ state, cases, justMoved }) {
  const tone = toneClasses(stateTone(state))
  const total = (cases || []).reduce((sum, c) => sum + Number(c.amount || 0), 0)

  return (
    <div className="flex w-72 shrink-0 flex-col rounded-lg border border-line bg-panel/50">
      <div className={`flex items-center justify-between rounded-t-lg border-b px-3 py-2 ${tone.border}`}>
        <div className="flex items-center gap-2">
          <span className={`h-2 w-2 rounded-full ${tone.dot}`} />
          <span className="text-sm font-medium text-white">{stateLabel(state)}</span>
        </div>
        <span className="text-xs text-muted">{cases ? cases.length : '—'}</span>
      </div>

      <div className="flex max-h-[calc(100vh-260px)] flex-col gap-2 overflow-y-auto p-2">
        {cases === null ? (
          <ColumnSkeleton />
        ) : cases.length === 0 ? (
          <div className="px-2 py-6 text-center text-xs text-muted">No cases</div>
        ) : (
          cases.map((c) => <CaseCard key={c.id} case={c} justMoved={justMoved.has(c.id)} />)
        )}
      </div>

      {cases && cases.length > 0 && (
        <div className="border-t border-line px-3 py-1.5 text-right font-mono text-xs text-muted">
          {formatINR(total)}
        </div>
      )}
    </div>
  )
}

function ColumnSkeleton() {
  return (
    <>
      <div className="h-16 animate-pulse rounded-lg border border-line bg-line/40" />
      <div className="h-16 animate-pulse rounded-lg border border-line bg-line/40" />
    </>
  )
}

function EmptyBoard() {
  return (
    <div className="rounded-lg border border-line bg-panel p-10 text-center">
      <p className="text-sm font-medium text-white">No cases yet</p>
      <p className="mx-auto mt-1 max-w-md text-sm text-muted">
        Run a batch (<code className="font-mono text-xs">python -m app.detection.batch_scanner</code>) or wait
        for a webhook to populate the pipeline. This board updates live the moment rows land in Supabase.
      </p>
    </div>
  )
}
