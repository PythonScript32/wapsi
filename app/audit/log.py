"""
Append-only audit log — the explainability backbone.

The track bar reads "every money action explainable, bounded and gated" and
"show the audit trail". policy_gate.py handles bounded-and-gated. This handles
explainable.

THE STANDARD TO WRITE TO
------------------------
A human should be able to read one case's trail top to bottom and understand
every rupee that moved and why — without reading any code. That means
`reasoning` is written for a person, not a log parser:

    good: "Insufficient funds. Customer's salary lands on the 1st, so retrying
           today would fail again. Scheduled for 2 Sep."
    bad:  "strategy=after_salary_day sched=2026-09-02"

IMMUTABILITY IS ENFORCED BY THE DATABASE
----------------------------------------
A Postgres trigger on audit_log raises on any UPDATE or DELETE. History is
physically immutable, not immutable-by-convention. That distinction is the
difference between an audit trail and a log file, and it is worth saying out
loud when you walk someone through the architecture.

FAILURE POLICY
--------------
Audit writes fail SOFT: a logging outage must never block a recovery. But a
failed write is never silent — it goes to stderr so it surfaces in the uvicorn
console and in any deployment's logs.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any

from app.db import repository

# ---------------------------------------------------------------------------
# Event types — the vocabulary of the trail. Keep this list closed; a typo'd
# event type is a hole in the audit story.
# ---------------------------------------------------------------------------
DETECTED = "DETECTED"
DIAGNOSED = "DIAGNOSED"
DECIDED = "DECIDED"
GATE_ALLOW = "GATE_ALLOW"
GATE_BLOCK = "GATE_BLOCK"
ACTED = "ACTED"
OUTREACH_SENT = "OUTREACH_SENT"
REPLY_RECEIVED = "REPLY_RECEIVED"
PROMISE_MADE = "PROMISE_MADE"
PROMISE_KEPT = "PROMISE_KEPT"
PROMISE_BROKEN = "PROMISE_BROKEN"
RECOVERED = "RECOVERED"
ESCALATED = "ESCALATED"
CLOSED_LOST = "CLOSED_LOST"
ERROR = "ERROR"

EVENT_TYPES = {
    DETECTED, DIAGNOSED, DECIDED, GATE_ALLOW, GATE_BLOCK, ACTED,
    OUTREACH_SENT, REPLY_RECEIVED, PROMISE_MADE, PROMISE_KEPT,
    PROMISE_BROKEN, RECOVERED, ESCALATED, CLOSED_LOST, ERROR,
}


def _jsonable(value: Any) -> Any:
    """Make a value safe to store in a jsonb column."""
    if value is None:
        return None
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return {"repr": repr(value)}


def record(
    case_id: str | None,
    actor: str,
    event_type: str,
    *,
    inp: Any = None,
    decision: str | None = None,
    reasoning: str | None = None,
    action: str | None = None,
    result: Any = None,
) -> None:
    """
    Append one row to the audit log.

    case_id    — the case this concerns (None only for system-wide events)
    actor      — which component or model acted, e.g. "diagnosis.classifier"
                 or "decision.engine:llama-3.3-70b". Naming the model matters:
                 when a decision looks wrong six weeks later, you need to know
                 what produced it.
    event_type — one of the constants above
    reasoning  — WHY, in plain language, for a human reader
    """
    if event_type not in EVENT_TYPES:
        print(f"[audit] WARNING unknown event_type {event_type!r}", file=sys.stderr)

    row = {
        "case_id": case_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "actor": actor,
        "event_type": event_type,
        "input": _jsonable(inp),
        "decision": decision,
        "reasoning": reasoning,
        "action": action,
        "result": _jsonable(result),
    }

    try:
        repository.append_audit(row)
    except Exception as exc:  # fail soft, never silent
        print(
            f"[audit] FAILED to persist {event_type} for case={case_id}: {exc}\n"
            f"[audit] row={row}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Convenience wrappers — use these so call sites stay short and consistent.
# ---------------------------------------------------------------------------

def gate(case_id: str, gate_result: Any, action: dict, actor: str = "governance.policy_gate") -> None:
    """
    Log a governance verdict. Call this on EVERY check, allow or block.

    Logging the allows matters as much as the blocks: it is what proves the gate
    actually ran, rather than being skipped on the happy path.
    """
    record(
        case_id,
        actor,
        GATE_ALLOW if gate_result.allowed else GATE_BLOCK,
        inp={
            "action_type": action.get("type"),
            "intervention": action.get("intervention"),
            "amount": action.get("amount"),
            "discount_pct": action.get("discount_pct"),
        },
        decision="ALLOW" if gate_result.allowed else f"BLOCK ({gate_result.gate})",
        reasoning=gate_result.reason,
    )


def money_action(
    case_id: str,
    intervention: str,
    amount: float,
    idempotency_key: str,
    result: Any,
    reasoning: str,
    actor: str = "execution.actions",
) -> None:
    """Log an executed money action. The idempotency key is recorded so a
    duplicate can be traced back to the original attempt."""
    record(
        case_id,
        actor,
        ACTED,
        inp={"intervention": intervention, "amount": amount,
             "idempotency_key": idempotency_key},
        decision=intervention,
        reasoning=reasoning,
        action=f"{intervention} Rs {amount:,.2f}",
        result=result,
    )


def error(case_id: str | None, actor: str, what: str, exc: Exception) -> None:
    """Log a failure. Every escalation should have one of these behind it."""
    record(
        case_id,
        actor,
        ERROR,
        reasoning=f"{what}: {type(exc).__name__}: {exc}",
        result={"error_type": type(exc).__name__, "error": str(exc)},
    )


def trail(case_id: str) -> list[dict]:
    """The full ordered history for one case — what the dashboard renders."""
    return repository.audit_for_case(case_id)


def print_trail(case_id: str) -> None:
    """Human-readable trail in the terminal. Useful while building, and a fast
    way to sanity-check a case during a demo."""
    rows = trail(case_id)
    if not rows:
        print(f"No audit entries for {case_id}")
        return

    print(f"\n=== Audit trail: {case_id} ===")
    for r in rows:
        ts = str(r.get("ts", ""))[:19].replace("T", " ")
        print(f"\n[{ts}] {r.get('event_type')}  ({r.get('actor')})")
        if r.get("decision"):
            print(f"  decision : {r['decision']}")
        if r.get("reasoning"):
            print(f"  why      : {r['reasoning']}")
        if r.get("action"):
            print(f"  action   : {r['action']}")
    print()
