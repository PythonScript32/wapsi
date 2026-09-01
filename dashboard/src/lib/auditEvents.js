// Audit-log event metadata, shared by AuditTrail and MessageThread. Mirrors
// app/audit/log.py's EVENT_TYPES -- keep both in sync by hand.
import {
  AlertOctagon, AlertTriangle, BadgeCheck, Brain, CalendarClock, CheckCircle2,
  MessageSquare, Radar, Send, ShieldAlert, ShieldCheck, Stethoscope, XCircle, Zap,
} from 'lucide-react'

// One tone + icon per event type, using ONLY tailwind.config.js's tokens.
// GATE_BLOCK=lost, GATE_ALLOW=recovered are the two PRD calls out by name;
// ACTED / OUTREACH_SENT / ESCALATED each get a tone+icon combo none of the
// others share, so they read as visually distinct at a glance.
const EVENT_META = {
  DETECTED: { tone: 'muted', icon: Radar, label: 'Detected' },
  DIAGNOSED: { tone: 'muted', icon: Stethoscope, label: 'Diagnosed' },
  DECIDED: { tone: 'accent', icon: Brain, label: 'Decided' },
  GATE_ALLOW: { tone: 'recovered', icon: ShieldCheck, label: 'Gate allowed' },
  GATE_BLOCK: { tone: 'lost', icon: ShieldAlert, label: 'Gate blocked' },
  ACTED: { tone: 'accent', icon: Zap, label: 'Action executed' },
  OUTREACH_SENT: { tone: 'promise', icon: Send, label: 'Outreach sent' },
  REPLY_RECEIVED: { tone: 'promise', icon: MessageSquare, label: 'Reply received' },
  PROMISE_MADE: { tone: 'promise', icon: CalendarClock, label: 'Promise made' },
  PROMISE_KEPT: { tone: 'recovered', icon: CheckCircle2, label: 'Promise kept' },
  PROMISE_BROKEN: { tone: 'lost', icon: XCircle, label: 'Promise broken' },
  RECOVERED: { tone: 'recovered', icon: BadgeCheck, label: 'Recovered' },
  ESCALATED: { tone: 'atrisk', icon: AlertTriangle, label: 'Escalated' },
  CLOSED_LOST: { tone: 'lost', icon: XCircle, label: 'Closed lost' },
  ERROR: { tone: 'lost', icon: AlertOctagon, label: 'Error' },
}

const DEFAULT_META = { tone: 'muted', icon: Radar, label: 'Event' }

export function eventMeta(eventType) {
  return EVENT_META[eventType] || { ...DEFAULT_META, label: eventType || 'Event' }
}

// audit_log.gate() writes decision="BLOCK (G3)" / "ALLOW" -- pull the gate
// id back out for its own badge (mirrors app/metrics/compute.py's
// _gate_id_from_decision).
export function gateIdFromDecision(decision) {
  const m = /\(([^)]+)\)/.exec(decision || '')
  return m ? m[1] : null
}

// A REPLY_RECEIVED row's transcript, whichever shape produced it:
//  - real voice/text replies (app/voice/inbound.py) put it in result.transcript
//  - simulated batch replies (app/detection/batch_scanner.py) put the raw
//    text in input.text instead
export function replyTranscript(row) {
  return row.result?.transcript ?? row.input?.text ?? null
}

// voice/inbound.py tags {channel: "voice"|"text"} on input; simulated
// replies carry no channel at all -- treat those as text.
export function replyChannel(row) {
  return row.input?.channel === 'voice' ? 'voice' : 'text'
}
