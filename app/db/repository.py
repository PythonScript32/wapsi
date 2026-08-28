"""
Data-access layer. ALL database reads and writes go through this module.

WHY THIS EXISTS
---------------
This is the seam that keeps the project portable. Pipeline code never imports
the Supabase client directly — it calls a function here. If Supabase goes down,
pauses, or we want to run offline, we reimplement this one file and nothing else
in the codebase changes.

It also makes the pipeline testable: swap this module for a fake and the whole
agent runs in memory.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.db.client import get_client

# States that must never be walked back. Webhooks arrive out of order, so a
# late-delivered "failed" event must not drag a RECOVERED case backwards.
TERMINAL_STATES = {"RECOVERED", "CLOSED_LOST"}


# ---------------------------------------------------------------------------
# cases
# ---------------------------------------------------------------------------

def insert_case(case: dict) -> dict:
    resp = get_client().table("cases").insert(case).execute()
    return resp.data[0] if resp.data else {}


def upsert_case(case: dict) -> dict:
    """Insert or update by primary key — safe against duplicate webhooks."""
    resp = get_client().table("cases").upsert(case).execute()
    return resp.data[0] if resp.data else {}


def get_case(case_id: str) -> dict | None:
    resp = get_client().table("cases").select("*").eq("id", case_id).limit(1).execute()
    return resp.data[0] if resp.data else None


def list_cases(batch_id: str | None = None, state: str | None = None,
               limit: int = 1000) -> list[dict]:
    q = get_client().table("cases").select("*")
    if batch_id:
        q = q.eq("batch_id", batch_id)
    if state:
        q = q.eq("state", state)
    return q.limit(limit).execute().data or []


def update_case(case_id: str, **fields: Any) -> dict | None:
    """Patch a case. Refuses to regress out of a terminal state."""
    current = get_case(case_id)
    if current is None:
        return None

    new_state = fields.get("state")
    if new_state and current.get("state") in TERMINAL_STATES and new_state not in TERMINAL_STATES:
        return current  # out-of-order event; ignore the regression

    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    resp = get_client().table("cases").update(fields).eq("id", case_id).execute()
    return resp.data[0] if resp.data else None


def mark_recovered(case_id: str, amount: float) -> dict | None:
    return update_case(
        case_id,
        state="RECOVERED",
        recovered_amount=amount,
        recovered_at=datetime.now(timezone.utc).isoformat(),
    )


def increment_attempts(case_id: str) -> int:
    case = get_case(case_id)
    if case is None:
        return 0
    n = int(case.get("attempts_made") or 0) + 1
    update_case(case_id, attempts_made=n)
    return n


# ---------------------------------------------------------------------------
# payment_attempts
# ---------------------------------------------------------------------------

def insert_attempt(attempt: dict) -> dict | None:
    """
    Record a payment attempt.

    `idempotency_key` is UNIQUE in Postgres. If the same key is inserted twice
    the database rejects it — exactly the protection we want. We swallow that
    specific conflict and return the existing row, so the caller learns "this
    already happened" instead of charging twice.
    """
    try:
        resp = get_client().table("payment_attempts").insert(attempt).execute()
        return resp.data[0] if resp.data else None
    except Exception as exc:
        text = str(exc).lower()
        if "duplicate" in text or "unique" in text or "23505" in text:
            return get_attempt_by_key(attempt["idempotency_key"])
        raise


def get_attempt_by_key(idempotency_key: str) -> dict | None:
    resp = (get_client().table("payment_attempts").select("*")
            .eq("idempotency_key", idempotency_key).limit(1).execute())
    return resp.data[0] if resp.data else None


def attempts_for_case(case_id: str) -> list[dict]:
    resp = (get_client().table("payment_attempts").select("*")
            .eq("case_id", case_id).order("attempt_no").execute())
    return resp.data or []


def update_attempt(attempt_id: str, **fields: Any) -> None:
    get_client().table("payment_attempts").update(fields).eq("id", attempt_id).execute()


# ---------------------------------------------------------------------------
# outreach
# ---------------------------------------------------------------------------

def insert_outreach(row: dict) -> dict | None:
    resp = get_client().table("outreach").insert(row).execute()
    return resp.data[0] if resp.data else None


def last_outreach_at(case_id: str) -> str | None:
    """Feeds gate G4 (minimum contact gap)."""
    resp = (get_client().table("outreach").select("sent_at")
            .eq("case_id", case_id).eq("direction", "outbound")
            .order("sent_at", desc=True).limit(1).execute())
    return resp.data[0]["sent_at"] if resp.data else None


def outreach_for_case(case_id: str) -> list[dict]:
    resp = (get_client().table("outreach").select("*")
            .eq("case_id", case_id).order("sent_at").execute())
    return resp.data or []


def record_reply(outreach_id: str, text: str, intent: str) -> None:
    get_client().table("outreach").update({
        "response_text": text,
        "response_intent": intent,
        "responded_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", outreach_id).execute()


# ---------------------------------------------------------------------------
# promises
# ---------------------------------------------------------------------------

def insert_promise(row: dict) -> dict | None:
    resp = get_client().table("promises").insert(row).execute()
    return resp.data[0] if resp.data else None


def active_promise(case_id: str) -> dict | None:
    """Feeds gate G10 — a live promise pauses other outreach."""
    resp = (get_client().table("promises").select("*")
            .eq("case_id", case_id).eq("status", "pending").limit(1).execute())
    return resp.data[0] if resp.data else None


def due_promises(on_date: str) -> list[dict]:
    resp = (get_client().table("promises").select("*")
            .eq("status", "pending").lte("promised_date", on_date).execute())
    return resp.data or []


def resolve_promise(promise_id: str, status: str) -> None:
    """status: 'kept' | 'broken'"""
    get_client().table("promises").update({
        "status": status,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", promise_id).execute()


def all_promises(batch_id: str | None = None) -> list[dict]:
    """For the kept-promise-rate metric."""
    if batch_id is None:
        return get_client().table("promises").select("*").execute().data or []
    case_ids = [c["id"] for c in list_cases(batch_id=batch_id)]
    if not case_ids:
        return []
    return get_client().table("promises").select("*").in_("case_id", case_ids).execute().data or []


# ---------------------------------------------------------------------------
# audit_log  (append-only — no update/delete functions exist here, by design)
# ---------------------------------------------------------------------------

def append_audit(row: dict) -> None:
    get_client().table("audit_log").insert(row).execute()


def audit_for_case(case_id: str) -> list[dict]:
    resp = (get_client().table("audit_log").select("*")
            .eq("case_id", case_id).order("ts").execute())
    return resp.data or []


def audit_by_event(event_type: str, limit: int = 500) -> list[dict]:
    """Used by metrics — e.g. counting GATE_BLOCK rows per gate."""
    resp = (get_client().table("audit_log").select("*")
            .eq("event_type", event_type).limit(limit).execute())
    return resp.data or []


# ---------------------------------------------------------------------------
# gate context — one call gathers everything policy_gate.check() needs
# ---------------------------------------------------------------------------

def gate_context(case_id: str) -> dict:
    """
    Collect the facts the governance gate needs.

    Keeping this here is what lets check() stay a pure function: this module
    talks to the database, that module only judges.
    """
    return {
        "last_contact_at": last_outreach_at(case_id),
        "has_active_promise": active_promise(case_id) is not None,
        "pre_debit_notice_at": _pre_debit_notice_at(case_id),
    }


def _pre_debit_notice_at(case_id: str) -> str | None:
    """When the RBI pre-debit notice was sent. Feeds gate G9."""
    resp = (get_client().table("outreach").select("sent_at")
            .eq("case_id", case_id).eq("channel", "pre_debit_notice")
            .order("sent_at", desc=True).limit(1).execute())
    return resp.data[0]["sent_at"] if resp.data else None


# ---------------------------------------------------------------------------
# batch helpers
# ---------------------------------------------------------------------------

def clear_batch(batch_id: str) -> None:
    """
    Wipe a batch before a re-run. Cases cascade to attempts/outreach/promises.

    audit_log rows are intentionally NOT deleted — the database trigger forbids
    it, and that is precisely the point of an append-only log.
    """
    get_client().table("cases").delete().eq("batch_id", batch_id).execute()
