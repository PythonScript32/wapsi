"""
Feature D -- promise-to-pay (PTP) lifecycle.

record_promise(): on a promise_to_pay intent, create the promise, move the case
to PROMISE_MADE, and schedule the retry for the promised date (plus a gentle
reminder the morning of).

resolve_due_promises(): on the promised date, re-enter the pipeline.
  paid  -> status 'kept',   case RECOVERED
  not   -> status 'broken', escalate per policy (one more touch, then ESCALATED)

RULES:
- A promise PAUSES other outreach until its date. Chasing someone who already
  promised is the fastest way to lose them.
- A promised date beyond grace_period_days is capped and flagged.
- kept_promise_rate is a reported metric -- keep the statuses honest.
"""
from __future__ import annotations

# TODO: def record_promise(case_id: str, amount: float, promised_date: str) -> dict
# TODO: def resolve_due_promises(today: str) -> list[dict]
