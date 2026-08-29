"""
Action executor: turns an ALLOWED Decision into a real effect.

Flow for EVERY action:
  1. Gather context: repository.gate_context(case_id)
  2. Build the action dict, including the idempotency key
     f"{case_id}:{intervention}:{attempt_no}"
  3. governance.policy_gate.check(...)
  4. audit.log.gate(...)                — log EVERY verdict, allow or block
  5. If blocked: translate G3/G5 into a terminal outcome, retry G6 without
     the discount, or (any other gate) return without touching money,
     outreach, or state
  6. If allowed: persist the money attempt and/or the outreach message,
     execute (Razorpay call, or 'send' outreach = simulate + persist)
  7. repository.increment_attempts(case_id)
  8. audit.log.money_action(...) / audit.log.record(...OUTREACH_SENT...)
  9. Advance the case state

THE GATE IS THE SOLE AUTHORITY ON BOUNDS
------------------------------------------
app/decision/engine.py proposes the intervention a reason category WANTS,
with no attempt-cap, grace-period, or discount-cap check of its own — those
three bounds are enforced here, at the gate, exactly once:
  G3 (attempt cap)    -> ESCALATED
  G5 (grace period)   -> CLOSED_LOST
  G6 (discount cap)   -> re-propose the same action with discount_pct=0;
                         escalate if even that doesn't clear the gate
Any other block (G1, G2, G4, G7, G8, G9, G10) just reports "blocked" — the
caller (app/detection/batch_scanner.py) decides whether and when to retry.

THE PRE-DEBIT NOTICE (RBI compliance, gate G9)
-----------------------------------------------
G9 blocks any mandate debit that isn't preceded by a pre-debit notice sent at
least rbi_pre_debit_notice_hours earlier. So a decision with
is_mandate_debit=True doesn't go straight through the flow above: the first
call sends the notice (its own governed outreach action, channel
"pre_debit_notice"); every call before the notice has aged just waits; only
once it has aged does the charge itself reach step 2 onward. This is what
keeps G9 from blocking most of a mandate-debit batch outright.

GRACEFUL FAILURE (the demo showpiece)
--------------------------------------
Razorpay timeout/5xx -> exponential backoff (1s, 4s, 10s) -> still failing ->
mark the attempt 'pending' (NOT 'failed' — we don't actually know), audit
ERROR + ESCALATED with full case context, and set the case to ESCALATED.
Never silently drop. Never double-charge: the idempotency key protects us
even if an earlier call actually succeeded server-side, since a duplicate
key is resolved to the existing attempt before any new call is made.

Never reads case["latent"] — execution decides blind, exactly as it would in
production. Only app/detection/batch_scanner.py may read that field.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.audit import log as audit_log
from app.db import repository
from app.execution import razorpay_client
from app.governance.policy_gate import GateResult, MONEY_ACTIONS, OUTREACH_ACTIONS
from app.governance.policy_gate import check as gate_check

_ACTOR = "execution.actions"

# decision.intervention -> the action "type" policy_gate.check() judges it as.
# "escalate" / "close_lost" are handled before this lookup — they never touch
# money or outreach, so they never touch the gate.
_ACTION_TYPES: dict[str, str] = {
    "retry_now": "charge",
    "retry_after_date": "charge",
    "request_re_mandate": "outreach",
    "request_card_update": "outreach",
    "send_link": "outreach",
    "send_link_with_offer": "offer",   # both money (discount, exposure) and outreach (the message)
}

# decision.intervention -> case state once the action has been executed
# (allowed by the gate and persisted). Not every intervention advances state.
_NEXT_STATE: dict[str, str] = {
    "retry_now": "RETRYING",
    "retry_after_date": "RETRYING",
    "request_re_mandate": "OUTREACH_SENT",
    "request_card_update": "OUTREACH_SENT",
    "send_link": "OUTREACH_SENT",
    "send_link_with_offer": "OUTREACH_SENT",
}


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt_amount(amount: Any) -> str:
    try:
        return f"{float(amount):,.0f}"
    except (TypeError, ValueError):
        return "0"


def _fmt_date(dt: datetime) -> str:
    return dt.strftime("%d %b")


def _customer_payload(case: dict) -> dict:
    return {"name": case.get("customer_ref"), "contact": case.get("customer_phone")}


def _purpose_for(case: dict) -> str:
    return "subscription renewal" if case.get("source") == "subscription" else "checkout completion"


_NO_DISCOUNT_FALLBACK_MESSAGE = (
    "Namaste {name}, yeh raha Rs {amount} ka aapka payment link, ek click mein pay kar dein."
)


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def execute(decision: dict, case: dict, policy: dict, *, live: bool = False, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    case_id = case["id"]
    intervention = decision["intervention"]

    if intervention == "escalate":
        return _apply_terminal(case_id, "ESCALATED", audit_log.ESCALATED, decision)
    if intervention == "close_lost":
        return _apply_terminal(case_id, "CLOSED_LOST", audit_log.CLOSED_LOST, decision)

    # step 1
    context = repository.gate_context(case_id)

    if decision.get("is_mandate_debit"):
        deferred = _ensure_pre_debit_notice(case, decision, policy, context, now)
        if deferred is not None:
            return deferred
        context = repository.gate_context(case_id)  # refresh: notice_at now set

    attempt_no = int(case.get("attempts_made") or 0) + 1
    action_type = _ACTION_TYPES.get(intervention, "outreach")

    # step 2
    action = {
        "type": action_type,
        "intervention": intervention,
        "idempotency_key": f"{case_id}:{intervention}:{attempt_no}",
        "discount_pct": decision.get("discount_pct", 0.0),
        "amount": case.get("amount"),
        "is_mandate_debit": bool(decision.get("is_mandate_debit", False)),
    }

    # step 3
    gate = gate_check(action, case, policy, now=now, **context)
    # step 4
    audit_log.gate(case_id, gate, action)
    # step 5
    if not gate.allowed:
        if gate.gate == "G6":
            retried = _retry_without_discount(action, case, decision, policy, context, now, live)
            if retried is not None:
                return retried
            result = _apply_terminal(
                case_id, "ESCALATED", audit_log.ESCALATED,
                {
                    "intervention": "escalate",
                    "reasoning": f"Discount still blocked even with it stripped to 0: {gate.reason}",
                },
            )
            result["gate"] = "G6"
            return result
        terminal = _translate_gate_block(gate, case_id)
        if terminal is not None:
            return terminal
        return {"executed": False, "gate": gate.gate, "reason": gate.reason}

    # step 6-9
    return _execute_allowed(action, case, decision, policy, live=live, now=now)


def _translate_gate_block(gate: GateResult, case_id: str) -> dict | None:
    """
    The gate is the sole authority on the attempt cap (G3) and the grace
    period (G5) — app/decision/engine.py proposes without checking either.
    A block on one of those bounds IS the terminal outcome, so it's
    translated directly instead of leaving the case dangling in limbo.
    Returns None for every other gate: the caller just reports the block.

    The returned result still carries "gate": gate.gate (overriding
    _apply_terminal's default None) — a caller counting gate_block_counts
    must see G3/G5 fired here, not just "executed: True". Losing that would
    make the gate's own enforcement invisible again, the exact problem this
    whole restructure exists to fix.
    """
    if gate.gate == "G3":
        result = _apply_terminal(
            case_id, "ESCALATED", audit_log.ESCALATED,
            {"intervention": "escalate", "reasoning": gate.reason},
        )
        result["gate"] = gate.gate
        return result
    if gate.gate == "G5":
        result = _apply_terminal(
            case_id, "CLOSED_LOST", audit_log.CLOSED_LOST,
            {"intervention": "close_lost", "reasoning": gate.reason},
        )
        result["gate"] = gate.gate
        return result
    return None


def _retry_without_discount(
    action: dict, case: dict, decision: dict, policy: dict, context: dict, now: datetime, live: bool,
) -> dict | None:
    """
    G6 block: the proposed discount alone exceeded policy's cap (e.g. a
    merchant override tighter than the engine's default offer). Re-propose
    the exact same action with the discount stripped to 0 rather than giving
    up outright — a plain link is still worth sending. Returns None if the
    zero-discount version doesn't clear the gate either, so the caller
    escalates instead.
    """
    case_id = case["id"]
    stripped_action = {**action, "discount_pct": 0.0}
    gate = gate_check(stripped_action, case, policy, now=now, **context)
    audit_log.gate(case_id, gate, stripped_action)
    if not gate.allowed:
        return None

    stripped_decision = {
        **decision,
        "discount_pct": 0.0,
        "message": _NO_DISCOUNT_FALLBACK_MESSAGE.format(
            name=case.get("customer_ref") or "Dost", amount=_fmt_amount(case.get("amount")),
        ),
    }
    return _execute_allowed(stripped_action, case, stripped_decision, policy, live=live, now=now)


def _execute_allowed(action: dict, case: dict, decision: dict, policy: dict, *, live: bool, now: datetime) -> dict:
    """
    Money and/or outreach side of an ALLOWED action: persist, execute, count
    the attempt, advance state. Shared by the normal path and the
    G6-retry-without-discount path — both have already cleared the gate by
    the time this runs.
    """
    case_id = case["id"]
    action_type = action["type"]
    intervention = action["intervention"]

    # step 6
    money_result = None
    reused = False
    if action_type in MONEY_ACTIONS:
        money_result = _execute_money(action, case, decision, policy, live=live, now=now)
        if money_result["escalated"]:
            # ESCALATED + ERROR already audited, case already updated, inside
            # _execute_money's failure path. Attempts are NOT incremented —
            # we never confirmed a try actually completed.
            return {
                "executed": False,
                "escalated": True,
                "intervention": intervention,
                "reason": money_result["reason"],
            }
        reused = bool(money_result.get("reused"))

    if reused:
        # This exact attempt already happened — same idempotency key, same
        # attempt_no. Nothing new to send, nothing new to count: re-sending
        # the message or re-incrementing attempts_made would just be a
        # duplicate contact / an inflated attempt count for one real try.
        return {"executed": True, "intervention": intervention, "gate": None, "reused": True, "money": money_result}

    outreach_result = None
    if action_type in OUTREACH_ACTIONS:
        outreach_result = _execute_outreach(action, case, decision, now)

    # step 7
    repository.increment_attempts(case_id)

    # step 9 (step 8 already logged inside _execute_money / _execute_outreach)
    new_state = _NEXT_STATE.get(intervention)
    if new_state:
        repository.update_case(case_id, state=new_state)

    return {
        "executed": True,
        "intervention": intervention,
        "gate": None,
        "money": money_result,
        "outreach": outreach_result,
    }


# ---------------------------------------------------------------------------
# pre-debit notice (RBI, gate G9)
# ---------------------------------------------------------------------------

def _ensure_pre_debit_notice(case: dict, decision: dict, policy: dict, context: dict, now: datetime) -> dict | None:
    """
    Returns a result dict to return immediately from execute() (notice just
    sent, or still aging), or None once the notice has aged enough that the
    real charge attempt should proceed.
    """
    notice_at = _parse_ts(context.get("pre_debit_notice_at"))
    notice_hours = int(policy.get("rbi_pre_debit_notice_hours", 24))

    if notice_at is None:
        return _send_pre_debit_notice(case, decision, policy, context, now)

    elapsed_hours = (now - notice_at).total_seconds() / 3600
    if elapsed_hours < notice_hours:
        retry_at = notice_at + timedelta(hours=notice_hours)
        return {
            "executed": False,
            "gate": None,
            "retry_at": retry_at.isoformat(),
            "reason": (
                f"Pre-debit notice sent {elapsed_hours:.1f}h ago; RBI requires "
                f"{notice_hours}h. Deferring the charge until it ages rather than "
                "attempting it early."
            ),
        }
    return None


def _send_pre_debit_notice(case: dict, decision: dict, policy: dict, context: dict, now: datetime) -> dict:
    case_id = case["id"]
    action = {
        "type": "outreach",
        "intervention": "pre_debit_notice",
        "idempotency_key": None,
        "discount_pct": 0.0,
        "amount": case.get("amount"),
        "is_mandate_debit": False,
    }
    gate = gate_check(action, case, policy, now=now, **context)
    audit_log.gate(case_id, gate, action)
    if not gate.allowed:
        # G3 can't apply to a plain outreach action, but G5 (grace period)
        # can — an aged-out case shouldn't even get its notice sent.
        terminal = _translate_gate_block(gate, case_id)
        if terminal is not None:
            return terminal
        return {"executed": False, "gate": gate.gate, "reason": gate.reason}

    message = _pre_debit_notice_message(case, decision, policy, now)
    row = repository.insert_outreach({
        "case_id": case_id,
        "channel": "pre_debit_notice",
        "direction": "outbound",
        "message": message,
        "sent_at": now.isoformat(),
    })

    notice_hours = int(policy.get("rbi_pre_debit_notice_hours", 24))
    audit_log.record(
        case_id,
        _ACTOR,
        audit_log.OUTREACH_SENT,
        inp={"channel": "pre_debit_notice"},
        decision="pre_debit_notice",
        reasoning=(
            "Sending the RBI-required pre-debit notice before attempting the mandate "
            f"charge. The charge itself waits until {notice_hours}h have passed."
        ),
        action="pre_debit_notice",
        result=row,
    )

    retry_at = now + timedelta(hours=notice_hours)
    return {
        "executed": True,
        "intervention": "pre_debit_notice",
        "gate": None,
        "retry_at": retry_at.isoformat(),
        "reason": "Pre-debit notice sent; the charge is deferred until it ages.",
    }


def _pre_debit_notice_message(case: dict, decision: dict, policy: dict, now: datetime) -> str:
    name = case.get("customer_ref") or "Dost"
    amount = _fmt_amount(case.get("amount"))
    scheduled = _parse_ts(decision.get("scheduled_for"))
    when = _fmt_date(scheduled) if scheduled else _fmt_date(now)
    return (
        f"Namaste {name}, aapka Rs {amount} ka payment {when} ko dobara try kiya "
        "jayega aapke registered mandate se. Agar koi dikkat ho ya aap ise rokna "
        "chahte hain, turant hume batayein."
    )


# ---------------------------------------------------------------------------
# money side
# ---------------------------------------------------------------------------

def _execute_money(action: dict, case: dict, decision: dict, policy: dict, *, live: bool, now: datetime) -> dict:
    case_id = case["id"]
    idem_key = action["idempotency_key"]

    existing = repository.get_attempt_by_key(idem_key)
    if existing is not None:
        # Same key already recorded — never call Razorpay again for it, and
        # never insert a second attempt row.
        return {"attempt": existing, "escalated": False, "result": existing, "reused": True}

    attempt_row = repository.insert_attempt({
        "case_id": case_id,
        "attempt_no": int(idem_key.rsplit(":", 1)[-1]),
        "strategy": decision.get("intervention"),
        "scheduled_for": decision.get("scheduled_for"),
        "idempotency_key": idem_key,
        "result": "pending",
    })
    attempt_id = (attempt_row or {}).get("id")

    if not live:
        simulated = {"id": f"plink_sim_{idem_key}", "status": "created"}
        _finish_attempt(attempt_id, "success", razorpay_ref=simulated["id"], now=now)
        audit_log.money_action(
            case_id, action["intervention"], float(action.get("amount") or 0), idem_key,
            simulated, "Simulated (live=False): recorded a test-mode result, no real API call.",
        )
        return {"attempt": attempt_row, "escalated": False, "result": simulated}

    delays = policy.get("action_retry_delays_s", [1, 4, 10])
    try:
        result = _with_backoff(
            lambda: razorpay_client.create_payment_link(
                amount=action.get("amount"),
                customer=_customer_payload(case),
                idempotency_key=idem_key,
                purpose=_purpose_for(case),
            ),
            delays,
        )
    except Exception as exc:
        _finish_attempt(attempt_id, "pending", now=now)
        audit_log.error(case_id, _ACTOR, f"Razorpay call failed after {len(delays)} attempts", exc)
        audit_log.record(
            case_id,
            _ACTOR,
            audit_log.ESCALATED,
            inp={"idempotency_key": idem_key},
            decision="escalate",
            reasoning=(
                f"Razorpay call failed {len(delays)} times in a row for {idem_key}. "
                "Marking the attempt pending, not failed, because we don't actually "
                "know whether it went through server-side. Escalating to a human with "
                "full case context rather than retrying blindly or risking a double "
                "charge."
            ),
            result={"error_type": type(exc).__name__, "error": str(exc)},
        )
        repository.update_case(case_id, state="ESCALATED")
        return {
            "attempt": attempt_row,
            "escalated": True,
            "reason": f"Razorpay call failed {len(delays)} times: {exc}",
        }

    _finish_attempt(attempt_id, "success", razorpay_ref=result.get("id"), now=now)
    audit_log.money_action(
        case_id, action["intervention"], float(action.get("amount") or 0), idem_key,
        result, "Live Razorpay payment link created.",
    )
    return {"attempt": attempt_row, "escalated": False, "result": result}


def _finish_attempt(attempt_id: str | None, result: str, *, now: datetime, razorpay_ref: str | None = None) -> None:
    if not attempt_id:
        return
    fields: dict[str, Any] = {"result": result, "executed_at": now.isoformat()}
    if razorpay_ref is not None:
        fields["razorpay_ref"] = razorpay_ref
    repository.update_attempt(attempt_id, **fields)


def _with_backoff(fn: Callable[[], Any], delays: list[float]) -> Any:
    """
    Call fn(), sleeping `delays[i]` before the i-th attempt — so even the
    first try waits a moment, giving a transient blip a chance to clear.
    Raises the last exception if every attempt in `delays` fails.
    """
    delays = delays or [0]
    last_exc: Exception | None = None
    for delay in delays:
        if delay:
            time.sleep(delay)
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see module docstring
            last_exc = exc
    raise last_exc


# ---------------------------------------------------------------------------
# outreach side
# ---------------------------------------------------------------------------

def _execute_outreach(action: dict, case: dict, decision: dict, now: datetime) -> dict:
    case_id = case["id"]
    row = repository.insert_outreach({
        "case_id": case_id,
        "channel": decision.get("channel", "whatsapp"),
        "direction": "outbound",
        "message": decision.get("message", ""),
        "sent_at": now.isoformat(),
    })
    audit_log.record(
        case_id,
        _ACTOR,
        audit_log.OUTREACH_SENT,
        inp={"channel": decision.get("channel"), "intervention": action["intervention"]},
        decision=action["intervention"],
        reasoning=decision.get("reasoning", ""),
        action=action["intervention"],
        result=row,
    )
    return {"outreach": row}


# ---------------------------------------------------------------------------
# escalate / close_lost — internal transitions, never touch money or outreach
# ---------------------------------------------------------------------------

def _apply_terminal(case_id: str, state: str, event_type: str, decision: dict) -> dict:
    audit_log.record(
        case_id,
        _ACTOR,
        event_type,
        decision=decision["intervention"],
        reasoning=decision.get("reasoning", ""),
        action=decision["intervention"],
    )
    repository.update_case(case_id, state=state)
    return {"executed": True, "intervention": decision["intervention"], "gate": None}
