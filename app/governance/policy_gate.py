"""
Governance layer -- wraps EVERY money/outreach action.
This is the module that wins the track: it makes actions bounded and gated.

check() answers one question: may this action fire, right now, for this case?

BOUNDS (from config.DEFAULT_POLICY):
  max_retries, max_discount_pct, max_exposure_inr, min_contact_gap_hours,
  grace_period_days, rbi_pre_debit_notice_hours

GATES (each returns allow/block + a plain-language reason):
  G1 already recovered?          -> block (never charge a paid customer)
  G2 opted out?                  -> block (opt-out is sacred)
  G3 attempts >= max for reason? -> block, escalate instead
  G4 inside min_contact_gap?     -> block (anti-harassment)
  G5 past grace period?          -> block, close as CLOSED_LOST
  G6 discount > max_discount_pct?-> block
  G7 amount > max_exposure_inr?  -> block, require human approval
  G8 idempotency key present?    -> block if missing (never double-charge)
  G9 RBI pre-debit notice sent >= 24h ago for a mandate debit? -> else block

EVERY call writes GATE_ALLOW or GATE_BLOCK to the audit log with the reason.
Fail closed: if a check cannot be evaluated, BLOCK.
"""
from __future__ import annotations

# TODO: def check(action: dict, case: dict, policy: dict) -> dict
#       -> {"allowed": bool, "reason": str, "gate": str | None}
