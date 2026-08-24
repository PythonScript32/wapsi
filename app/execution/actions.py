"""
Action executor: turns an ALLOWED Decision into a real effect.

Flow for EVERY action:
  1. governance.policy_gate.check(...)  -> must return allowed=True
  2. build/lookup the idempotency key   -> f"{case_id}:{intervention}:{attempt_no}"
  3. execute (Razorpay call, or 'send' outreach = simulate + persist the message)
  4. persist payment_attempt / outreach row
  5. audit.log.record(...) with the result
  6. advance the case state

GRACEFUL FAILURE (the demo showpiece):
  Razorpay timeout/5xx -> exponential backoff (e.g. 1s, 4s, 10s) ->
  still failing -> mark the attempt 'pending', do NOT retry blindly, escalate to
  the human queue WITH the full case context, and audit it as ERROR + ESCALATED.
  Never silently drop. Never double-charge (the idempotency key protects us even
  if the first call actually succeeded server-side).
"""
from __future__ import annotations

# TODO: def execute(decision: dict, case: dict, policy: dict) -> dict
# TODO: def _with_backoff(fn, *, attempts=3)
