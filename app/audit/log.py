"""
Append-only audit log -- the explainability backbone.

Every stage writes exactly one row: what it saw, what it decided, WHY, what it
did, and what happened. The Postgres trigger blocks UPDATE/DELETE, so history
cannot be rewritten.

Event types:
  DETECTED | DIAGNOSED | DECIDED | GATE_ALLOW | GATE_BLOCK | ACTED |
  OUTREACH_SENT | REPLY_RECEIVED | PROMISE_MADE | PROMISE_KEPT |
  PROMISE_BROKEN | RECOVERED | ESCALATED | CLOSED_LOST | ERROR

`reasoning` must be plain-language and specific enough that a human can read one
case's trail top-to-bottom and understand every rupee that moved and why.
"""
from __future__ import annotations

# TODO: def record(case_id, actor, event_type, *, inp=None, decision=None,
#                  reasoning=None, action=None, result=None) -> None
# TODO: def trail(case_id: str) -> list[dict]   # ordered audit rows for one case
