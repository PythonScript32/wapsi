// Case state machine + reason category metadata, shared by every component
// that renders a case. Order here IS column order on the Pipeline board.
// Mirrors app/db/repository.py's TERMINAL_STATES and the case_state enum in
// supabase/migrations/001_initial_schema.sql -- keep both in sync by hand,
// there's no shared source of truth across Python and JS.

export const CASE_STATES = [
  'DETECTED',
  'DIAGNOSED',
  'SCHEDULED',
  'OUTREACH_SENT',
  'AWAITING_RESPONSE',
  'PROMISE_MADE',
  'RETRYING',
  'RECOVERED',
  'ESCALATED',
  'CLOSED_LOST',
]

export const TERMINAL_STATES = new Set(['RECOVERED', 'ESCALATED', 'CLOSED_LOST'])

const STATE_LABELS = {
  DETECTED: 'Detected',
  DIAGNOSED: 'Diagnosed',
  SCHEDULED: 'Scheduled',
  OUTREACH_SENT: 'Outreach Sent',
  AWAITING_RESPONSE: 'Awaiting Response',
  PROMISE_MADE: 'Promise Made',
  RETRYING: 'Retrying',
  RECOVERED: 'Recovered',
  ESCALATED: 'Escalated',
  CLOSED_LOST: 'Closed Lost',
}

export function stateLabel(state) {
  return STATE_LABELS[state] || state
}

// A column's accent tone, using ONLY tailwind.config.js's design tokens --
// reads left to right as the story of a case: new -> being worked -> waiting
// on the customer -> retried -> resolved (green) or not (red).
const STATE_TONES = {
  DETECTED: 'muted',
  DIAGNOSED: 'muted',
  SCHEDULED: 'accent',
  OUTREACH_SENT: 'accent',
  AWAITING_RESPONSE: 'accent',
  PROMISE_MADE: 'promise',
  RETRYING: 'atrisk',
  RECOVERED: 'recovered',
  ESCALATED: 'lost',
  CLOSED_LOST: 'lost',
}

export function stateTone(state) {
  return STATE_TONES[state] || 'muted'
}

const REASON_LABELS = {
  insufficient_funds: 'Insufficient Funds',
  expired_card: 'Expired Card',
  mandate_revoked: 'Mandate Revoked',
  bank_downtime: 'Bank Downtime',
  technical_other: 'Technical Error',
  checkout_dropoff: 'Checkout Drop-off',
}

export function reasonLabel(reason) {
  return REASON_LABELS[reason] || (reason ? reason.replaceAll('_', ' ') : 'Unclassified')
}

// One tone per reason, using only the design tokens -- "lost" is reused for
// expired_card/mandate_revoked (both are dead-payment-method blockers), the
// only repeat needed to cover 6 reasons with 5 semantic tokens + muted.
const REASON_TONES = {
  insufficient_funds: 'atrisk',
  expired_card: 'lost',
  mandate_revoked: 'lost',
  bank_downtime: 'accent',
  technical_other: 'muted',
  checkout_dropoff: 'promise',
}

export function reasonTone(reason) {
  return REASON_TONES[reason] || 'muted'
}
