"""
In-memory implementation of app/db/repository.py's exact interface.

WHY THIS EXISTS
---------------
Every write in the real repository is a network round trip to Supabase. A
300-case holdout batch touches this layer thousands of times (diagnose,
decide, gate-check, attempt, outreach, audit — repeatedly, per case, per
simulated day); at real network latency that's hours, not seconds. This
class holds the exact same data model in plain dicts and lists so a batch
run can execute at the speed of Python, then flush the final state to
Supabase in a handful of bulk calls (see repository.bulk_insert, used by
app/detection/batch_scanner.py).

SAME INTERFACE, SAME SEMANTICS
-------------------------------
Every public function repository.py exposes exists here with the same
name and signature, as a bound method — that's what lets
batch_scanner swap `app.db.repository`'s functions for this class's
methods and have the rest of the pipeline (classifier, decision engine,
actions, audit log) work completely unaware anything changed. In
particular:
  - insert_attempt() replicates the UNIQUE(idempotency_key) conflict: a
    repeated key returns the EXISTING row instead of creating a second one.
  - update_case() replicates the terminal-state regression guard: once a
    case is RECOVERED or CLOSED_LOST, a state field that isn't itself
    terminal is silently dropped (out-of-order events must never regress it).
  - append_audit() is the only way to add a row, and there is deliberately
    no update/delete on it anywhere in this class — same append-only
    contract the real audit_log table's trigger enforces.

Every getter returns copies, never the internal stored dict — a caller
mutating what it got back must not corrupt this "database"'s state, exactly
as a real round trip through JSON would isolate them.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

TERMINAL_STATES = {"RECOVERED", "CLOSED_LOST"}

_CASE_DEFAULTS = {
    "batch_id": "live",
    "currency": "INR",
    "reason_raw": None,
    "reason_category": None,
    "customer_phone": None,
    "state": "DETECTED",
    "attempts_made": 0,
    "opted_out": False,
    "recovered_amount": 0.0,
    "recovered_at": None,
    "latent": None,
}


class MemoryRepository:
    def __init__(self) -> None:
        self._cases: dict[str, dict] = {}
        self._attempts: dict[str, dict] = {}
        self._attempt_ids_by_key: dict[str, str] = {}
        self._outreach: dict[str, dict] = {}
        self._promises: dict[str, dict] = {}
        self._audit_log: list[dict] = []
        self._audit_seq = 0

    # -----------------------------------------------------------------
    # cases
    # -----------------------------------------------------------------

    def insert_case(self, case: dict) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        row = {**_CASE_DEFAULTS, **case}
        row.setdefault("created_at", now)
        row["updated_at"] = now
        self._cases[row["id"]] = row
        return dict(row)

    def upsert_case(self, case: dict) -> dict:
        existing = self._cases.get(case.get("id"))
        if existing is None:
            return self.insert_case(case)
        existing.update(case)
        existing["updated_at"] = datetime.now(timezone.utc).isoformat()
        return dict(existing)

    def get_case(self, case_id: str) -> dict | None:
        row = self._cases.get(case_id)
        return dict(row) if row is not None else None

    def list_cases(self, batch_id: str | None = None, state: str | None = None, limit: int = 1000) -> list[dict]:
        rows = list(self._cases.values())
        if batch_id:
            rows = [r for r in rows if r.get("batch_id") == batch_id]
        if state:
            rows = [r for r in rows if r.get("state") == state]
        return [dict(r) for r in rows[:limit]]

    def update_case(self, case_id: str, **fields: Any) -> dict | None:
        """Patch a case. Refuses to regress out of a terminal state."""
        current = self._cases.get(case_id)
        if current is None:
            return None

        new_state = fields.get("state")
        if new_state and current.get("state") in TERMINAL_STATES and new_state not in TERMINAL_STATES:
            return dict(current)  # out-of-order event; ignore the regression

        fields = dict(fields)
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        current.update(fields)
        return dict(current)

    def mark_recovered(self, case_id: str, amount: float) -> dict | None:
        return self.update_case(
            case_id,
            state="RECOVERED",
            recovered_amount=amount,
            recovered_at=datetime.now(timezone.utc).isoformat(),
        )

    def increment_attempts(self, case_id: str) -> int:
        case = self._cases.get(case_id)
        if case is None:
            return 0
        n = int(case.get("attempts_made") or 0) + 1
        self.update_case(case_id, attempts_made=n)
        return n

    # -----------------------------------------------------------------
    # payment_attempts
    # -----------------------------------------------------------------

    def insert_attempt(self, attempt: dict) -> dict | None:
        """
        idempotency_key is UNIQUE. A repeated key returns the row that's
        already there instead of creating a second one — the same
        protection the real UNIQUE constraint + repository.py's exception
        catch gives, just without needing an exception to detect it.
        """
        key = attempt.get("idempotency_key")
        if key and key in self._attempt_ids_by_key:
            return self.get_attempt_by_key(key)

        row = dict(attempt)
        row.setdefault("id", str(uuid.uuid4()))
        row.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        self._attempts[row["id"]] = row
        if key:
            self._attempt_ids_by_key[key] = row["id"]
        return dict(row)

    def get_attempt_by_key(self, idempotency_key: str) -> dict | None:
        attempt_id = self._attempt_ids_by_key.get(idempotency_key)
        if attempt_id is None:
            return None
        row = self._attempts.get(attempt_id)
        return dict(row) if row is not None else None

    def attempts_for_case(self, case_id: str) -> list[dict]:
        rows = [r for r in self._attempts.values() if r.get("case_id") == case_id]
        rows.sort(key=lambda r: r.get("attempt_no") or 0)
        return [dict(r) for r in rows]

    def update_attempt(self, attempt_id: str, **fields: Any) -> None:
        row = self._attempts.get(attempt_id)
        if row is not None:
            row.update(fields)

    # -----------------------------------------------------------------
    # outreach
    # -----------------------------------------------------------------

    def insert_outreach(self, row: dict) -> dict | None:
        stored = dict(row)
        stored.setdefault("id", str(uuid.uuid4()))
        stored.setdefault("direction", "outbound")
        stored.setdefault("language", "hinglish")
        stored.setdefault("sent_at", datetime.now(timezone.utc).isoformat())
        self._outreach[stored["id"]] = stored
        return dict(stored)

    def last_outreach_at(self, case_id: str) -> str | None:
        """Feeds gate G4 (minimum contact gap)."""
        rows = [
            r for r in self._outreach.values()
            if r.get("case_id") == case_id and r.get("direction") == "outbound"
        ]
        if not rows:
            return None
        rows.sort(key=lambda r: r.get("sent_at") or "")
        return rows[-1].get("sent_at")

    def outreach_for_case(self, case_id: str) -> list[dict]:
        rows = [r for r in self._outreach.values() if r.get("case_id") == case_id]
        rows.sort(key=lambda r: r.get("sent_at") or "")
        return [dict(r) for r in rows]

    def record_reply(self, outreach_id: str, text: str, intent: str) -> None:
        row = self._outreach.get(outreach_id)
        if row is not None:
            row.update({
                "response_text": text,
                "response_intent": intent,
                "responded_at": datetime.now(timezone.utc).isoformat(),
            })

    # -----------------------------------------------------------------
    # promises
    # -----------------------------------------------------------------

    def insert_promise(self, row: dict) -> dict | None:
        stored = dict(row)
        stored.setdefault("id", str(uuid.uuid4()))
        stored.setdefault("status", "pending")
        stored.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        self._promises[stored["id"]] = stored
        return dict(stored)

    def active_promise(self, case_id: str) -> dict | None:
        """Feeds gate G10 — a live promise pauses other outreach."""
        for row in self._promises.values():
            if row.get("case_id") == case_id and row.get("status") == "pending":
                return dict(row)
        return None

    def due_promises(self, on_date: str) -> list[dict]:
        rows = [
            r for r in self._promises.values()
            if r.get("status") == "pending" and (r.get("promised_date") or "") <= on_date
        ]
        return [dict(r) for r in rows]

    def resolve_promise(self, promise_id: str, status: str) -> None:
        """status: 'kept' | 'broken'"""
        row = self._promises.get(promise_id)
        if row is not None:
            row.update({"status": status, "resolved_at": datetime.now(timezone.utc).isoformat()})

    def promises_for_case(self, case_id: str) -> list[dict]:
        rows = [r for r in self._promises.values() if r.get("case_id") == case_id]
        rows.sort(key=lambda r: r.get("created_at") or "")
        return [dict(r) for r in rows]

    def all_promises(self, batch_id: str | None = None) -> list[dict]:
        """For the kept-promise-rate metric."""
        if batch_id is None:
            return [dict(r) for r in self._promises.values()]
        case_ids = {c["id"] for c in self.list_cases(batch_id=batch_id)}
        return [dict(r) for r in self._promises.values() if r.get("case_id") in case_ids]

    # -----------------------------------------------------------------
    # audit_log (append-only — no update/delete methods exist here, by design)
    # -----------------------------------------------------------------

    def append_audit(self, row: dict) -> None:
        stored = dict(row)
        self._audit_seq += 1
        stored.setdefault("id", self._audit_seq)
        stored.setdefault("ts", datetime.now(timezone.utc).isoformat())
        self._audit_log.append(stored)

    def audit_for_case(self, case_id: str) -> list[dict]:
        rows = [r for r in self._audit_log if r.get("case_id") == case_id]
        rows.sort(key=lambda r: r.get("ts") or "")
        return [dict(r) for r in rows]

    def audit_by_event(self, event_type: str, limit: int = 500) -> list[dict]:
        """Used by metrics — e.g. counting GATE_BLOCK rows per gate."""
        rows = [r for r in self._audit_log if r.get("event_type") == event_type]
        return [dict(r) for r in rows[:limit]]

    # -----------------------------------------------------------------
    # gate context — one call gathers everything policy_gate.check() needs
    # -----------------------------------------------------------------

    def gate_context(self, case_id: str) -> dict:
        return {
            "last_contact_at": self.last_outreach_at(case_id),
            "has_active_promise": self.active_promise(case_id) is not None,
            "pre_debit_notice_at": self._pre_debit_notice_at(case_id),
        }

    def _pre_debit_notice_at(self, case_id: str) -> str | None:
        """When the RBI pre-debit notice was sent. Feeds gate G9."""
        rows = [
            r for r in self._outreach.values()
            if r.get("case_id") == case_id and r.get("channel") == "pre_debit_notice"
        ]
        if not rows:
            return None
        rows.sort(key=lambda r: r.get("sent_at") or "")
        return rows[-1].get("sent_at")

    # -----------------------------------------------------------------
    # batch helpers
    # -----------------------------------------------------------------

    def clear_batch(self, batch_id: str) -> None:
        """
        Wipe a batch before a re-run. Cases cascade to attempts/outreach/
        promises, mirroring the real schema's ON DELETE CASCADE.

        audit_log rows are intentionally NOT deleted — same append-only
        contract as the real table's trigger.
        """
        case_ids = {cid for cid, c in self._cases.items() if c.get("batch_id") == batch_id}
        for cid in case_ids:
            del self._cases[cid]

        for aid in [aid for aid, a in self._attempts.items() if a.get("case_id") in case_ids]:
            key = self._attempts[aid].get("idempotency_key")
            del self._attempts[aid]
            if key:
                self._attempt_ids_by_key.pop(key, None)

        for oid in [oid for oid, o in self._outreach.items() if o.get("case_id") in case_ids]:
            del self._outreach[oid]

        for pid in [pid for pid, p in self._promises.items() if p.get("case_id") in case_ids]:
            del self._promises[pid]

    # -----------------------------------------------------------------
    # dump — NOT part of repository.py's interface. Used only by
    # batch_scanner to bulk-flush a finished memory run to Supabase.
    # -----------------------------------------------------------------

    def dump_cases(self) -> list[dict]:
        return [dict(r) for r in self._cases.values()]

    def dump_attempts(self) -> list[dict]:
        return [dict(r) for r in self._attempts.values()]

    def dump_outreach(self) -> list[dict]:
        return [dict(r) for r in self._outreach.values()]

    def dump_promises(self) -> list[dict]:
        return [dict(r) for r in self._promises.values()]

    def dump_audit_log(self) -> list[dict]:
        return [dict(r) for r in self._audit_log]
