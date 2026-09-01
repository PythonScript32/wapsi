import { eventMeta, gateIdFromDecision } from '../lib/auditEvents'
import { formatDateTime } from '../lib/format'
import { toneClasses } from '../lib/tones'

// Vertical timeline, one entry per audit_log row: timestamp, actor,
// decision, and -- the point of this whole screen -- the plain-language
// REASONING, made the visual focus (largest, brightest text in each row).
// GATE_BLOCK rows go `lost` red with the gate id; GATE_ALLOW go `recovered`
// green; every event type gets its own tone+icon (see lib/auditEvents.js)
// so ACTED / OUTREACH_SENT / ESCALATED read as distinct at a glance.
export default function AuditTrail({ rows }) {
  if (!rows || rows.length === 0) {
    return (
      <div className="rounded-lg border border-line bg-panel p-8 text-center text-sm text-muted">
        No audit trail yet for this case.
      </div>
    )
  }

  const ordered = [...rows].sort((a, b) => new Date(a.ts) - new Date(b.ts))

  return (
    <ol className="relative flex flex-col gap-5 pl-2">
      {ordered.map((row, i) => (
        <TrailEntry key={row.id ?? i} row={row} isLast={i === ordered.length - 1} />
      ))}
    </ol>
  )
}

function TrailEntry({ row, isLast }) {
  const meta = eventMeta(row.event_type)
  const tone = toneClasses(meta.tone)
  const Icon = meta.icon
  const gateId = row.event_type === 'GATE_BLOCK' || row.event_type === 'GATE_ALLOW'
    ? gateIdFromDecision(row.decision)
    : null
  const hasDetails = row.input || row.result

  return (
    <li className="relative flex gap-4">
      {!isLast && <span className="absolute left-[15px] top-8 bottom-[-20px] w-px bg-line" aria-hidden="true" />}

      <span className={`z-10 flex h-8 w-8 shrink-0 items-center justify-center rounded-full border ${tone.border} ${tone.bg}`}>
        <Icon className={`h-4 w-4 ${tone.text}`} strokeWidth={2} />
      </span>

      <div className={`flex-1 rounded-lg border bg-panel p-3 ${tone.border}`}>
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
          <div className="flex items-center gap-2">
            <span className={`text-xs font-semibold uppercase tracking-wide ${tone.text}`}>{meta.label}</span>
            {gateId && (
              <span className={`rounded border px-1.5 py-0.5 font-mono text-[11px] font-semibold ${tone.border} ${tone.bg} ${tone.text}`}>
                {gateId}
              </span>
            )}
            {row.decision && !gateId && (
              <span className="rounded bg-line px-1.5 py-0.5 font-mono text-[11px] text-muted">{row.decision}</span>
            )}
          </div>
          <span className="font-mono text-xs text-muted" title={row.ts}>{formatDateTime(row.ts)}</span>
        </div>

        {row.reasoning && (
          <p className="mt-2 text-[15px] leading-snug text-white">{row.reasoning}</p>
        )}

        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
          <span className="font-mono">{row.actor}</span>
          {row.action && <span className="font-mono">→ {row.action}</span>}
        </div>

        {hasDetails && (
          <details className="mt-2">
            <summary className="cursor-pointer select-none text-xs text-muted hover:text-white">
              raw details
            </summary>
            <pre className="mt-1 overflow-x-auto rounded bg-ink p-2 font-mono text-[11px] text-muted">
              {JSON.stringify({ input: row.input, result: row.result }, null, 2)}
            </pre>
          </details>
        )}
      </div>
    </li>
  )
}
