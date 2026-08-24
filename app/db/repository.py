"""
Data-access layer. ALL database reads/writes go through here.

Why this exists: it is the seam that keeps us portable. If Supabase is down or
we want to run offline, we swap the implementation in this one file and nothing
else in the codebase changes. Never call get_client() directly from pipeline
code -- call a repository function.
"""
from __future__ import annotations

from typing import Any

# TODO: def insert_case(case: dict) -> dict
# TODO: def get_case(case_id: str) -> dict | None
# TODO: def update_case_state(case_id: str, state: str, **fields) -> None
# TODO: def list_cases(batch_id: str | None = None, state: str | None = None) -> list[dict]
# TODO: def insert_attempt(attempt: dict) -> dict        # unique idempotency_key
# TODO: def attempts_for_case(case_id: str) -> list[dict]
# TODO: def insert_outreach(row: dict) -> dict
# TODO: def last_outreach_at(case_id: str) -> str | None  # for min_contact_gap
# TODO: def insert_promise(row: dict) -> dict
# TODO: def due_promises(on_date: str) -> list[dict]
# TODO: def resolve_promise(promise_id: str, status: str) -> None
# TODO: def append_audit(row: dict) -> None               # append-only
# TODO: def audit_for_case(case_id: str) -> list[dict]
