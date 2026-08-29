"""
Tests for app/db/memory_repository.py in isolation — no batch_scanner, no
pipeline, just the repository interface itself, exercised the same way
tests/test_governance.py exercises policy_gate.check(): every documented
behavior gets a direct test, especially the two golden-rule-critical ones —
insert_attempt's idempotency-key conflict and update_case's terminal-state
regression guard.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.db.memory_repository import MemoryRepository

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo() -> MemoryRepository:
    return MemoryRepository()


def make_case(**overrides) -> dict:
    case = {"id": "case_1", "source": "subscription", "customer_ref": "Priya Sharma", "amount": 499.0}
    case.update(overrides)
    return case


# ---------------------------------------------------------------------------
# cases
# ---------------------------------------------------------------------------

def test_insert_case_applies_schema_defaults(repo):
    row = repo.insert_case(make_case())
    assert row["state"] == "DETECTED"
    assert row["attempts_made"] == 0
    assert row["opted_out"] is False
    assert row["recovered_amount"] == 0.0
    assert row["batch_id"] == "live"
    assert row["currency"] == "INR"
    assert row["created_at"] and row["updated_at"]


def test_insert_case_respects_explicit_fields_over_defaults(repo):
    row = repo.insert_case(make_case(batch_id="dev", state="DIAGNOSED", attempts_made=2))
    assert row["batch_id"] == "dev"
    assert row["state"] == "DIAGNOSED"
    assert row["attempts_made"] == 2


def test_get_case_returns_a_copy_not_the_internal_row(repo):
    repo.insert_case(make_case())
    row = repo.get_case("case_1")
    row["state"] = "TAMPERED"
    assert repo.get_case("case_1")["state"] == "DETECTED"


def test_get_case_missing_returns_none(repo):
    assert repo.get_case("nope") is None


def test_upsert_case_inserts_when_absent(repo):
    row = repo.upsert_case(make_case())
    assert row["id"] == "case_1"
    assert repo.get_case("case_1") is not None


def test_upsert_case_updates_when_present(repo):
    repo.insert_case(make_case())
    row = repo.upsert_case(make_case(amount=999.0))
    assert row["amount"] == 999.0
    assert repo.get_case("case_1")["amount"] == 999.0


def test_list_cases_filters_by_batch_id_and_state(repo):
    repo.insert_case(make_case(id="a", batch_id="dev", state="DETECTED"))
    repo.insert_case(make_case(id="b", batch_id="dev", state="RECOVERED"))
    repo.insert_case(make_case(id="c", batch_id="holdout", state="DETECTED"))

    assert {c["id"] for c in repo.list_cases(batch_id="dev")} == {"a", "b"}
    assert {c["id"] for c in repo.list_cases(batch_id="dev", state="RECOVERED")} == {"b"}
    assert {c["id"] for c in repo.list_cases()} == {"a", "b", "c"}


def test_list_cases_respects_limit(repo):
    for i in range(5):
        repo.insert_case(make_case(id=f"case_{i}"))
    assert len(repo.list_cases(limit=2)) == 2


# ---------------------------------------------------------------------------
# update_case — the terminal-state regression guard
# ---------------------------------------------------------------------------

def test_update_case_missing_returns_none(repo):
    assert repo.update_case("nope", state="DIAGNOSED") is None


def test_update_case_applies_a_normal_transition(repo):
    repo.insert_case(make_case())
    row = repo.update_case("case_1", state="DIAGNOSED", reason_category="expired_card")
    assert row["state"] == "DIAGNOSED"
    assert row["reason_category"] == "expired_card"


def test_update_case_blocks_regression_out_of_a_terminal_state(repo):
    repo.insert_case(make_case())
    repo.update_case("case_1", state="RECOVERED")
    row = repo.update_case("case_1", state="OUTREACH_SENT")  # out-of-order webhook
    assert row["state"] == "RECOVERED"  # unchanged


def test_update_case_allows_moving_between_two_terminal_states(repo):
    """G1's terminal set has no internal ordering — RECOVERED -> CLOSED_LOST
    is still 'staying terminal', not a regression, so it's allowed."""
    repo.insert_case(make_case())
    repo.update_case("case_1", state="RECOVERED")
    row = repo.update_case("case_1", state="CLOSED_LOST")
    assert row["state"] == "CLOSED_LOST"


def test_update_case_allows_non_state_fields_even_when_terminal(repo):
    repo.insert_case(make_case())
    repo.update_case("case_1", state="RECOVERED")
    row = repo.update_case("case_1", recovered_amount=499.0)
    assert row["recovered_amount"] == 499.0
    assert row["state"] == "RECOVERED"


def test_mark_recovered_sets_state_amount_and_timestamp(repo):
    repo.insert_case(make_case())
    row = repo.mark_recovered("case_1", 499.0)
    assert row["state"] == "RECOVERED"
    assert row["recovered_amount"] == 499.0
    assert row["recovered_at"]


def test_increment_attempts_counts_up_and_persists(repo):
    repo.insert_case(make_case())
    assert repo.increment_attempts("case_1") == 1
    assert repo.increment_attempts("case_1") == 2
    assert repo.get_case("case_1")["attempts_made"] == 2


def test_increment_attempts_missing_case_returns_zero(repo):
    assert repo.increment_attempts("nope") == 0


# ---------------------------------------------------------------------------
# payment_attempts — the idempotency-key conflict
# ---------------------------------------------------------------------------

def test_insert_attempt_returns_a_new_row(repo):
    row = repo.insert_attempt({"case_id": "case_1", "attempt_no": 1, "idempotency_key": "case_1:retry:1"})
    assert row["id"]
    assert row["idempotency_key"] == "case_1:retry:1"


def test_insert_attempt_with_duplicate_key_returns_the_existing_row(repo):
    first = repo.insert_attempt({"case_id": "case_1", "attempt_no": 1, "idempotency_key": "k1", "result": "success"})
    second = repo.insert_attempt({"case_id": "case_1", "attempt_no": 1, "idempotency_key": "k1", "result": "pending"})
    assert second["id"] == first["id"]
    assert second["result"] == "success"  # the original, not the duplicate's payload


def test_insert_attempt_duplicate_never_creates_a_second_row(repo):
    for _ in range(3):
        repo.insert_attempt({"case_id": "case_1", "attempt_no": 1, "idempotency_key": "k1"})
    assert len(repo.dump_attempts()) == 1


def test_get_attempt_by_key_missing_returns_none(repo):
    assert repo.get_attempt_by_key("nope") is None


def test_attempts_for_case_ordered_by_attempt_no(repo):
    repo.insert_attempt({"case_id": "case_1", "attempt_no": 2, "idempotency_key": "k2"})
    repo.insert_attempt({"case_id": "case_1", "attempt_no": 1, "idempotency_key": "k1"})
    rows = repo.attempts_for_case("case_1")
    assert [r["attempt_no"] for r in rows] == [1, 2]


def test_update_attempt_mutates_the_stored_row(repo):
    row = repo.insert_attempt({"case_id": "case_1", "attempt_no": 1, "idempotency_key": "k1"})
    repo.update_attempt(row["id"], result="success", razorpay_ref="plink_1")
    assert repo.get_attempt_by_key("k1")["result"] == "success"


def test_update_attempt_missing_id_is_a_silent_no_op(repo):
    repo.update_attempt("nope", result="success")  # must not raise


# ---------------------------------------------------------------------------
# outreach
# ---------------------------------------------------------------------------

def test_insert_outreach_applies_defaults(repo):
    row = repo.insert_outreach({"case_id": "case_1", "channel": "whatsapp", "message": "hi"})
    assert row["direction"] == "outbound"
    assert row["language"] == "hinglish"
    assert row["sent_at"]


def test_last_outreach_at_only_counts_outbound(repo):
    repo.insert_outreach({"case_id": "case_1", "channel": "whatsapp", "message": "hi", "sent_at": "2026-09-01T00:00:00+00:00"})
    repo.insert_outreach({"case_id": "case_1", "channel": "whatsapp", "message": "reply", "direction": "inbound", "sent_at": "2026-09-02T00:00:00+00:00"})
    assert repo.last_outreach_at("case_1") == "2026-09-01T00:00:00+00:00"


def test_last_outreach_at_picks_the_most_recent(repo):
    repo.insert_outreach({"case_id": "case_1", "channel": "whatsapp", "message": "1", "sent_at": "2026-09-01T00:00:00+00:00"})
    repo.insert_outreach({"case_id": "case_1", "channel": "whatsapp", "message": "2", "sent_at": "2026-09-05T00:00:00+00:00"})
    assert repo.last_outreach_at("case_1") == "2026-09-05T00:00:00+00:00"


def test_last_outreach_at_no_outreach_returns_none(repo):
    assert repo.last_outreach_at("case_1") is None


def test_outreach_for_case_ordered_by_sent_at(repo):
    repo.insert_outreach({"case_id": "case_1", "channel": "whatsapp", "message": "2", "sent_at": "2026-09-05T00:00:00+00:00"})
    repo.insert_outreach({"case_id": "case_1", "channel": "whatsapp", "message": "1", "sent_at": "2026-09-01T00:00:00+00:00"})
    rows = repo.outreach_for_case("case_1")
    assert [r["message"] for r in rows] == ["1", "2"]


def test_record_reply_sets_response_fields(repo):
    row = repo.insert_outreach({"case_id": "case_1", "channel": "whatsapp", "message": "hi"})
    repo.record_reply(row["id"], "haan abhi karta hun", "pay_now")
    stored = repo.outreach_for_case("case_1")[0]
    assert stored["response_text"] == "haan abhi karta hun"
    assert stored["response_intent"] == "pay_now"
    assert stored["responded_at"]


# ---------------------------------------------------------------------------
# promises
# ---------------------------------------------------------------------------

def test_insert_promise_defaults_to_pending(repo):
    row = repo.insert_promise({"case_id": "case_1", "promised_amount": 499.0, "promised_date": "2026-09-20"})
    assert row["status"] == "pending"
    assert row["id"]


def test_active_promise_only_finds_pending(repo):
    row = repo.insert_promise({"case_id": "case_1", "promised_amount": 499.0, "promised_date": "2026-09-20"})
    assert repo.active_promise("case_1")["id"] == row["id"]
    repo.resolve_promise(row["id"], "kept")
    assert repo.active_promise("case_1") is None


def test_due_promises_filters_by_date_and_status(repo):
    repo.insert_promise({"case_id": "case_1", "promised_amount": 100.0, "promised_date": "2026-09-10"})
    repo.insert_promise({"case_id": "case_2", "promised_amount": 200.0, "promised_date": "2026-09-20"})
    due = repo.due_promises("2026-09-15")
    assert {p["case_id"] for p in due} == {"case_1"}


def test_due_promises_excludes_already_resolved(repo):
    row = repo.insert_promise({"case_id": "case_1", "promised_amount": 100.0, "promised_date": "2026-09-10"})
    repo.resolve_promise(row["id"], "kept")
    assert repo.due_promises("2026-09-15") == []


def test_resolve_promise_sets_status_and_resolved_at(repo):
    row = repo.insert_promise({"case_id": "case_1", "promised_amount": 100.0, "promised_date": "2026-09-10"})
    repo.resolve_promise(row["id"], "broken")
    stored = repo.all_promises()[0]
    assert stored["status"] == "broken"
    assert stored["resolved_at"]


def test_all_promises_without_batch_id_returns_everything(repo):
    repo.insert_promise({"case_id": "case_1", "promised_amount": 1.0, "promised_date": "2026-09-10"})
    repo.insert_promise({"case_id": "case_2", "promised_amount": 1.0, "promised_date": "2026-09-10"})
    assert len(repo.all_promises()) == 2


def test_all_promises_filters_by_batch_id_via_case_membership(repo):
    repo.insert_case(make_case(id="case_1", batch_id="dev"))
    repo.insert_case(make_case(id="case_2", batch_id="holdout"))
    repo.insert_promise({"case_id": "case_1", "promised_amount": 1.0, "promised_date": "2026-09-10"})
    repo.insert_promise({"case_id": "case_2", "promised_amount": 1.0, "promised_date": "2026-09-10"})
    dev_promises = repo.all_promises(batch_id="dev")
    assert len(dev_promises) == 1
    assert dev_promises[0]["case_id"] == "case_1"


# ---------------------------------------------------------------------------
# audit_log — append-only
# ---------------------------------------------------------------------------

def test_append_audit_assigns_incrementing_ids(repo):
    repo.append_audit({"case_id": "case_1", "actor": "test", "event_type": "DETECTED"})
    repo.append_audit({"case_id": "case_1", "actor": "test", "event_type": "DIAGNOSED"})
    rows = repo.audit_for_case("case_1")
    assert rows[0]["id"] < rows[1]["id"]


def test_append_audit_defaults_ts_when_missing(repo):
    repo.append_audit({"case_id": "case_1", "actor": "test", "event_type": "DETECTED"})
    assert repo.audit_for_case("case_1")[0]["ts"]


def test_audit_for_case_ordered_by_ts(repo):
    repo.append_audit({"case_id": "case_1", "actor": "t", "event_type": "DETECTED", "ts": "2026-09-05T00:00:00+00:00"})
    repo.append_audit({"case_id": "case_1", "actor": "t", "event_type": "DIAGNOSED", "ts": "2026-09-01T00:00:00+00:00"})
    rows = repo.audit_for_case("case_1")
    assert [r["event_type"] for r in rows] == ["DIAGNOSED", "DETECTED"]


def test_audit_by_event_filters_and_limits(repo):
    for _ in range(3):
        repo.append_audit({"case_id": "case_1", "actor": "t", "event_type": "GATE_BLOCK"})
    repo.append_audit({"case_id": "case_1", "actor": "t", "event_type": "GATE_ALLOW"})
    assert len(repo.audit_by_event("GATE_BLOCK")) == 3
    assert len(repo.audit_by_event("GATE_BLOCK", limit=2)) == 2


def test_audit_log_is_never_mutated_by_other_operations(repo):
    repo.append_audit({"case_id": "case_1", "actor": "t", "event_type": "DETECTED"})
    repo.insert_case(make_case())
    repo.update_case("case_1", state="DIAGNOSED")
    repo.clear_batch("live")
    assert len(repo.audit_for_case("case_1")) == 1


# ---------------------------------------------------------------------------
# gate_context
# ---------------------------------------------------------------------------

def test_gate_context_reflects_outreach_and_promise_and_notice_state(repo):
    assert repo.gate_context("case_1") == {
        "last_contact_at": None, "has_active_promise": False, "pre_debit_notice_at": None,
    }

    repo.insert_outreach({"case_id": "case_1", "channel": "pre_debit_notice", "message": "notice", "sent_at": "2026-09-01T00:00:00+00:00"})
    repo.insert_promise({"case_id": "case_1", "promised_amount": 1.0, "promised_date": "2026-09-10"})

    ctx = repo.gate_context("case_1")
    assert ctx["last_contact_at"] == "2026-09-01T00:00:00+00:00"
    assert ctx["has_active_promise"] is True
    assert ctx["pre_debit_notice_at"] == "2026-09-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# clear_batch — cascades to attempts/outreach/promises, spares audit_log
# ---------------------------------------------------------------------------

def test_clear_batch_removes_only_that_batchs_cases(repo):
    repo.insert_case(make_case(id="a", batch_id="dev"))
    repo.insert_case(make_case(id="b", batch_id="holdout"))
    repo.clear_batch("dev")
    assert repo.get_case("a") is None
    assert repo.get_case("b") is not None


def test_clear_batch_cascades_to_attempts_outreach_and_promises(repo):
    repo.insert_case(make_case(id="a", batch_id="dev"))
    repo.insert_attempt({"case_id": "a", "attempt_no": 1, "idempotency_key": "a:retry:1"})
    repo.insert_outreach({"case_id": "a", "channel": "whatsapp", "message": "hi"})
    repo.insert_promise({"case_id": "a", "promised_amount": 1.0, "promised_date": "2026-09-10"})

    repo.clear_batch("dev")

    assert repo.dump_attempts() == []
    assert repo.dump_outreach() == []
    assert repo.dump_promises() == []
    assert repo.get_attempt_by_key("a:retry:1") is None  # the key index is cleared too


def test_clear_batch_never_touches_audit_log(repo):
    repo.insert_case(make_case(id="a", batch_id="dev"))
    repo.append_audit({"case_id": "a", "actor": "t", "event_type": "DETECTED"})
    repo.clear_batch("dev")
    assert len(repo.audit_for_case("a")) == 1


# ---------------------------------------------------------------------------
# dump_* — the bulk-flush surface batch_scanner uses
# ---------------------------------------------------------------------------

def test_dump_methods_return_everything_and_are_copies(repo):
    repo.insert_case(make_case())
    repo.insert_attempt({"case_id": "case_1", "attempt_no": 1, "idempotency_key": "k1"})
    repo.insert_outreach({"case_id": "case_1", "channel": "whatsapp", "message": "hi"})
    repo.insert_promise({"case_id": "case_1", "promised_amount": 1.0, "promised_date": "2026-09-10"})
    repo.append_audit({"case_id": "case_1", "actor": "t", "event_type": "DETECTED"})

    assert len(repo.dump_cases()) == 1
    assert len(repo.dump_attempts()) == 1
    assert len(repo.dump_outreach()) == 1
    assert len(repo.dump_promises()) == 1
    assert len(repo.dump_audit_log()) == 1

    dumped = repo.dump_cases()
    dumped[0]["state"] = "TAMPERED"
    assert repo.get_case("case_1")["state"] != "TAMPERED"
