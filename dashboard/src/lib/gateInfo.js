// Plain-English description per governance gate, mirroring the comments in
// app/governance/policy_gate.py's check() -- keep both in sync by hand.
export const GATE_INFO = {
  G1: 'Never touch a case that is already resolved.',
  G2: 'Opt-out is permanent — all contact and collection stops for good.',
  G3: 'Per-reason attempt cap — escalate to a human instead of retrying forever.',
  G4: 'Minimum 24h gap between contacts, so outreach never reads as harassment.',
  G5: 'Grace period — close the case as lost after too many days with no resolution.',
  G6: 'Discount cap — never offer more margin away than policy allows.',
  G7: 'Exposure cap — amounts above the limit require human approval.',
  G8: 'Idempotency key required for every money action — never risk a double charge.',
  G9: 'RBI pre-debit notice must age before a mandate debit is allowed to fire.',
  G10: 'An active promise-to-pay pauses every other action until its date.',
}

export function gateDescription(gateId) {
  return GATE_INFO[gateId] || 'Governance gate.'
}
