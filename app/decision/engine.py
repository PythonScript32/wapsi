"""
Decision engine (the brain): category + case history + policy -> an intervention.

Returns a Decision:
  {
    "intervention": "retry_now" | "retry_after_date" | "request_re_mandate"
                    | "request_card_update" | "send_link"
                    | "send_link_with_offer" | "escalate" | "close_lost",
    "scheduled_for": iso8601 | None,
    "channel": "whatsapp" | "sms" | "email" | "voice",
    "message": "<Hinglish copy>",
    "discount_pct": float,          # 0 unless an offer, always <= policy cap
    "is_mandate_debit": bool,
    "reasoning": "<why, plain language>"
  }

TIMING INTELLIGENCE (the thing that beats a naive retry):
  insufficient_funds -> wait for the customer's salary day (1st / month-end
                        cluster), then retry. Retrying immediately mostly fails.
  bank_downtime      -> short exponential backoff (hours), retry soon.
  mandate_revoked    -> NEVER silently retry; request re-mandate.
  expired_card       -> request card update; payment link as fallback.
  technical_other    -> backoff, capped attempts.
  checkout_dropoff   -> nudge first; bounded offer only on the second touch.

Every field is clamped to policy before it leaves this module, and the result
must still pass governance.policy_gate.check() before execution — this module
proposes, it does not authorise.

Never reads case["latent"] — decisions are made blind, exactly as they would
be in production. Only app/detection/batch_scanner.py may read that field.

MESSAGES are template-first: every intervention has a complete, natural
Hinglish template that works with no network at all. use_llm=False by default
— batch runs never touch the network for copy; pass use_llm=True (e.g. the
live demo) to let the LLM (app/llm/client.py) rephrase the template. If that
call fails for any reason, the template stands; personalisation must never
block a decision.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.audit import log as audit_log
from app.llm import client as llm_client

# ---------------------------------------------------------------------------
# timestamp / attempt helpers
# ---------------------------------------------------------------------------

def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO timestamp into an aware UTC datetime. None if unparseable."""
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


def _attempt_number(case: dict, history: list[dict] | None) -> int:
    """
    The attempt about to be proposed. history (prior payment_attempts /
    outreach rows for this case) is authoritative when present; the case's
    own counter is the fallback, and we trust whichever says more happened,
    since undercounting risks a silent extra retry.
    """
    history = history or []
    return max(len(history), int(case.get("attempts_made") or 0)) + 1


def _next_salary_day(now: datetime) -> datetime:
    """
    HEURISTIC ONLY — the pipeline never sees the customer's real salary day.
    Indian salaries cluster on the 1st of the month and on month-end. Guess
    whichever of those two lands soonest strictly after `now` (by calendar
    date, never today, even if the clock time technically hasn't passed yet).
    """
    if now.month == 12:
        next_month_start = now.replace(
            year=now.year + 1, month=1, day=1, hour=9, minute=0, second=0, microsecond=0
        )
    else:
        next_month_start = now.replace(
            month=now.month + 1, day=1, hour=9, minute=0, second=0, microsecond=0
        )
    this_month_end = next_month_start - timedelta(days=1)

    candidates = [d for d in (this_month_end, next_month_start) if d.date() > now.date()]
    return min(candidates)


def _backoff_target(attempt_no: int, now: datetime, policy: dict) -> datetime:
    schedule = policy.get("backoff_hours", [4, 12, 24])
    if not schedule:
        schedule = [24]
    hours = schedule[min(attempt_no - 1, len(schedule) - 1)]
    return now + timedelta(hours=hours)


# ---------------------------------------------------------------------------
# formatting helpers (Hinglish templates, no em dashes anywhere)
# ---------------------------------------------------------------------------

def _fmt_amount(amount: Any) -> str:
    try:
        return f"{float(amount):,.0f}"
    except (TypeError, ValueError):
        return "0"


def _fmt_date(dt: datetime) -> str:
    return dt.strftime("%d %b")


def _customer_name(case: dict) -> str:
    return case.get("customer_ref") or "Dost"


def _strip_em_dash(text: str) -> str:
    """Belt-and-braces: no em/en dashes reach the customer, template or LLM."""
    return text.replace("—", ",").replace("–", ",")


def _default_channel(policy: dict) -> str:
    priority = policy.get("channel_priority") or ["whatsapp"]
    return priority[0]


_PERSONALIZE_PROMPT = (
    "Rewrite this Hinglish customer message so it sounds warm and natural, in at "
    "most two short sentences. Keep every fact exactly the same: amounts, dates, "
    "percentages, links. Do not use em dashes. Reply with only the rewritten "
    "message, nothing else.\n\nOriginal message:\n\"{message}\""
)


def _personalize(message: str) -> str:
    """Best-effort LLM rewrite. Any failure here must never block a decision,
    so callers wrap this in a try/except and keep the template on error."""
    reply = llm_client.call(_PERSONALIZE_PROMPT.format(message=message))
    cleaned = (reply or "").strip()
    return cleaned or message


# ---------------------------------------------------------------------------
# per-reason decision logic — each returns a partial decision (no case_id,
# no clamping yet; decide() finishes the job)
# ---------------------------------------------------------------------------

def _decide_insufficient_funds(case: dict, attempt_no: int, now: datetime, policy: dict) -> dict:
    salary_dt = _next_salary_day(now)
    name, amount, date_str = _customer_name(case), _fmt_amount(case.get("amount")), _fmt_date(salary_dt)
    return {
        "intervention": "retry_after_date",
        "scheduled_for": salary_dt,
        "channel": _default_channel(policy),
        "message": (
            f"Namaste {name}, aapka Rs {amount} ka payment abhi fail ho gaya, account mein "
            f"balance kam tha. Hum ise {date_str} ko dobara try karenge, jab tak salary aa "
            "chuki hogi. Aapko kuch karne ki zaroorat nahi hai."
        ),
        "discount_pct": 0.0,
        "is_mandate_debit": True,
        "reasoning": (
            f"Insufficient funds on attempt {attempt_no}. Retrying today would very likely "
            f"fail again, so scheduling for {date_str}, the next likely salary-cluster date "
            "(1st or month-end). This is a heuristic guess, not the customer's actual salary "
            "day, which the pipeline never sees."
        ),
    }


def _decide_backoff_retry(case: dict, reason: str, attempt_no: int, now: datetime, policy: dict) -> dict:
    scheduled = _backoff_target(attempt_no, now, policy)
    name, amount, date_str = _customer_name(case), _fmt_amount(case.get("amount")), _fmt_date(scheduled)
    label = reason.replace("_", " ")
    return {
        "intervention": "retry_after_date",
        "scheduled_for": scheduled,
        "channel": _default_channel(policy),
        "message": (
            f"Namaste {name}, aapka Rs {amount} ka payment ek technical issue ki wajah se "
            f"fail ho gaya. Hum ise {date_str} ko dobara try karenge, tab tak aapko kuch "
            "karne ki zaroorat nahi."
        ),
        "discount_pct": 0.0,
        "is_mandate_debit": True,
        "reasoning": (
            f"{label.capitalize()} on attempt {attempt_no}. The money is presumably still "
            f"reachable, so backing off and retrying at {scheduled.isoformat()} rather than "
            "assuming the case is unrecoverable."
        ),
    }


def _decide_mandate_revoked(case: dict, attempt_no: int, policy: dict) -> dict:
    name = _customer_name(case)
    return {
        "intervention": "request_re_mandate",
        "scheduled_for": None,
        "channel": _default_channel(policy),
        "message": (
            f"Namaste {name}, aapka autopay mandate cancel ho gaya hai isliye payment nahi "
            "ho paya. Kripya naya mandate set up karein taaki aapki service bina rukawat "
            "chalti rahe."
        ),
        "discount_pct": 0.0,
        "is_mandate_debit": False,
        "reasoning": (
            "Mandate has been revoked; a revoked mandate cannot be charged, silently retrying "
            "would just fail again. Requesting a fresh mandate instead."
        ),
    }


def _decide_expired_card(case: dict, attempt_no: int, policy: dict) -> dict:
    name, amount = _customer_name(case), _fmt_amount(case.get("amount"))
    if attempt_no <= 1:
        return {
            "intervention": "request_card_update",
            "scheduled_for": None,
            "channel": _default_channel(policy),
            "message": (
                f"Namaste {name}, aapka card expire ho chuka hai isliye Rs {amount} ka "
                "payment fail ho gaya. Kripya apna naya card add karein taaki aapki service "
                "chalti rahe."
            ),
            "discount_pct": 0.0,
            "is_mandate_debit": False,
            "reasoning": (
                "Card has expired and can never be charged again. Asking the customer to "
                "update it before trying anything else."
            ),
        }
    return {
        "intervention": "send_link",
        "scheduled_for": None,
        "channel": _default_channel(policy),
        "message": (
            f"Namaste {name}, lagta hai card update nahi ho paaya. Koi baat nahi, yeh raha "
            f"Rs {amount} ka seedha payment link, ek click mein pay kar dein."
        ),
        "discount_pct": 0.0,
        "is_mandate_debit": False,
        "reasoning": (
            f"Card update wasn't completed after attempt 1. Falling back to a direct payment "
            f"link on attempt {attempt_no} rather than asking again."
        ),
    }


def _decide_checkout_dropoff(case: dict, attempt_no: int, policy: dict) -> dict:
    name, amount = _customer_name(case), _fmt_amount(case.get("amount"))
    if attempt_no <= 1:
        return {
            "intervention": "send_link",
            "scheduled_for": None,
            "channel": _default_channel(policy),
            "message": (
                f"Namaste {name}, lagta hai checkout complete nahi hua. Rs {amount} ka aapka "
                "order abhi bhi ready hai, yeh raha payment link, bas ek click door."
            ),
            "discount_pct": 0.0,
            "is_mandate_debit": False,
            "reasoning": (
                "Checkout abandoned. First touch is a plain nudge with a fresh payment link, "
                "no discount yet, intent alone is usually enough to convert."
            ),
        }
    discount = float(policy.get("default_offer_pct", 10.0))
    return {
        "intervention": "send_link_with_offer",
        "scheduled_for": None,
        "channel": _default_channel(policy),
        "message": (
            f"Namaste {name}, khaas aapke liye {discount:.0f}% off! Rs {amount} ka order abhi "
            "complete karein is offer ke saath, zyada der mat sochiye."
        ),
        "discount_pct": discount,
        "is_mandate_debit": False,
        "reasoning": (
            f"First nudge didn't convert. Offering a bounded {discount:.0f}% discount on the "
            "second and final touch, capped by policy, before closing the case."
        ),
    }


def _decide_checkout_touches_exhausted(attempt_no: int, cap: int, policy: dict) -> dict:
    """
    PRD FR-B5: checkout_dropoff gets at most 2 touches, then CLOSED_LOST — not
    an escalation. Two unconverted nudges is a normal, low-stakes outcome for
    an abandoned cart, not something that needs a human.
    """
    return {
        "intervention": "close_lost",
        "scheduled_for": None,
        "channel": _default_channel(policy),
        "message": (
            f"Internal note: closed as lost. Touch {attempt_no} exceeds the {cap}-touch cap "
            "for checkout_dropoff."
        ),
        "discount_pct": 0.0,
        "is_mandate_debit": False,
        "reasoning": (
            f"Touch {attempt_no} exceeds the {cap}-touch cap for checkout_dropoff. Two touches "
            "without conversion means the nudge and the offer both missed; closing as lost "
            "rather than escalating a low-intent abandoned cart to a human."
        ),
    }


def _decide_escalate(reason: str, attempt_no: int, cap: int, policy: dict) -> dict:
    return {
        "intervention": "escalate",
        "scheduled_for": None,
        "channel": _default_channel(policy),
        "message": (
            f"Internal note: escalated to a human. Reason {reason}, attempt {attempt_no} "
            f"exceeds the {cap}-attempt cap."
        ),
        "discount_pct": 0.0,
        "is_mandate_debit": False,
        "reasoning": (
            f"Attempt {attempt_no} exceeds the {cap}-attempt cap for {reason}. Escalating to "
            "a human instead of continuing to retry blindly."
        ),
    }


def _decide_close_lost(age_days: int, grace_days: int, policy: dict) -> dict:
    return {
        "intervention": "close_lost",
        "scheduled_for": None,
        "channel": _default_channel(policy),
        "message": (
            f"Internal note: closed as lost. Case is {age_days} days old, past the "
            f"{grace_days}-day grace period."
        ),
        "discount_pct": 0.0,
        "is_mandate_debit": False,
        "reasoning": (
            f"Case is {age_days} days old, past the {grace_days}-day grace period. Closing as "
            "lost rather than continuing to chase indefinitely."
        ),
    }


def _decide_unknown_reason(reason: Any, policy: dict) -> dict:
    return {
        "intervention": "escalate",
        "scheduled_for": None,
        "channel": _default_channel(policy),
        "message": f"Internal note: escalated, unrecognised reason category {reason!r}.",
        "discount_pct": 0.0,
        "is_mandate_debit": False,
        "reasoning": (
            f"Reason category {reason!r} isn't one this engine knows how to handle. Failing "
            "safe to a human rather than guessing an intervention."
        ),
    }


_BACKOFF_REASONS = {"bank_downtime", "technical_other"}


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def decide(
    case: dict,
    history: list[dict],
    policy: dict,
    *,
    now: datetime | None = None,
    use_llm: bool = False,
) -> dict:
    """
    Turn a diagnosed case into a bounded, reason-appropriate intervention.

    use_llm=False (the default): messages come entirely from templates, no
    network call is made. Batch runs should never pass use_llm=True; the live
    demo may, to let the LLM rephrase the template — if that call fails for
    any reason the template is used as-is.
    """
    now = now or datetime.now(timezone.utc)
    case_id = case.get("id")
    reason = case.get("reason_category")
    attempt_no = _attempt_number(case, history)
    created_at = _parse_ts(case.get("created_at"))

    grace_days = int(policy.get("grace_period_days", 14))
    if created_at is not None:
        age_days = (now - created_at).days
        if age_days > grace_days:
            partial = _decide_close_lost(age_days, grace_days, policy)
            return _finalize(case_id, reason, attempt_no, partial, policy, created_at, now, use_llm=False)

    rule = policy.get("retry_rules", {}).get(reason, {})
    cap = int(rule.get("max_attempts", policy.get("max_retries", 3)))
    if attempt_no > cap:
        if reason == "checkout_dropoff":
            partial = _decide_checkout_touches_exhausted(attempt_no, cap, policy)
        else:
            partial = _decide_escalate(reason, attempt_no, cap, policy)
        return _finalize(case_id, reason, attempt_no, partial, policy, created_at, now, use_llm=False)

    if reason == "insufficient_funds":
        partial = _decide_insufficient_funds(case, attempt_no, now, policy)
    elif reason in _BACKOFF_REASONS:
        partial = _decide_backoff_retry(case, reason, attempt_no, now, policy)
    elif reason == "mandate_revoked":
        partial = _decide_mandate_revoked(case, attempt_no, policy)
    elif reason == "expired_card":
        partial = _decide_expired_card(case, attempt_no, policy)
    elif reason == "checkout_dropoff":
        partial = _decide_checkout_dropoff(case, attempt_no, policy)
    else:
        partial = _decide_unknown_reason(reason, policy)

    return _finalize(case_id, reason, attempt_no, partial, policy, created_at, now, use_llm=use_llm)


def _finalize(
    case_id: str | None,
    reason: Any,
    attempt_no: int,
    partial: dict,
    policy: dict,
    created_at: datetime | None,
    now: datetime,
    *,
    use_llm: bool,
) -> dict:
    """Clamp every bound, optionally personalise the message, log DECIDED."""
    discount = max(0.0, min(float(partial["discount_pct"]), float(policy.get("max_discount_pct", 10.0))))

    scheduled_for = partial["scheduled_for"]
    if scheduled_for is not None and created_at is not None:
        grace_days = int(policy.get("grace_period_days", 14))
        deadline = created_at + timedelta(days=grace_days)
        scheduled_for = min(scheduled_for, deadline)

    message = _strip_em_dash(partial["message"])
    personalized = False
    if use_llm:
        try:
            message = _strip_em_dash(_personalize(message))
            personalized = True
        except Exception:
            pass  # template already natural and complete; never block on the LLM

    decision = {
        "intervention": partial["intervention"],
        "scheduled_for": scheduled_for.isoformat() if scheduled_for is not None else None,
        "channel": partial["channel"],
        "message": message,
        "discount_pct": discount,
        "is_mandate_debit": bool(partial["is_mandate_debit"]),
        "reasoning": partial["reasoning"],
    }

    audit_log.record(
        case_id,
        "decision.engine",
        audit_log.DECIDED,
        inp={"reason_category": reason, "attempt_no": attempt_no},
        decision=decision["intervention"],
        reasoning=decision["reasoning"],
        result={**decision, "used_llm": use_llm, "personalized": personalized},
    )
    return decision
