"""
Feature D -- promise-to-pay (PTP) lifecycle (PRD.md §8).

record_promise(): on a promise_to_pay intent, create the promise, move the case
to PROMISE_MADE, and schedule the retry for the promised date (plus a gentle
reminder the morning of).

resolve_due_promises(): on the promised date, re-enter the pipeline.
  paid  -> status 'kept',   case RECOVERED
  not   -> status 'broken', escalate

RULES:
- FR-D3: a promise PAUSES other outreach until its date -- that's gate G10
  (app/governance/policy_gate.py), which reads has_active_promise from
  repository.active_promise(). This module's only job is to keep that table's
  status column honest; it does not reimplement G10.
- FR-D5: a promised date beyond policy["max_promise_horizon_days"] is capped
  and flagged, never silently accepted.
- PRD §13 item 23: a broken promise gets exactly one second chance -- a case
  whose promises have already broken twice gets no more; record_promise
  refuses to create a third and escalates instead.
- PRD §13 item 24: a case that pays before its promised date is handled by
  mark_kept_early() -- it marks the still-pending promise kept immediately
  and cancels the wait, rather than leaving it to be swept up (and wrongly
  called "broken") whenever its date eventually arrives.
- kept_promise_rate is a reported metric -- keep the statuses honest.

LATENT BOUNDARY: this module never reads case["latent"] -- only
app/detection/batch_scanner.py may. resolve_due_promises() cannot itself know
whether a customer actually paid (in the simulated batch that's hidden ground
truth; in production it's a live payment/webhook check) -- the caller supplies
that verdict per promise via the `is_paid` callback. This module only owns the
mechanical lifecycle: capping, status transitions, case-state transitions, the
second-chance count, and the audit trail.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable

from app import config
from app.audit import log as audit_log
from app.db import repository

_ACTOR = "promises.tracker"

SOURCES = ("voice", "text", "inferred")

# PRD §13 item 23: one second chance after a broken promise, not unlimited.
# Once a case has this many broken promises on record, record_promise refuses
# to create another and escalates instead.
_MAX_BROKEN_PROMISES = 2


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()


def _reminder_at(promised: date) -> datetime:
    """FR-D2: "a gentle morning reminder" on the promised date itself."""
    return datetime.combine(promised, time(9, 0), tzinfo=timezone.utc)


def _broken_count(case_id: str) -> int:
    history = repository.promises_for_case(case_id)
    return sum(1 for p in history if p.get("status") == "broken")


def record_promise(
    case_id: str,
    amount: float,
    promised_date: date | datetime | str,
    *,
    source: str,
    policy: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """
    FR-D1/D2/D5: create a promise and move the case to PROMISE_MADE.

    source: "voice" | "text" | "inferred" -- which channel the promise came
    from. Recorded on the promise row so metrics can report how many
    promises originated from the voice channel.

    Caps promised_date at policy["max_promise_horizon_days"] (default from
    config.DEFAULT_POLICY), flagging when it did -- FR-D5, never a silent
    truncation.

    PRD §13 item 23: if this case already has _MAX_BROKEN_PROMISES broken
    promises on record, refuses to create another -- escalates the case
    instead and returns without a promise. (A single broken promise still
    gets one more try; that's the "allowed once" the item describes.)

    Returns:
      {"created": bool, "escalated": bool, "promise": dict | None,
       "capped": bool, "reminder_at": str | None, "reasoning": str}
    """
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}, got {source!r}")

    policy = policy or config.DEFAULT_POLICY
    now = now or datetime.now(timezone.utc)
    today = now.date()

    broken = _broken_count(case_id)
    if broken >= _MAX_BROKEN_PROMISES:
        reasoning = (
            f"Customer has already broken {broken} promises on this case. Policy "
            "allows one second chance, not unlimited ones -- escalating instead of "
            "recording a promise we have no reason to trust."
        )
        repository.update_case(case_id, state="ESCALATED")
        audit_log.record(
            case_id, _ACTOR, audit_log.ESCALATED,
            inp={"source": source, "broken_promise_count": broken},
            decision="escalate",
            reasoning=reasoning,
        )
        return {
            "created": False, "escalated": True, "promise": None,
            "capped": False, "reminder_at": None, "reasoning": reasoning,
        }

    requested = _as_date(promised_date)
    horizon_days = int(policy.get("max_promise_horizon_days", 14))
    latest_allowed = today + timedelta(days=horizon_days)
    capped = requested > latest_allowed
    final_date = min(requested, latest_allowed)

    promise = repository.insert_promise({
        "case_id": case_id,
        "promised_amount": float(amount),
        "promised_date": final_date.isoformat(),
        "status": "pending",
        "source": source,
    })
    repository.update_case(case_id, state="PROMISE_MADE")

    reminder_at = _reminder_at(final_date)
    reasoning = f"Customer ({source}) promised to pay by {final_date.isoformat()}."
    if capped:
        reasoning += (
            f" Requested date {requested.isoformat()} was beyond the "
            f"{horizon_days}-day promise horizon; capped to {final_date.isoformat()}."
        )

    audit_log.record(
        case_id, _ACTOR, audit_log.PROMISE_MADE,
        inp={"source": source, "requested_date": requested.isoformat(), "amount": amount},
        decision="promise_to_pay",
        reasoning=reasoning,
        result={"promise": promise, "capped": capped, "reminder_at": reminder_at.isoformat()},
    )

    return {
        "created": True, "escalated": False, "promise": promise,
        "capped": capped, "reminder_at": reminder_at.isoformat(), "reasoning": reasoning,
    }


def mark_kept_early(case_id: str, *, now: datetime | None = None) -> dict | None:
    """
    PRD §13 item 24: the customer paid before their promised date arrived.
    Marks the still-pending promise 'kept' immediately and cancels the wait
    (repository.due_promises() only sees status='pending' rows, so once this
    runs the promise stops being "due" at all -- there's nothing left to
    later, wrongly, sweep up as broken) and recovers the case right now
    rather than waiting for the promised date to confirm what's already true.

    Returns None (nothing to do) if the case has no active pending promise.
    """
    promise = repository.active_promise(case_id)
    if promise is None:
        return None

    now = now or datetime.now(timezone.utc)
    amount = float(promise.get("promised_amount") or 0)

    repository.resolve_promise(promise["id"], "kept")
    repository.mark_recovered(case_id, amount)

    audit_log.record(
        case_id, _ACTOR, audit_log.PROMISE_KEPT,
        reasoning=(
            f"Customer paid before the promised date ({promise.get('promised_date')}). "
            "Marking the promise kept early and cancelling the scheduled retry."
        ),
    )
    audit_log.record(case_id, _ACTOR, audit_log.RECOVERED, reasoning="Promise kept early; case recovered.")

    return {"case_id": case_id, "promise_id": promise["id"], "status": "kept", "case_state": "RECOVERED"}


def resolve_due_promises(
    today: date | datetime | str,
    policy: dict,
    *,
    is_paid: Callable[[dict], bool] | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """
    FR-D4: for every promise whose promised_date has arrived (<= today),
    decide kept vs broken and apply the consequence.

    is_paid(promise) -> bool tells the tracker whether THIS promise was kept.
    Answering that needs information this module must never touch itself:
    case["latent"] in the simulated batch (only app/detection/batch_scanner.py
    may read it), or a real payment/webhook check in production -- the caller
    supplies the verdict. Defaults to checking whether the case is already
    RECOVERED (e.g. via mark_kept_early(), or some other path), a reasonable
    fallback when the caller has no richer signal.

    Returns one result dict per promise resolved:
      {"case_id", "promise_id", "status": "kept" | "broken", "case_state"}
    """
    if is_paid is None:
        def is_paid(promise: dict) -> bool:
            case = repository.get_case(promise.get("case_id"))
            return bool(case and case.get("state") == "RECOVERED")

    on_date = _as_date(today).isoformat() if not isinstance(today, str) else today
    results: list[dict] = []

    for promise in repository.due_promises(on_date):
        case_id = promise.get("case_id")
        promise_id = promise.get("id")
        paid = bool(is_paid(promise))

        if paid:
            amount = float(promise.get("promised_amount") or 0)
            repository.resolve_promise(promise_id, "kept")
            repository.mark_recovered(case_id, amount)
            audit_log.record(case_id, _ACTOR, audit_log.PROMISE_KEPT, reasoning="Customer paid as promised.")
            audit_log.record(case_id, _ACTOR, audit_log.RECOVERED, reasoning="Promise kept; case recovered.")
            results.append({"case_id": case_id, "promise_id": promise_id, "status": "kept", "case_state": "RECOVERED"})
        else:
            repository.resolve_promise(promise_id, "broken")
            audit_log.record(case_id, _ACTOR, audit_log.PROMISE_BROKEN, reasoning="Promised date passed with no payment.")
            repository.update_case(case_id, state="ESCALATED")
            audit_log.record(
                case_id, _ACTOR, audit_log.ESCALATED,
                reasoning="Broken promise. Escalating rather than chasing indefinitely.",
            )
            results.append({"case_id": case_id, "promise_id": promise_id, "status": "broken", "case_state": "ESCALATED"})

    return results
