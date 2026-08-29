"""
Tests for the action executor. Governance is exercised for real (check() is
pure, no reason to mock it) — only the DB layer (repository) and the live
Razorpay call are faked, so these tests never touch a network or a database.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import DEFAULT_POLICY
from app.execution import actions

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeRepository:
    """In-memory stand-in for app.db.repository, covering only the functions
    actions.py calls. Mirrors the real module's idempotency behaviour:
    insert_attempt is keyed by idempotency_key and never creates a second row
    for a key it has already seen."""

    def __init__(self):
        self.attempts_by_key: dict[str, dict] = {}
        self.attempts_created = 0
        self.outreach: list[dict] = []
        self.case_updates: list[dict] = []
        self.increment_calls = 0
        self.last_contact_at: str | None = None
        self.has_active_promise: bool = False
        self.pre_debit_notice_at: str | None = None

    def gate_context(self, case_id):
        return {
            "last_contact_at": self.last_contact_at,
            "has_active_promise": self.has_active_promise,
            "pre_debit_notice_at": self.pre_debit_notice_at,
        }

    def insert_attempt(self, attempt):
        key = attempt["idempotency_key"]
        if key in self.attempts_by_key:
            return self.attempts_by_key[key]
        row = {**attempt, "id": f"attempt_{len(self.attempts_by_key) + 1}"}
        self.attempts_by_key[key] = row
        self.attempts_created += 1
        return row

    def get_attempt_by_key(self, key):
        return self.attempts_by_key.get(key)

    def update_attempt(self, attempt_id, **fields):
        for row in self.attempts_by_key.values():
            if row["id"] == attempt_id:
                row.update(fields)

    def insert_outreach(self, row):
        stored = {**row, "id": f"outreach_{len(self.outreach) + 1}"}
        self.outreach.append(stored)
        if stored.get("channel") == "pre_debit_notice":
            self.pre_debit_notice_at = stored["sent_at"]
        self.last_contact_at = stored["sent_at"]
        return stored

    def increment_attempts(self, case_id):
        self.increment_calls += 1
        return self.increment_calls

    def update_case(self, case_id, **fields):
        self.case_updates.append({"case_id": case_id, **fields})
        return {"id": case_id, **fields}


class FakeRazorpay:
    """Stands in for app.execution.razorpay_client.create_payment_link."""

    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.calls = 0
        self.last_customer = None
        self.last_purpose = None

    def create_payment_link(self, amount, customer, idempotency_key, *, purpose="payment"):
        self.calls += 1
        self.last_customer = customer
        self.last_purpose = purpose
        if self.calls <= self.fail_times:
            raise ConnectionError(f"simulated Razorpay outage (call {self.calls})")
        return {"id": f"plink_{idempotency_key}", "status": "created"}


@pytest.fixture
def fake_repo(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(actions.repository, "gate_context", repo.gate_context)
    monkeypatch.setattr(actions.repository, "insert_attempt", repo.insert_attempt)
    monkeypatch.setattr(actions.repository, "get_attempt_by_key", repo.get_attempt_by_key)
    monkeypatch.setattr(actions.repository, "update_attempt", repo.update_attempt)
    monkeypatch.setattr(actions.repository, "insert_outreach", repo.insert_outreach)
    monkeypatch.setattr(actions.repository, "increment_attempts", repo.increment_attempts)
    monkeypatch.setattr(actions.repository, "update_case", repo.update_case)
    return repo


@pytest.fixture(autouse=True)
def no_audit_writes(monkeypatch):
    monkeypatch.setattr(actions.audit_log, "record", lambda *a, **k: None)
    monkeypatch.setattr(actions.audit_log, "gate", lambda *a, **k: None)
    monkeypatch.setattr(actions.audit_log, "error", lambda *a, **k: None)
    monkeypatch.setattr(actions.audit_log, "money_action", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    monkeypatch.setattr(actions.time, "sleep", lambda s: None)


def make_case(**overrides) -> dict:
    case = {
        "id": "case_test000001",
        "source": "subscription",
        "reason_category": "expired_card",
        "amount": 499.0,
        "attempts_made": 0,
        "opted_out": False,
        "customer_ref": "Priya Sharma",
        "created_at": (NOW - timedelta(days=1)).isoformat(),
    }
    case.update(overrides)
    return case


def make_decision(**overrides) -> dict:
    decision = {
        "intervention": "send_link",
        "scheduled_for": None,
        "channel": "whatsapp",
        "message": "Yeh raha aapka payment link.",
        "discount_pct": 0.0,
        "is_mandate_debit": False,
        "reasoning": "Card update wasn't completed; falling back to a direct link.",
    }
    decision.update(overrides)
    return decision


# ---------------------------------------------------------------------------
# a blocked action executes nothing, still writes a GATE_BLOCK audit row
# ---------------------------------------------------------------------------

def test_blocked_action_executes_nothing_and_writes_gate_block(fake_repo, monkeypatch):
    gate_calls = []
    monkeypatch.setattr(actions.audit_log, "gate", lambda case_id, gate, action: gate_calls.append(gate))

    case = make_case(opted_out=True)  # G2: opt-out blocks every action type
    decision = make_decision(intervention="request_re_mandate", is_mandate_debit=False)

    result = actions.execute(decision, case, DEFAULT_POLICY, live=False, now=NOW)

    assert result["executed"] is False
    assert result["gate"] == "G2"
    assert fake_repo.outreach == []
    assert fake_repo.attempts_by_key == {}
    assert fake_repo.increment_calls == 0
    assert fake_repo.case_updates == []

    assert len(gate_calls) == 1
    assert gate_calls[0].allowed is False
    assert gate_calls[0].gate == "G2"


def test_blocked_action_still_logs_the_gate_verdict_for_an_allowed_case_too(fake_repo, monkeypatch):
    """Sanity check the other direction: an ALLOWED verdict is logged too,
    proving audit.log.gate runs on every check, not just blocks."""
    gate_calls = []
    monkeypatch.setattr(actions.audit_log, "gate", lambda case_id, gate, action: gate_calls.append(gate))

    case = make_case()
    decision = make_decision()
    actions.execute(decision, case, DEFAULT_POLICY, live=False, now=NOW)

    assert len(gate_calls) == 1
    assert gate_calls[0].allowed is True


# ---------------------------------------------------------------------------
# duplicate idempotency key never creates a second attempt
# ---------------------------------------------------------------------------

def test_duplicate_idempotency_key_never_creates_a_second_attempt(fake_repo):
    """Simulate the realistic failure mode this protects against: the same
    logical attempt reaching execute() twice (a duplicate webhook, a retried
    scheduler tick). Seed the repo with the attempt already recorded under
    the key execute() will compute, then call execute() once more and prove
    nothing new is created — a live call sent seconds apart from the first
    would otherwise also collide with G4's contact gap, which isn't what
    this test is about."""
    case = make_case(reason_category="checkout_dropoff", source="checkout", attempts_made=1)
    decision = make_decision(intervention="send_link_with_offer", discount_pct=10.0)
    idem_key = f"{case['id']}:send_link_with_offer:2"
    fake_repo.attempts_by_key[idem_key] = {
        "id": "attempt_seed", "idempotency_key": idem_key, "result": "success",
    }

    result = actions.execute(decision, case, DEFAULT_POLICY, live=False, now=NOW)

    assert result["executed"] is True
    assert result.get("reused") is True
    assert fake_repo.attempts_created == 0          # no NEW attempt row
    assert len(fake_repo.attempts_by_key) == 1       # still just the seeded one
    assert fake_repo.increment_calls == 0            # not double-counted
    assert len(fake_repo.outreach) == 0              # message not (re)sent


def test_duplicate_idempotency_key_never_calls_razorpay(fake_repo, monkeypatch):
    fake_rp = FakeRazorpay()
    monkeypatch.setattr(actions.razorpay_client, "create_payment_link", fake_rp.create_payment_link)

    case = make_case(reason_category="checkout_dropoff", source="checkout", attempts_made=1)
    decision = make_decision(intervention="send_link_with_offer", discount_pct=10.0)
    idem_key = f"{case['id']}:send_link_with_offer:2"
    fake_repo.attempts_by_key[idem_key] = {
        "id": "attempt_seed", "idempotency_key": idem_key, "result": "success",
    }

    actions.execute(decision, case, DEFAULT_POLICY, live=True, now=NOW)

    assert fake_rp.calls == 0


# ---------------------------------------------------------------------------
# three consecutive failures produce ESCALATED, not a silent drop
# ---------------------------------------------------------------------------

def test_three_consecutive_failures_escalate(fake_repo, monkeypatch):
    fake_rp = FakeRazorpay(fail_times=99)  # always fails
    monkeypatch.setattr(actions.razorpay_client, "create_payment_link", fake_rp.create_payment_link)

    case = make_case(reason_category="checkout_dropoff", source="checkout", attempts_made=1)
    decision = make_decision(intervention="send_link_with_offer", discount_pct=10.0)

    result = actions.execute(decision, case, DEFAULT_POLICY, live=True, now=NOW)

    assert result["executed"] is False
    assert result["escalated"] is True
    assert fake_rp.calls == len(DEFAULT_POLICY["action_retry_delays_s"])  # 3 attempts, never more
    assert any(u.get("state") == "ESCALATED" for u in fake_repo.case_updates)
    # never dropped silently: the attempt row is marked pending, not failed
    attempt = next(iter(fake_repo.attempts_by_key.values()))
    assert attempt["result"] == "pending"
    # never counted as a completed attempt
    assert fake_repo.increment_calls == 0


def test_failure_then_recovery_within_the_retry_budget_succeeds(fake_repo, monkeypatch):
    fake_rp = FakeRazorpay(fail_times=2)  # fails twice, succeeds on the 3rd
    monkeypatch.setattr(actions.razorpay_client, "create_payment_link", fake_rp.create_payment_link)

    case = make_case(reason_category="checkout_dropoff", source="checkout", attempts_made=1)
    decision = make_decision(intervention="send_link_with_offer", discount_pct=10.0)

    result = actions.execute(decision, case, DEFAULT_POLICY, live=True, now=NOW)

    assert result["executed"] is True
    assert result.get("escalated") is not True
    assert fake_rp.calls == 3
    attempt = next(iter(fake_repo.attempts_by_key.values()))
    assert attempt["result"] == "success"


def test_razorpay_call_gets_a_customer_dict_not_a_string(fake_repo, monkeypatch):
    fake_rp = FakeRazorpay()
    monkeypatch.setattr(actions.razorpay_client, "create_payment_link", fake_rp.create_payment_link)

    case = make_case(
        reason_category="checkout_dropoff", source="checkout", attempts_made=1,
        customer_ref="Priya Sharma", customer_phone="98765**43",
    )
    decision = make_decision(intervention="send_link_with_offer", discount_pct=10.0)

    actions.execute(decision, case, DEFAULT_POLICY, live=True, now=NOW)

    assert fake_rp.last_customer == {"name": "Priya Sharma", "contact": "98765**43"}
    assert fake_rp.last_purpose == "checkout completion"


# ---------------------------------------------------------------------------
# pre-debit notice flow (gate G9)
# ---------------------------------------------------------------------------

def test_mandate_debit_first_call_sends_notice_and_defers_the_charge(fake_repo):
    case = make_case(reason_category="insufficient_funds")
    decision = make_decision(
        intervention="retry_after_date",
        is_mandate_debit=True,
        scheduled_for=(NOW + timedelta(days=10)).isoformat(),
    )

    result = actions.execute(decision, case, DEFAULT_POLICY, live=False, now=NOW)

    assert result["intervention"] == "pre_debit_notice"
    assert len(fake_repo.outreach) == 1
    assert fake_repo.outreach[0]["channel"] == "pre_debit_notice"
    assert fake_repo.attempts_by_key == {}  # no charge attempted yet
    assert fake_repo.case_updates == []     # state untouched this cycle
    # the scheduler needs to know when to re-queue this case
    expected_retry_at = NOW + timedelta(hours=DEFAULT_POLICY["rbi_pre_debit_notice_hours"])
    assert result["retry_at"] == expected_retry_at.isoformat()


def test_mandate_debit_waits_while_notice_is_still_aging(fake_repo):
    notice_sent_at = NOW - timedelta(hours=2)
    fake_repo.pre_debit_notice_at = notice_sent_at.isoformat()  # < 24h

    case = make_case(reason_category="insufficient_funds")
    decision = make_decision(intervention="retry_after_date", is_mandate_debit=True)

    result = actions.execute(decision, case, DEFAULT_POLICY, live=False, now=NOW)

    assert result["executed"] is False
    assert result["gate"] is None  # deferred before ever reaching the gate
    assert fake_repo.attempts_by_key == {}
    assert len(fake_repo.outreach) == 0  # notice not re-sent
    # without retry_at, a mandate-debit case would stall forever
    expected_retry_at = notice_sent_at + timedelta(hours=DEFAULT_POLICY["rbi_pre_debit_notice_hours"])
    assert result["retry_at"] == expected_retry_at.isoformat()


def test_mandate_debit_charges_once_the_notice_has_aged(fake_repo):
    fake_repo.pre_debit_notice_at = (NOW - timedelta(hours=25)).isoformat()  # > 24h

    case = make_case(reason_category="insufficient_funds")
    decision = make_decision(intervention="retry_after_date", is_mandate_debit=True)

    result = actions.execute(decision, case, DEFAULT_POLICY, live=False, now=NOW)

    assert result["executed"] is True
    assert result["intervention"] == "retry_after_date"
    assert len(fake_repo.attempts_by_key) == 1
    assert any(u.get("state") == "RETRYING" for u in fake_repo.case_updates)


def test_mandate_debit_notice_itself_is_blocked_by_opt_out(fake_repo):
    case = make_case(reason_category="insufficient_funds", opted_out=True)
    decision = make_decision(intervention="retry_after_date", is_mandate_debit=True)

    result = actions.execute(decision, case, DEFAULT_POLICY, live=False, now=NOW)

    assert result["executed"] is False
    assert result["gate"] == "G2"
    assert fake_repo.outreach == []
