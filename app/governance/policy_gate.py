"""
Governance layer — the gate every money and outreach action must pass.

This is the most important module in the codebase. The track bar reads "every
money action explainable, bounded and gated". This file is the "bounded and
gated" half; app/audit/log.py is the "explainable" half.

DESIGN NOTE — why check() is a pure function
--------------------------------------------
check() takes everything it needs as arguments and touches no database, no
clock, no network. The caller gathers the facts; this function only judges them.

That buys three things:
  1. Trivially testable — every gate gets a unit test with no fixtures.
  2. Deterministic — identical inputs always give an identical verdict, which is
     what makes the audit trail trustworthy.
  3. It cannot fail for an environmental reason. A gate that could raise a
     network error would be a gate that sometimes doesn't run.

FAIL CLOSED
-----------
If a gate cannot be evaluated — missing field, unparseable date, unknown action
type — the answer is BLOCK, never ALLOW. In a system that moves money, "I'm not
sure" must never mean "go ahead".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# Action types that move money. These face the strictest gates.
MONEY_ACTIONS = {"charge", "retry", "offer"}

# Action types that contact the customer.
OUTREACH_ACTIONS = {"outreach", "offer", "voice"}

# Terminal states — nothing further may happen to these cases.
TERMINAL_STATES = {"RECOVERED", "CLOSED_LOST"}


@dataclass(frozen=True)
class GateResult:
    """The verdict. `reason` is written verbatim into the audit log."""

    allowed: bool
    gate: str | None
    reason: str

    def __bool__(self) -> bool:  # lets callers write `if result:`
        return self.allowed


def _allow(reason: str) -> GateResult:
    return GateResult(True, None, reason)


def _block(gate: str, reason: str) -> GateResult:
    return GateResult(False, gate, reason)


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


def check(
    action: dict,
    case: dict,
    policy: dict,
    *,
    now: datetime | None = None,
    last_contact_at: Any = None,
    has_active_promise: bool = False,
    pre_debit_notice_at: Any = None,
) -> GateResult:
    """
    Decide whether `action` may fire for `case` right now.

    action: {
        "type": "charge" | "retry" | "outreach" | "offer" | "voice",
        "intervention": str,
        "idempotency_key": str | None,   # required for money actions
        "discount_pct": float,           # for offers
        "amount": float,                 # for money actions
        "is_mandate_debit": bool,        # triggers the RBI notice gate
    }
    case:   a row from the `cases` table.
    policy: config.DEFAULT_POLICY (or a merchant override).

    Context arguments, gathered by the caller from the repository:
        last_contact_at     — timestamp of the most recent outreach, if any
        has_active_promise  — is there a pending promise-to-pay?
        pre_debit_notice_at — when the RBI pre-debit notice was sent
    """
    now = now or datetime.now(timezone.utc)

    # ---- structural validation: fail closed on anything malformed -----------
    if not isinstance(action, dict) or not isinstance(case, dict):
        return _block("G0", "Malformed action or case — blocking (fail closed).")

    action_type = action.get("type")
    if action_type not in MONEY_ACTIONS | OUTREACH_ACTIONS:
        return _block("G0", f"Unknown action type {action_type!r} — blocking (fail closed).")

    is_money = action_type in MONEY_ACTIONS
    is_outreach = action_type in OUTREACH_ACTIONS

    # ---- G1: never touch a case that is already resolved --------------------
    state = case.get("state")
    if state in TERMINAL_STATES:
        return _block(
            "G1",
            f"Case is {state}. Never charge or chase a customer whose case is closed.",
        )

    # ---- G2: opt-out is sacred, and permanent -------------------------------
    if case.get("opted_out"):
        return _block(
            "G2",
            "Customer opted out. All contact and collection stops permanently.",
        )

    # ---- G3: per-reason attempt cap -----------------------------------------
    if is_money:
        reason = case.get("reason_category")
        rule = policy.get("retry_rules", {}).get(reason)
        if rule is None:
            return _block(
                "G3",
                f"No retry rule defined for reason {reason!r} — blocking (fail closed).",
            )
        attempts = int(case.get("attempts_made") or 0)
        cap = int(rule.get("max_attempts", policy.get("max_retries", 3)))
        if attempts >= cap:
            return _block(
                "G3",
                f"Attempt cap reached for {reason}: {attempts}/{cap}. "
                "Escalate instead of retrying.",
            )

    # ---- G4: minimum gap between contacts (anti-harassment) -----------------
    if is_outreach:
        gap_hours = int(policy.get("min_contact_gap_hours", 24))
        last = _parse_ts(last_contact_at)
        if last is not None:
            elapsed = now - last
            if elapsed < timedelta(hours=gap_hours):
                hrs = elapsed.total_seconds() / 3600
                return _block(
                    "G4",
                    f"Last contact was {hrs:.1f}h ago; minimum gap is {gap_hours}h. "
                    "Contacting again now would be harassment.",
                )

    # ---- G5: grace period ---------------------------------------------------
    grace_days = int(policy.get("grace_period_days", 14))
    created = _parse_ts(case.get("created_at"))
    if created is None:
        return _block("G5", "Case has no valid created_at — blocking (fail closed).")
    age_days = (now - created).days
    if age_days > grace_days:
        return _block(
            "G5",
            f"Case is {age_days} days old; grace period is {grace_days} days. "
            "Close as CLOSED_LOST rather than continuing to pursue.",
        )

    # ---- G6: discount cap ---------------------------------------------------
    discount = float(action.get("discount_pct") or 0)
    if discount:
        max_discount = float(policy.get("max_discount_pct", 10))
        if discount > max_discount:
            return _block(
                "G6",
                f"Offer of {discount}% exceeds the {max_discount}% cap. Recovering revenue "
                "by giving away more margin than policy allows is still a loss.",
            )

    # ---- G7: exposure cap — large amounts need a human ----------------------
    if is_money:
        amount = float(action.get("amount") or case.get("amount") or 0)
        max_exposure = float(policy.get("max_exposure_inr", 5000))
        if amount > max_exposure:
            return _block(
                "G7",
                f"Amount Rs {amount:,.2f} exceeds the Rs {max_exposure:,.2f} autonomous "
                "limit. Requires human approval.",
            )

    # ---- G8: idempotency key is mandatory for money -------------------------
    if is_money:
        key = action.get("idempotency_key")
        if not key or not str(key).strip():
            return _block(
                "G8",
                "No idempotency key. Without one, a retried request could double-charge "
                "the customer. Refusing to move money.",
            )

    # ---- G9: RBI pre-debit notification -------------------------------------
    if is_money and action.get("is_mandate_debit"):
        notice_hours = int(policy.get("rbi_pre_debit_notice_hours", 24))
        notice_at = _parse_ts(pre_debit_notice_at)
        if notice_at is None:
            return _block(
                "G9",
                "No pre-debit notice on record. RBI requires the customer be notified at "
                f"least {notice_hours}h before a mandate debit.",
            )
        elapsed_h = (now - notice_at).total_seconds() / 3600
        if elapsed_h < notice_hours:
            return _block(
                "G9",
                f"Pre-debit notice sent {elapsed_h:.1f}h ago; RBI requires {notice_hours}h. "
                "Debit must wait.",
            )

    # ---- G10: an active promise pauses everything else ----------------------
    if has_active_promise and action.get("intervention") != "promise_retry":
        return _block(
            "G10",
            "Customer has an active promise-to-pay. Chasing them before the promised date "
            "is the fastest way to lose them. Waiting.",
        )

    # ---- all gates passed ---------------------------------------------------
    suffix = f" ({action['intervention']})" if action.get("intervention") else ""
    return _allow(f"All gates passed for {action_type}{suffix}.")
