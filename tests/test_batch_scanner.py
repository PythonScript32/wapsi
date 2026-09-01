"""
Tests for the batch runner. No real DB, no real Razorpay, no real clock: a
FakeRepository stands in for app.db.repository (shared by batch_scanner AND
the actions/classifier/decision modules it drives, since they all import the
same module object), live is always False so actions.py only simulates, and
the simulated clock is seeded via run_batch(..., now=...) for determinism.

Every run_batch() call here passes persist="supabase" — that tells
run_batch() NOT to perform its own persist="memory" swap, so it runs against
whatever repository.* functions are already in effect, which is exactly this
file's own pre-patched FakeRepository. persist="memory" (the default) and
app/db/memory_repository.py's own correctness are covered separately in
tests/test_memory_repository.py.

Most of the tricky logic (salary-day grading, reply routing, naive baseline)
is tested as pure functions first; a handful of run_batch() tests then check
the day-by-day wiring end to end.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

import app.audit.log as audit_log_module
import app.db.repository as repository_module
from app.config import DEFAULT_POLICY
from app.detection import batch_scanner as bs
from app.detection import synthetic_data as sd
from app.scheduler import jobs as scheduler_jobs

NOW = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)  # mid-month, month has 20 days left

# Captured before the autouse no_audit_writes fixture ever runs, so a test
# that needs REAL audit rows (e.g. to prove they flush correctly) can
# restore them for just that test via monkeypatch.
_REAL_AUDIT_RECORD = audit_log_module.record
_REAL_AUDIT_GATE = audit_log_module.gate
_REAL_AUDIT_ERROR = audit_log_module.error
_REAL_AUDIT_MONEY_ACTION = audit_log_module.money_action


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeRepository:
    def __init__(self):
        self.cases: dict[str, dict] = {}
        self.attempts_by_key: dict[str, dict] = {}
        self.outreach: list[dict] = []
        self.promises: dict[str, dict] = {}
        self._next_promise_id = 1
        self.case_updates: list[dict] = []
        self.recovered: list[tuple] = []
        self.last_contact_at: dict[str, str] = {}
        self.has_active_promise: dict[str, bool] = {}
        self.pre_debit_notice_at: dict[str, str] = {}
        self.audit_log: list[dict] = []

    def clear_batch(self, batch_id):
        self.cases = {cid: c for cid, c in self.cases.items() if c.get("batch_id") != batch_id}

    def insert_case(self, case):
        self.cases[case["id"]] = dict(case)
        return self.cases[case["id"]]

    def gate_context(self, case_id):
        return {
            "last_contact_at": self.last_contact_at.get(case_id),
            "has_active_promise": self.has_active_promise.get(case_id, False),
            "pre_debit_notice_at": self.pre_debit_notice_at.get(case_id),
        }

    def insert_attempt(self, attempt):
        key = attempt["idempotency_key"]
        if key in self.attempts_by_key:
            return self.attempts_by_key[key]
        row = {**attempt, "id": f"attempt_{len(self.attempts_by_key) + 1}"}
        self.attempts_by_key[key] = row
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
        case_id = stored["case_id"]
        if stored.get("channel") == "pre_debit_notice":
            self.pre_debit_notice_at[case_id] = stored["sent_at"]
        self.last_contact_at[case_id] = stored["sent_at"]
        return stored

    def increment_attempts(self, case_id):
        case = self.cases.setdefault(case_id, {})
        case["attempts_made"] = int(case.get("attempts_made") or 0) + 1
        return case["attempts_made"]

    def update_case(self, case_id, **fields):
        self.case_updates.append({"case_id": case_id, **fields})
        case = self.cases.setdefault(case_id, {"id": case_id})
        case.update(fields)
        return case

    def mark_recovered(self, case_id, amount):
        self.recovered.append((case_id, amount))
        return self.update_case(case_id, state="RECOVERED", recovered_amount=amount)

    def insert_promise(self, row):
        pid = f"promise_{self._next_promise_id}"
        self._next_promise_id += 1
        stored = {**row, "id": pid}
        self.promises[pid] = stored
        self.has_active_promise[row["case_id"]] = True
        return stored

    def due_promises(self, on_date):
        return [p for p in self.promises.values() if p["status"] == "pending" and p["promised_date"] <= on_date]

    def resolve_promise(self, promise_id, status):
        p = self.promises.get(promise_id)
        if p is None:
            return
        p["status"] = status
        self.has_active_promise[p["case_id"]] = False

    def attempts_for_case(self, case_id):
        return [row for row in self.attempts_by_key.values() if row.get("case_id") == case_id]

    def outreach_for_case(self, case_id):
        return [row for row in self.outreach if row.get("case_id") == case_id]

    def all_promises(self, batch_id=None):
        return list(self.promises.values())

    def promises_for_case(self, case_id):
        return [p for p in self.promises.values() if p.get("case_id") == case_id]

    def get_case(self, case_id):
        return self.cases.get(case_id)

    def append_audit(self, row):
        self.audit_log.append(dict(row))

    def audit_for_case(self, case_id):
        return [row for row in self.audit_log if row.get("case_id") == case_id]


@pytest.fixture
def fake_repo(monkeypatch):
    repo = FakeRepository()
    for name in (
        "clear_batch", "insert_case", "gate_context", "insert_attempt", "get_attempt_by_key",
        "update_attempt", "insert_outreach", "increment_attempts", "update_case", "mark_recovered",
        "insert_promise", "due_promises", "resolve_promise", "promises_for_case", "get_case",
        "attempts_for_case", "outreach_for_case", "all_promises", "append_audit", "audit_for_case",
    ):
        monkeypatch.setattr(repository_module, name, getattr(repo, name))
    return repo


@pytest.fixture(autouse=True)
def no_audit_writes(monkeypatch):
    monkeypatch.setattr(audit_log_module, "record", lambda *a, **k: None)
    monkeypatch.setattr(audit_log_module, "gate", lambda *a, **k: None)
    monkeypatch.setattr(audit_log_module, "error", lambda *a, **k: None)
    monkeypatch.setattr(audit_log_module, "money_action", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def no_snapshot_file(monkeypatch):
    """run_batch() writes data/results_{set}.json — keep tests from touching
    the real filesystem / polluting the repo's data/ directory."""
    written = {}
    monkeypatch.setattr(bs.metrics, "export_snapshot", lambda data, path: written.update(path=path, data=data))
    return written


@pytest.fixture(autouse=True)
def no_schema_check(monkeypatch):
    """run_batch() calls repository.verify_schema() first thing, which hits
    the real Supabase client -- these tests never touch the network, real
    schema state included. Schema-check behavior itself is covered directly
    in tests/test_repository.py."""
    monkeypatch.setattr(repository_module, "verify_schema", lambda: None)


def make_case(**overrides) -> dict:
    case = {
        "id": "case_test000001",
        "batch_id": "dev",
        "source": "subscription",
        "customer_ref": "Priya Sharma",
        "customer_phone": "98765**43",
        "amount": 499.0,
        "currency": "INR",
        "reason_raw": "Payment failed due to insufficient funds in the customer account",
        "reason_category": "insufficient_funds",
        "created_at": NOW.isoformat(),
        "latent": {
            "recoverable": True,
            "correct_strategy": "after_salary_day",
            "responds_to_outreach": False,
            "reply_intent": "none",
            "reply_text_hinglish": None,
            "promise_offset_days": None,
            "keeps_promise": None,
            "salary_day": None,
            "resolves_after_days": None,
        },
    }
    case.update(overrides)
    return case


def make_decision(**overrides) -> dict:
    decision = {
        "intervention": "send_link",
        "scheduled_for": None,
        "channel": "whatsapp",
        "message": "Yeh raha payment link.",
        "discount_pct": 0.0,
        "is_mandate_debit": False,
        "reasoning": "test",
    }
    decision.update(overrides)
    return decision


# ---------------------------------------------------------------------------
# _true_next_salary_date
# ---------------------------------------------------------------------------

def test_true_next_salary_date_within_same_month():
    # Sep 10, salary_day 30 -> Sep 30 (still ahead this month)
    assert bs._true_next_salary_date(NOW, 30).date() == datetime(2026, 9, 30).date()


def test_true_next_salary_date_rolls_to_next_month_when_already_passed():
    # Sep 10, salary_day 1 -> already happened this month -> Oct 1
    assert bs._true_next_salary_date(NOW, 1).date() == datetime(2026, 10, 1).date()


def test_true_next_salary_date_clamps_to_days_in_month():
    # Feb has 28 days in 2026 (not a leap year); salary_day 31 clamps to Feb 28
    from_dt = datetime(2026, 2, 5, tzinfo=timezone.utc)
    assert bs._true_next_salary_date(from_dt, 31).date() == datetime(2026, 2, 28).date()


# ---------------------------------------------------------------------------
# _matches_correct_strategy
# ---------------------------------------------------------------------------

def test_not_recoverable_never_matches():
    case = make_case(latent={**make_case()["latent"], "recoverable": False})
    decision = make_decision(intervention="retry_after_date", scheduled_for=NOW.isoformat())
    assert bs._matches_correct_strategy(case, decision, case["latent"], NOW) is False


def test_escalate_and_close_lost_never_match():
    case = make_case()
    for intervention in ("escalate", "close_lost"):
        decision = make_decision(intervention=intervention)
        assert bs._matches_correct_strategy(case, decision, case["latent"], NOW) is False


def test_insufficient_funds_fails_when_guess_undershoots_real_salary_day():
    # salary_day=1: true next salary is Oct 1. Heuristic guesses Sep 30 (month-end).
    latent = {**make_case()["latent"], "salary_day": 1}
    case = make_case(latent=latent, created_at=NOW.isoformat())
    decision = make_decision(intervention="retry_after_date", scheduled_for="2026-09-30T09:00:00+00:00")
    assert bs._matches_correct_strategy(case, decision, latent, NOW) is False


def test_insufficient_funds_succeeds_when_guess_aligns_with_real_salary_day():
    # salary_day=30: true next salary is Sep 30, matching the month-end guess exactly.
    latent = {**make_case()["latent"], "salary_day": 30}
    case = make_case(latent=latent, created_at=NOW.isoformat())
    decision = make_decision(intervention="retry_after_date", scheduled_for="2026-09-30T09:00:00+00:00")
    assert bs._matches_correct_strategy(case, decision, latent, NOW) is True


def test_bank_downtime_fails_when_scheduled_too_soon():
    latent = {
        "recoverable": True, "correct_strategy": "backoff", "resolves_after_days": 3,
    }
    case = make_case(reason_category="bank_downtime", latent=latent, created_at=NOW.isoformat())
    decision = make_decision(intervention="retry_after_date", scheduled_for=(NOW + timedelta(hours=4)).isoformat())
    assert bs._matches_correct_strategy(case, decision, latent, NOW) is False


def test_bank_downtime_succeeds_once_resolves_after_days_has_passed():
    latent = {
        "recoverable": True, "correct_strategy": "backoff", "resolves_after_days": 1,
    }
    case = make_case(reason_category="bank_downtime", latent=latent, created_at=NOW.isoformat())
    decision = make_decision(intervention="retry_after_date", scheduled_for=(NOW + timedelta(days=1)).isoformat())
    assert bs._matches_correct_strategy(case, decision, latent, NOW) is True


def test_mandate_revoked_needs_no_extra_timing_check():
    latent = {"recoverable": True, "correct_strategy": "request_re_mandate"}
    case = make_case(reason_category="mandate_revoked", latent=latent)
    decision = make_decision(intervention="request_re_mandate")
    assert bs._matches_correct_strategy(case, decision, latent, NOW) is True


# ---------------------------------------------------------------------------
# _maybe_route_reply
# ---------------------------------------------------------------------------

def test_no_reply_route_for_non_outreach_intervention():
    case = make_case()
    latent = {**case["latent"], "responds_to_outreach": True, "reply_intent": "opt_out"}
    decision = make_decision(intervention="retry_after_date")
    assert bs._maybe_route_reply(case, decision, latent, NOW) is None


def test_no_reply_route_when_customer_does_not_respond():
    case = make_case()
    latent = {**case["latent"], "responds_to_outreach": False, "reply_intent": "promise_to_pay"}
    decision = make_decision(intervention="send_link")
    assert bs._maybe_route_reply(case, decision, latent, NOW) is None


def test_opt_out_reply_halts_and_returns_false(fake_repo):
    case = make_case()
    latent = {**case["latent"], "responds_to_outreach": True, "reply_intent": "opt_out"}
    decision = make_decision(intervention="send_link")
    result = bs._maybe_route_reply(case, decision, latent, NOW)
    assert result is False
    assert case["opted_out"] is True
    assert fake_repo.case_updates[-1]["opted_out"] is True


def test_dispute_reply_escalates_and_returns_false(fake_repo):
    case = make_case()
    latent = {**case["latent"], "responds_to_outreach": True, "reply_intent": "dispute"}
    decision = make_decision(intervention="send_link")
    result = bs._maybe_route_reply(case, decision, latent, NOW)
    assert result is False
    assert case["state"] == "ESCALATED"


@pytest.mark.parametrize("intent", ["already_paid", "pay_now"])
def test_already_paid_and_pay_now_recover_immediately(intent):
    case = make_case()
    latent = {**case["latent"], "responds_to_outreach": True, "reply_intent": intent}
    decision = make_decision(intervention="send_link")
    assert bs._maybe_route_reply(case, decision, latent, NOW) is True


@pytest.mark.parametrize("intent", ["already_paid", "pay_now"])
def test_already_paid_and_pay_now_do_not_recover_a_case_that_is_not_latent_recoverable(intent):
    """Regression guard: ceiling_capture (recovered_value / recoverable
    ceiling) can never exceed 100% because recovered cases are a subset of
    the recoverable ones -- a customer reply must not be able to recover a
    case latent says can never actually recover, the same ground truth
    _matches_correct_strategy already enforces for the retry path."""
    case = make_case()
    latent = {**case["latent"], "recoverable": False, "responds_to_outreach": True, "reply_intent": intent}
    decision = make_decision(intervention="send_link")
    assert bs._maybe_route_reply(case, decision, latent, NOW) is False


def test_resolve_due_promises_does_not_recover_a_kept_promise_that_is_not_latent_recoverable(fake_repo):
    """Same ceiling regression as the already_paid/pay_now case above, for
    the promise-kept path: a case latent says can never actually recover
    must not be recovered just because it also (independently) keeps a
    promise in the synthetic data. Exercised through the Scheduler now
    (app.scheduler.jobs), with batch_scanner's own latent-reading is_paid
    closure -- the same wiring run_batch() uses."""
    case = make_case(id="case_np1", amount=499.0)
    case["latent"] = {**case["latent"], "recoverable": False, "keeps_promise": True}
    cases = {"case_np1": case}
    fake_repo.insert_promise({
        "case_id": "case_np1", "promised_amount": 499.0,
        "promised_date": NOW.date().isoformat(), "status": "pending", "source": "text",
    })
    scheduler = scheduler_jobs.Scheduler(cases, promise_is_paid=bs._is_paid(cases))

    scheduler._resolve_due_promises(NOW)

    assert case["state"] != "RECOVERED"
    promise = next(iter(fake_repo.promises.values()))
    assert promise["status"] == "broken"


def test_resolve_due_promises_recovers_a_kept_promise_that_is_latent_recoverable(fake_repo):
    case = make_case(id="case_np2", amount=499.0)
    case["latent"] = {**case["latent"], "recoverable": True, "keeps_promise": True}
    cases = {"case_np2": case}
    fake_repo.insert_promise({
        "case_id": "case_np2", "promised_amount": 499.0,
        "promised_date": NOW.date().isoformat(), "status": "pending", "source": "text",
    })
    scheduler = scheduler_jobs.Scheduler(cases, promise_is_paid=bs._is_paid(cases))

    scheduler._resolve_due_promises(NOW)

    assert case["state"] == "RECOVERED"
    promise = next(iter(fake_repo.promises.values()))
    assert promise["status"] == "kept"


def test_promise_to_pay_creates_a_promise_and_returns_false(fake_repo):
    case = make_case()
    latent = {**case["latent"], "responds_to_outreach": True, "reply_intent": "promise_to_pay", "promise_offset_days": 5}
    decision = make_decision(intervention="send_link")
    result = bs._maybe_route_reply(case, decision, latent, NOW)
    assert result is False
    assert case["state"] == "PROMISE_MADE"
    assert len(fake_repo.promises) == 1
    promise = next(iter(fake_repo.promises.values()))
    assert promise["promised_date"] == (NOW.date() + timedelta(days=5)).isoformat()


def test_promise_to_pay_is_capped_at_max_promise_horizon_days(fake_repo):
    case = make_case()
    horizon = DEFAULT_POLICY["max_promise_horizon_days"]
    latent = {
        **case["latent"], "responds_to_outreach": True, "reply_intent": "promise_to_pay",
        "promise_offset_days": horizon + 30,
    }
    decision = make_decision(intervention="send_link")
    bs._maybe_route_reply(case, decision, latent, NOW)
    promise = next(iter(fake_repo.promises.values()))
    assert promise["promised_date"] == (NOW.date() + timedelta(days=horizon)).isoformat()


def test_unclear_reply_has_no_override():
    case = make_case()
    latent = {**case["latent"], "responds_to_outreach": True, "reply_intent": "unclear"}
    decision = make_decision(intervention="send_link")
    assert bs._maybe_route_reply(case, decision, latent, NOW) is None


# ---------------------------------------------------------------------------
# naive baseline
# ---------------------------------------------------------------------------

def test_naive_not_recoverable_never_recovers_regardless_of_reason():
    unrecoverable = {"recoverable": False}
    for reason in (
        "insufficient_funds", "bank_downtime", "mandate_revoked",
        "expired_card", "checkout_dropoff", "technical_other",
    ):
        assert bs._naive_recovers({"id": "x", "reason_category": reason, "latent": unrecoverable}) is False


def test_naive_technical_other_recovers_whenever_recoverable():
    assert bs._naive_recovers({"id": "x", "reason_category": "technical_other", "latent": {"recoverable": True}}) is True


def test_naive_mandate_revoked_and_expired_card_never_recover_even_if_recoverable():
    """Retrying the exact same broken payment method changes nothing, no
    matter when — these two can never succeed on a blind immediate retry."""
    recoverable = {"recoverable": True}
    assert bs._naive_recovers({"id": "x", "reason_category": "mandate_revoked", "latent": recoverable}) is False
    assert bs._naive_recovers({"id": "x", "reason_category": "expired_card", "latent": recoverable}) is False


def test_naive_checkout_dropoff_never_recovers_even_if_recoverable():
    assert bs._naive_recovers({"id": "x", "reason_category": "checkout_dropoff", "latent": {"recoverable": True}}) is False


def test_naive_bank_downtime_recovers_only_when_outage_already_cleared():
    recovers = {"recoverable": True, "resolves_after_days": 0}
    still_down = {"recoverable": True, "resolves_after_days": 1}
    assert bs._naive_recovers({"id": "x", "reason_category": "bank_downtime", "latent": recovers}) is True
    assert bs._naive_recovers({"id": "x", "reason_category": "bank_downtime", "latent": still_down}) is False


def test_naive_insufficient_funds_is_deterministic_per_case_id():
    """The topup chance is randomised, but seeded by case id — the same case
    must give the same answer every time, run after run."""
    case = {"id": "case_abc", "reason_category": "insufficient_funds", "latent": {"recoverable": True}}
    first = bs._naive_recovers(case)
    for _ in range(5):
        assert bs._naive_recovers(case) == first


def test_naive_insufficient_funds_topup_chance_lands_near_the_documented_rate():
    """Not exact — it's a probability — but across many distinct case ids the
    empirical rate should land close to _NAIVE_TOPUP_CHANCE, not near 0% or
    100%, proving the RNG is actually wired to the constant."""
    hits = sum(
        bs._naive_recovers({"id": f"case_{i}", "reason_category": "insufficient_funds", "latent": {"recoverable": True}})
        for i in range(2000)
    )
    rate = hits / 2000
    assert abs(rate - bs._NAIVE_TOPUP_CHANCE) < 0.05


def test_naive_baseline_aggregates_across_the_dataset(monkeypatch):
    dataset = [
        {"id": "a", "reason_category": "technical_other", "amount": 100.0, "latent": {"recoverable": True}},
        {"id": "b", "reason_category": "technical_other", "amount": 200.0, "latent": {"recoverable": False}},
        {"id": "c", "reason_category": "bank_downtime", "amount": 300.0, "latent": {"recoverable": True, "resolves_after_days": 0}},
        {"id": "d", "reason_category": "mandate_revoked", "amount": 400.0, "latent": {"recoverable": True}},
    ]
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: dataset)
    result = bs.naive_baseline("dev")
    assert result == {
        "recovered_count": 2,
        "recovered_value": 400.0,
        "total_count": 4,
        "at_risk_value": 1000.0,
    }


def test_naive_baseline_lands_in_the_realistic_range_on_the_real_dev_dataset():
    """The headline credibility check: no more +980%-lift strawman."""
    result = bs.naive_baseline("dev")
    rate = result["recovered_count"] / result["total_count"]
    assert 0.12 <= rate <= 0.18


def test_run_batch_never_lets_ceiling_capture_exceed_100_percent_on_the_real_dev_dataset(monkeypatch):
    """Regression guard: recovered cases must be a subset of the recoverable
    ones (ceiling_capture = recovered_value / recoverable_value can never
    exceed 1.0). metrics.compute() itself raises loudly if it ever does
    (see app/metrics/compute.py's _assert_ceiling_not_exceeded) -- a passing
    run_batch() call here already proves that didn't happen on the real
    dataset, and the explicit bound below is the actual assertion this test
    exists to make."""
    monkeypatch.setattr(repository_module, "bulk_insert", lambda table, rows, chunk_size=500: len(rows))

    result = bs.run_batch("dev", now=NOW, persist="memory")

    assert result["ceiling_capture"] is not None
    assert result["ceiling_capture"] <= 1.0


# ---------------------------------------------------------------------------
# regression: insufficient_funds recovery must not depend on what day of the
# month the batch happens to run on
# ---------------------------------------------------------------------------

_DAYS_OF_MONTH_TO_CHECK = [1, 10, 20, 28]


def _insufficient_funds_batch(now: datetime, n: int = 30, seed: int = 7) -> list[dict]:
    """A representative slice of freshly-detected insufficient_funds cases,
    with created_at generated the same way app.detection.synthetic_data does
    (within 72h of `now`) but anchored to a caller-chosen `now` instead of the
    real wall clock -- so the day-of-month regression test below can pin it
    to the 1st/10th/20th/28th without the dataset's own generation time
    leaking in as a confound."""
    rng = random.Random(seed)
    return [
        make_case(
            id=f"case_domreg_{i}",
            created_at=(now - timedelta(hours=rng.randint(0, 72))).isoformat(),
            latent=sd._make_latent("insufficient_funds", rng),
        )
        for i in range(n)
    ]


def test_insufficient_funds_recovery_does_not_silently_depend_on_day_of_month(monkeypatch):
    """Regression guard: _next_salary_day()'s heuristic (1st/month-end
    cluster) can land anywhere from 1 to ~29 days out depending purely on
    what day of the month `now` is. Before the grace-period fallback in
    app/decision/engine.py (_decide_insufficient_funds_link_fallback), a
    schedule that landed past grace_period_days was still proposed as a
    mandate-debit retry, guaranteed to be G5-blocked into CLOSED_LOST once
    the clock reached it -- so insufficient_funds recovery could silently
    collapse to 0% just because the batch happened to run on, say, the 1st
    instead of the 28th. Runs the same representative case mix with `now`
    pinned to four different days of the month and asserts recovery stays in
    a reasonable, stable band throughout.
    """
    monkeypatch.setattr(repository_module, "bulk_insert", lambda table, rows, chunk_size=500: len(rows))

    rates: dict[int, float] = {}
    for day in _DAYS_OF_MONTH_TO_CHECK:
        now = datetime(2026, 9, day, 9, 0, tzinfo=timezone.utc)
        cases = _insufficient_funds_batch(now)
        monkeypatch.setattr(bs, "_load_dataset", lambda set_name, cases=cases: cases)
        result = bs.run_batch("dev", horizon_days=30, live=False, now=now)
        rates[day] = result["recovery_by_reason"].get("insufficient_funds", {}).get("rate", 0.0)

    for day, rate in rates.items():
        assert rate >= 0.35, f"day {day} of month: insufficient_funds recovery collapsed to {rate:.1%} ({rates})"
    spread = max(rates.values()) - min(rates.values())
    assert spread <= 0.40, f"insufficient_funds recovery swings wildly by day of month: {rates}"


# ---------------------------------------------------------------------------
# run_batch — end to end wiring, small crafted datasets, fake repo
# ---------------------------------------------------------------------------

def test_run_batch_diagnoses_and_recovers_a_same_day_case(monkeypatch, fake_repo):
    """checkout_dropoff, touch 1, customer says pay_now — should resolve on
    day 0, no waiting needed."""
    case = make_case(
        id="case_checkout1", source="checkout", reason_category="checkout_dropoff",
        reason_raw="Order created but not paid within window",
        latent={
            "recoverable": True, "correct_strategy": "nudge_then_offer",
            "responds_to_outreach": True, "reply_intent": "pay_now",
            "reply_text_hinglish": "ok kar deta hun abhi",
            "promise_offset_days": None, "keeps_promise": None,
            "salary_day": None, "resolves_after_days": None,
        },
    )
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: [case])

    result = bs.run_batch("dev", horizon_days=3, live=False, now=NOW, persist="supabase")

    assert result["recovered_count"] == 1
    assert result["total_cases"] == 1
    stored = fake_repo.cases["case_checkout1"]
    assert stored["state"] == "RECOVERED"
    assert stored["recovered_amount"] == 499.0


def test_run_batch_mandate_debit_takes_at_least_two_days(monkeypatch, fake_repo):
    """insufficient_funds: day 0 must only send the pre-debit notice, never
    attempt the charge the same day it was diagnosed. A 1-day horizon means
    the end-of-horizon sweep (fix: no case may finish non-terminal) closes
    the still-SCHEDULED case as CLOSED_LOST — that's the correct, honest
    outcome for "we didn't have time to see this through," not a bug."""
    # created_at must land inside the grace period relative to the guessed
    # salary day, or the decision engine now falls back to send_link instead
    # of a mandate debit (see app/decision/engine.py's grace-period check) —
    # that's a different code path than the one this test exercises.
    case = make_case(id="case_if1", created_at=NEAR_MONTH_END.isoformat())
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: [case])

    bs.run_batch("dev", horizon_days=1, live=False, now=NEAR_MONTH_END, persist="supabase")

    assert fake_repo.attempts_by_key == {}  # no charge attempted on day 0
    notices = [o for o in fake_repo.outreach if o["channel"] == "pre_debit_notice"]
    assert len(notices) == 1
    assert fake_repo.cases["case_if1"]["state"] == "CLOSED_LOST"  # swept at horizon end


NEAR_MONTH_END = datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc)  # 4 days to month-end, well inside the 14-day grace period


def test_run_batch_mandate_debit_charges_once_notice_and_schedule_are_both_ready(monkeypatch, fake_repo):
    """Give the case a salary_day that aligns with the month-end guess, and
    run long enough for both the notice to age and the guessed date to
    arrive — the charge should fire and recover the case."""
    case = make_case(
        id="case_if2", created_at=NEAR_MONTH_END.isoformat(),
        latent={**make_case()["latent"], "salary_day": 30},
    )
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: [case])

    result = bs.run_batch("dev", horizon_days=10, live=False, now=NEAR_MONTH_END, persist="supabase")

    assert len(fake_repo.attempts_by_key) == 1
    assert fake_repo.cases["case_if2"]["state"] == "RECOVERED"
    assert result["recovered_count"] == 1


def test_run_batch_pre_debit_notice_decision_is_not_recomputed_while_waiting(monkeypatch, fake_repo):
    """Regression guard: if decide() were re-run every waiting day, a fresh
    salary-day guess relative to the shifting 'now' could keep pushing the
    target out of reach. The scheduled_for on the eventual attempt must match
    day 0's original guess."""
    case = make_case(
        id="case_if3", created_at=NEAR_MONTH_END.isoformat(),
        latent={**make_case()["latent"], "salary_day": 30},
    )
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: [case])

    bs.run_batch("dev", horizon_days=10, live=False, now=NEAR_MONTH_END, persist="supabase")

    attempt = next(iter(fake_repo.attempts_by_key.values()))
    assert attempt["scheduled_for"].startswith("2026-09-30")


def test_run_batch_writes_a_snapshot(monkeypatch, fake_repo, no_snapshot_file):
    case = make_case(id="case_x", reason_category="mandate_revoked",
                      reason_raw="Mandate has been revoked by the customer",
                      latent={"recoverable": False, "correct_strategy": "request_re_mandate",
                              "responds_to_outreach": False, "reply_intent": "none",
                              "reply_text_hinglish": None, "promise_offset_days": None,
                              "keeps_promise": None, "salary_day": None, "resolves_after_days": None})
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: [case])

    result = bs.run_batch("dev", horizon_days=2, live=False, now=NOW, persist="supabase")

    assert no_snapshot_file["path"] == "data/results_dev.json"
    assert no_snapshot_file["data"] is result
    assert result["total_cases"] == 1


def test_run_batch_returns_the_expected_metrics_shape(monkeypatch, fake_repo):
    case = make_case(id="case_shape")
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: [case])

    result = bs.run_batch("dev", horizon_days=1, live=False, now=NOW, persist="supabase")

    for key in (
        "total_cases", "at_risk_value", "recovered_count", "recovered_value",
        "recovery_rate_count", "recovery_rate_value", "recovery_lift", "ceiling_capture",
        "recovery_by_reason", "gate_block_counts", "exception_list",
    ):
        assert key in result


# ---------------------------------------------------------------------------
# --limit / diagnostic slicing
# ---------------------------------------------------------------------------

def test_limit_processes_only_the_first_n_cases(monkeypatch, fake_repo):
    dataset = [make_case(id=f"case_{i}", reason_category="mandate_revoked",
                          reason_raw="Mandate has been revoked by the customer",
                          latent={"recoverable": False, "correct_strategy": "request_re_mandate",
                                  "responds_to_outreach": False, "reply_intent": "none",
                                  "reply_text_hinglish": None, "promise_offset_days": None,
                                  "keeps_promise": None, "salary_day": None, "resolves_after_days": None})
               for i in range(5)]
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: dataset)

    result = bs.run_batch("dev", horizon_days=1, live=False, now=NOW, limit=2, persist="supabase")

    assert result["total_cases"] == 2
    assert len(fake_repo.cases) == 2
    assert set(fake_repo.cases) == {"case_0", "case_1"}


def test_limit_none_processes_the_whole_dataset(monkeypatch, fake_repo):
    dataset = [make_case(id=f"case_{i}") for i in range(3)]
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: dataset)

    result = bs.run_batch("dev", horizon_days=1, live=False, now=NOW, persist="supabase")

    assert result["total_cases"] == 3


# ---------------------------------------------------------------------------
# progress output
# ---------------------------------------------------------------------------

def test_progress_output_prints_one_line_per_simulated_day(monkeypatch, fake_repo, capsys):
    case = make_case(id="case_progress", reason_category="mandate_revoked",
                      reason_raw="Mandate has been revoked by the customer",
                      latent={"recoverable": False, "correct_strategy": "request_re_mandate",
                              "responds_to_outreach": False, "reply_intent": "none",
                              "reply_text_hinglish": None, "promise_offset_days": None,
                              "keeps_promise": None, "salary_day": None, "resolves_after_days": None})
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: [case])

    bs.run_batch("dev", horizon_days=4, live=False, now=NOW, persist="supabase")

    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip().startswith("day ")]
    assert len(lines) == 4
    assert "day   1/4" in lines[0]
    assert "day   4/4" in lines[3]
    for line in lines:
        assert "active=" in line
        assert "recovered=" in line
        assert "elapsed=" in line


def test_progress_output_tracks_active_and_recovered_counts(monkeypatch, fake_repo, capsys):
    """checkout_dropoff + pay_now recovers same-day, so day 1 already shows
    it: active drops to 0, recovered climbs to 1, and stays there."""
    case = make_case(
        id="case_progress2", source="checkout", reason_category="checkout_dropoff",
        reason_raw="Order created but not paid within window",
        latent={
            "recoverable": True, "correct_strategy": "nudge_then_offer",
            "responds_to_outreach": True, "reply_intent": "pay_now",
            "reply_text_hinglish": "ok kar deta hun abhi",
            "promise_offset_days": None, "keeps_promise": None,
            "salary_day": None, "resolves_after_days": None,
        },
    )
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: [case])

    bs.run_batch("dev", horizon_days=2, live=False, now=NOW, persist="supabase")

    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip().startswith("day ")]
    assert "active=   0" in lines[0]
    assert "recovered=   1" in lines[0]
    assert "recovered=   1" in lines[1]


# ---------------------------------------------------------------------------
# persist="memory" (the default) — swaps in app/db/memory_repository.py for
# the whole run, then bulk-flushes to Supabase via repository.bulk_insert.
# None of these use the fake_repo fixture: persist="memory" ignores whatever
# repository.* currently is and swaps in its own MemoryRepository, which is
# exactly the point.
# ---------------------------------------------------------------------------

def mandate_revoked_case(**overrides) -> dict:
    case = make_case(
        reason_category="mandate_revoked",
        reason_raw="Mandate has been revoked by the customer",
        latent={
            "recoverable": False, "correct_strategy": "request_re_mandate",
            "responds_to_outreach": False, "reply_intent": "none",
            "reply_text_hinglish": None, "promise_offset_days": None,
            "keeps_promise": None, "salary_day": None, "resolves_after_days": None,
        },
    )
    case.update(overrides)
    return case


# ---------------------------------------------------------------------------
# _strip_generated_ids — DB-generated ids must never be written by the flush
# ---------------------------------------------------------------------------

def test_strip_generated_ids_removes_id_for_db_generated_tables():
    rows = [{"id": "abc-123", "case_id": "c1"}, {"id": "def-456", "case_id": "c2"}]
    for table in ("payment_attempts", "outreach", "promises", "audit_log"):
        stripped = bs._strip_generated_ids(table, rows)
        assert all("id" not in row for row in stripped)
        assert all(row["case_id"] for row in stripped)  # other fields survive


def test_strip_generated_ids_keeps_id_for_cases():
    rows = [{"id": "case_1", "amount": 499.0}]
    stripped = bs._strip_generated_ids("cases", rows)
    assert stripped[0]["id"] == "case_1"


def test_strip_generated_ids_does_not_mutate_the_input():
    rows = [{"id": "abc-123"}]
    bs._strip_generated_ids("outreach", rows)
    assert rows[0]["id"] == "abc-123"  # original untouched


def test_flush_strips_ids_for_every_table_except_cases(monkeypatch):
    """End-to-end: audit_log.id is bigserial (DB-generated) — the classic
    case this whole fix targets — and outreach.id is a uuid default. Neither
    should reach bulk_insert; cases.id (the text PK from the dataset) must.

    Restores REAL audit logging for this one test (overriding the autouse
    no_audit_writes fixture) so there's an actual audit_log row to check.
    """
    monkeypatch.setattr(audit_log_module, "record", _REAL_AUDIT_RECORD)
    monkeypatch.setattr(audit_log_module, "gate", _REAL_AUDIT_GATE)
    monkeypatch.setattr(audit_log_module, "error", _REAL_AUDIT_ERROR)
    monkeypatch.setattr(audit_log_module, "money_action", _REAL_AUDIT_MONEY_ACTION)

    flushed = []
    monkeypatch.setattr(
        repository_module, "bulk_insert",
        lambda table, rows, chunk_size=500: flushed.append((table, list(rows))) or len(rows),
    )
    case = mandate_revoked_case(id="case_flush_ids")
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: [case])

    bs.run_batch("dev", horizon_days=1, live=False, now=NOW)

    by_table = dict(flushed)
    assert all("id" in row for row in by_table["cases"])
    assert by_table["audit_log"], "expected at least one audit row to exercise the strip"
    for table in ("payment_attempts", "outreach", "promises", "audit_log"):
        assert all("id" not in row for row in by_table[table])


def test_persist_memory_is_the_default(monkeypatch):
    """No fake_repo fixture here on purpose: persist='memory' must work with
    whatever repository.* happens to be — it swaps its own backend in."""
    flushed = []
    monkeypatch.setattr(
        repository_module, "bulk_insert",
        lambda table, rows, chunk_size=500: flushed.append((table, list(rows))) or len(rows),
    )
    case = mandate_revoked_case(id="case_mem1")
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: [case])

    result = bs.run_batch("dev", horizon_days=2, live=False, now=NOW)  # persist omitted -> "memory"

    assert result["total_cases"] == 1
    tables = {t for t, _ in flushed}
    assert tables == {"cases", "payment_attempts", "outreach", "promises", "audit_log"}
    cases_flushed = next(rows for t, rows in flushed if t == "cases")
    assert len(cases_flushed) == 1
    assert cases_flushed[0]["id"] == "case_mem1"


def test_persist_memory_explicit_matches_the_default(monkeypatch):
    flushed = []
    monkeypatch.setattr(
        repository_module, "bulk_insert",
        lambda table, rows, chunk_size=500: flushed.append((table, list(rows))) or len(rows),
    )
    case = mandate_revoked_case(id="case_mem2")
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: [case])

    bs.run_batch("dev", horizon_days=2, live=False, now=NOW, persist="memory")

    assert any(t == "cases" for t, _ in flushed)


def test_persist_memory_restores_the_real_repository_functions_afterward(monkeypatch):
    """The swap must not leak: once run_batch() returns, app.db.repository's
    functions must be exactly what they were before, not left pointing at a
    now-discarded MemoryRepository instance."""
    monkeypatch.setattr(repository_module, "bulk_insert", lambda *a, **k: 0)
    originals = {name: getattr(repository_module, name) for name in bs._REPOSITORY_FUNCTIONS}

    case = mandate_revoked_case(id="case_mem3")
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: [case])
    bs.run_batch("dev", horizon_days=1, live=False, now=NOW)

    for name, fn in originals.items():
        assert getattr(repository_module, name) is fn


def test_persist_supabase_never_flushes(fake_repo, monkeypatch):
    """persist='supabase' means every call already hit 'the database' as it
    happened — there's nothing to bulk-flush afterward."""
    flushed = []
    monkeypatch.setattr(repository_module, "bulk_insert", lambda *a, **k: flushed.append(a) or 0)
    case = mandate_revoked_case(id="case_sb1")
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: [case])

    bs.run_batch("dev", horizon_days=1, live=False, now=NOW, persist="supabase")

    assert flushed == []


def test_persist_invalid_value_raises(monkeypatch):
    case = mandate_revoked_case()
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: [case])
    with pytest.raises(ValueError):
        bs.run_batch("dev", horizon_days=1, live=False, now=NOW, persist="nope")


def test_persist_memory_runs_the_real_dev_dataset_in_seconds(monkeypatch):
    """The actual point of persist='memory': the real 100-case dev set, the
    default 21-day horizon, no fakes standing in for the dataset — must run
    at Python speed, not network speed. A generous ceiling (real hardware
    finishes this in well under a second) still catches an accidental
    reintroduction of a per-call network round trip.

    Deliberately does NOT pin `now`: the dataset's created_at values are
    relative to whenever it was generated, and a fixed historical `now`
    could push every case's age past grace_period_days before it's even
    processed — this test is about speed, not a specific outcome mix."""
    import time as time_module

    monkeypatch.setattr(repository_module, "bulk_insert", lambda *a, **k: 0)

    started = time_module.perf_counter()
    result = bs.run_batch("dev")  # real dataset, default horizon_days=21, persist="memory"
    elapsed = time_module.perf_counter() - started

    assert result["total_cases"] == 100
    assert elapsed < 10.0


# ---------------------------------------------------------------------------
# the gate is the sole authority on bounds — G2 (opt-out) mid-sequence
# ---------------------------------------------------------------------------

def opted_out_reply_case(**overrides) -> dict:
    case = make_case(
        reason_category="expired_card",
        reason_raw="Card has expired",
        latent={
            "recoverable": True, "correct_strategy": "request_card_update",
            "responds_to_outreach": True, "reply_intent": "opt_out",
            "reply_text_hinglish": "mujhe nahi chahiye ab, band kar do",
            "promise_offset_days": None, "keeps_promise": None,
            "salary_day": None, "resolves_after_days": None,
        },
    )
    case.update(overrides)
    return case


def test_opt_out_mid_sequence_gets_exactly_one_confirming_gate_pass(fake_repo, monkeypatch):
    """Day 1 sends the first outreach and the reply sets opted_out. Day 2 is
    the very next action attempt: it must still go through the gate (so G2
    fires and lands in the audit trail via audit.log.gate) rather than being
    silently swallowed by the loop filter. From day 3 on, the case is
    skipped without touching the gate again."""
    case = opted_out_reply_case(id="case_optout")
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: [case])

    result = bs.run_batch("dev", horizon_days=5, live=False, now=NOW, persist="supabase")

    assert result["gate_block_counts"].get("G2") == 1
    # exactly one outreach ever sent — the day-1 request_card_update. The
    # day-2 attempt (send_link fallback) was blocked by G2 before anything
    # could be persisted, and days 3-5 never reached the gate at all.
    assert len(fake_repo.outreach) == 1
    assert fake_repo.cases["case_optout"]["opted_out"] is True
    assert fake_repo.cases["case_optout"]["state"] != "RECOVERED"


# ---------------------------------------------------------------------------
# the gate is the sole authority — end-to-end gate_block_counts on the real
# dev dataset (the credibility check this whole restructure was about)
# ---------------------------------------------------------------------------

def test_gate_block_counts_show_more_than_just_g10_on_the_real_dataset(monkeypatch):
    """Before this restructure, decision engine self-checks (attempt cap,
    grace period) pre-empted G3/G5 entirely, and G6 was unreachable because
    discounts were pre-clamped — only G10 (active-promise pause) ever fired.
    Now the gate is the sole authority: G3 and/or G5 must show up on a real,
    sufficiently long run — not just "something other than G10" (G2 alone
    would satisfy that trivially without proving the attempt-cap/grace-period
    bounds actually bind at the gate)."""
    monkeypatch.setattr(repository_module, "bulk_insert", lambda *a, **k: 0)

    result = bs.run_batch("dev", horizon_days=60, persist="memory")

    counts = result["gate_block_counts"]
    assert counts.get("G3", 0) + counts.get("G5", 0) > 0, f"neither G3 nor G5 fired: {counts!r}"


def test_gate_block_counts_are_not_lost_when_a_block_translates_to_terminal(monkeypatch):
    """Regression guard for the exact bug this restructure's own testing
    caught: _apply_terminal defaults "gate" to None, so a translated G3/G5
    (or G6-then-escalate) block must have its gate id restored onto the
    result — otherwise it executes correctly but vanishes from
    gate_block_counts, which is precisely the invisibility problem this
    whole task exists to fix."""
    monkeypatch.setattr(repository_module, "bulk_insert", lambda *a, **k: 0)

    result = bs.run_batch("dev", horizon_days=60, persist="memory")

    counts = result["gate_block_counts"]
    escalated_or_closed = counts.get("G3", 0) + counts.get("G5", 0) + counts.get("G6", 0)
    assert escalated_or_closed > 0
    # a sanity cross-check: at least that many cases actually ended up
    # ESCALATED or CLOSED_LOST, so the count isn't just tallying phantom blocks
    terminal_non_recovered = sum(
        1 for c in result["exception_list"] if c["state"] in ("ESCALATED", "CLOSED_LOST")
    )
    assert terminal_non_recovered > 0


# ---------------------------------------------------------------------------
# horizon: default 30 days, and an end-of-horizon sweep so no case finishes
# a batch in a non-terminal state
# ---------------------------------------------------------------------------

def test_default_horizon_days_is_30():
    import inspect
    sig = inspect.signature(bs.run_batch)
    assert sig.parameters["horizon_days"].default == 30


# ---------------------------------------------------------------------------
# --clear: a re-run must not silently skip the flush on a duplicate key from
# a prior run of the same batch_id
# ---------------------------------------------------------------------------

def test_clear_defaults_to_true():
    import inspect
    sig = inspect.signature(bs.run_batch)
    assert sig.parameters["clear"].default is True


def test_clear_true_wipes_a_stale_row_left_by_a_prior_run(monkeypatch, fake_repo):
    fake_repo.cases["stale_case"] = {"id": "stale_case", "batch_id": "dev", "state": "DETECTED"}
    case = mandate_revoked_case(id="case_clear1")
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: [case])

    bs.run_batch("dev", horizon_days=1, live=False, now=NOW, persist="supabase")  # clear omitted -> True

    assert "stale_case" not in fake_repo.cases
    assert "case_clear1" in fake_repo.cases


def test_clear_false_leaves_a_prior_run_batch_id_row_in_place(monkeypatch, fake_repo):
    fake_repo.cases["stale_case"] = {"id": "stale_case", "batch_id": "dev", "state": "DETECTED"}
    case = mandate_revoked_case(id="case_clear2")
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: [case])

    bs.run_batch("dev", horizon_days=1, live=False, now=NOW, persist="supabase", clear=False)

    assert "stale_case" in fake_repo.cases
    assert "case_clear2" in fake_repo.cases


# ---------------------------------------------------------------------------
# run_batch wires the gathered attempts/outreach/promises/audit rows into
# metrics.compute() -- the new Sec 4.2-4.4 keys must actually be populated,
# not just present-but-empty because nothing was ever passed through.
# ---------------------------------------------------------------------------

def test_run_batch_result_includes_the_new_metric_groups(monkeypatch, fake_repo):
    """Restores REAL audit logging (overriding the autouse no_audit_writes
    fixture) so actions_without_audit is checked against real ACTED /
    OUTREACH_SENT rows -- with audit disabled (as most tests in this file
    run), that invariant would correctly report a mismatch, since no audit
    row is ever written at all."""
    monkeypatch.setattr(audit_log_module, "record", _REAL_AUDIT_RECORD)
    monkeypatch.setattr(audit_log_module, "gate", _REAL_AUDIT_GATE)
    monkeypatch.setattr(audit_log_module, "error", _REAL_AUDIT_ERROR)
    monkeypatch.setattr(audit_log_module, "money_action", _REAL_AUDIT_MONEY_ACTION)

    case = make_case(id="case_metrics1")
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: [case])

    result = bs.run_batch("dev", horizon_days=3, live=False, now=NOW, persist="supabase")

    for key in (
        "kept_promise_rate", "false_escalation_rate", "avg_time_to_recovery_days",
        "interventions_per_recovery", "cost_per_recovered_rupee", "contact_efficiency",
        "double_charge_incidents", "post_opt_out_contacts", "actions_without_audit",
        "over_cap_discounts", "worst_three_reasons",
    ):
        assert key in result
    # a fresh dev batch's own repository dedup must keep every safety
    # invariant at 0 -- this is the same real-dataset credibility check as
    # test_no_case_finishes_the_real_dev_batch_in_a_non_terminal_state.
    for key in (
        "double_charge_incidents", "post_opt_out_contacts",
        "actions_without_audit", "over_cap_discounts",
    ):
        assert result[key] == 0


# ---------------------------------------------------------------------------
# _fmt_ratio -- a small-but-real ratio (e.g. cost_per_recovered_rupee, often
# a few thousandths of a rupee) must not silently print as "0.00"
# ---------------------------------------------------------------------------

def test_fmt_ratio_default_precision_matches_prior_behaviour():
    assert bs._fmt_ratio(3.14159) == "3.14"


def test_fmt_ratio_higher_precision_shows_a_small_nonzero_value():
    assert bs._fmt_ratio(0.0017595240291486025, decimals=4) == "0.0018"


def test_fmt_ratio_none_is_reported_as_not_available():
    assert bs._fmt_ratio(None, decimals=4) == "n/a"


def test_print_summary_cost_per_recovered_rupee_is_not_flattened_to_zero(capsys):
    bs._print_summary("dev", {
        "total_cases": 1, "recovered_count": 1, "recovery_rate_count": 1.0,
        "recovered_value": 100.0, "at_risk_value": 100.0, "recovery_rate_value": 1.0,
        "exception_list": [], "gate_block_counts": {},
        "cost_per_recovered_rupee": 0.0017595240291486025,
    })
    out = capsys.readouterr().out
    assert "cost per recovered Rs   : 0.0018\n" in out
    assert "cost per recovered Rs   : 0.00\n" not in out


# ---------------------------------------------------------------------------
# _fmt_pct_n -- a rate must show its sample size, not just the percentage
# ---------------------------------------------------------------------------

def test_fmt_pct_n_shows_percentage_and_fraction():
    assert bs._fmt_pct_n(3 / 11, 3, 11) == "27.3% (3/11)"


def test_fmt_pct_n_none_is_reported_as_not_available_with_zero_over_zero():
    assert bs._fmt_pct_n(None, 0, 0) == "n/a (0/0)"


def test_print_summary_kept_promise_rate_shows_its_denominator(capsys):
    bs._print_summary("dev", {
        "total_cases": 1, "recovered_count": 1, "recovery_rate_count": 1.0,
        "recovered_value": 100.0, "at_risk_value": 100.0, "recovery_rate_value": 1.0,
        "exception_list": [], "gate_block_counts": {},
        "kept_promise_rate": 3 / 11, "kept_promise_kept_count": 3, "kept_promise_resolved_count": 11,
    })
    out = capsys.readouterr().out
    assert "kept-promise rate       : 27.3% (3/11)\n" in out


def test_print_summary_kept_promise_rate_with_no_data_shows_zero_over_zero(capsys):
    bs._print_summary("dev", {
        "total_cases": 1, "recovered_count": 0, "recovery_rate_count": 0.0,
        "recovered_value": 0.0, "at_risk_value": 100.0, "recovery_rate_value": 0.0,
        "exception_list": [], "gate_block_counts": {},
    })
    out = capsys.readouterr().out
    assert "kept-promise rate       : n/a (0/0)\n" in out


def test_safety_invariants_hold_on_the_real_dev_dataset(monkeypatch):
    """The credibility check for the whole metrics extension: run the real
    100-case dev set end to end and prove every governance/safety invariant
    is actually 0 on real pipeline output, not just on hand-crafted data.
    Restores REAL audit logging (see test_run_batch_result_includes_the_new_
    metric_groups above) -- actions_without_audit is meaningless with audit
    writes disabled."""
    monkeypatch.setattr(audit_log_module, "record", _REAL_AUDIT_RECORD)
    monkeypatch.setattr(audit_log_module, "gate", _REAL_AUDIT_GATE)
    monkeypatch.setattr(audit_log_module, "error", _REAL_AUDIT_ERROR)
    monkeypatch.setattr(audit_log_module, "money_action", _REAL_AUDIT_MONEY_ACTION)
    monkeypatch.setattr(repository_module, "bulk_insert", lambda *a, **k: 0)

    result = bs.run_batch("dev", persist="memory")  # default horizon_days=30

    assert result["double_charge_incidents"] == 0
    assert result["post_opt_out_contacts"] == 0
    assert result["actions_without_audit"] == 0
    assert result["over_cap_discounts"] == 0


def test_end_of_horizon_sweep_closes_a_still_active_case(monkeypatch, fake_repo):
    """A single-day horizon leaves a freshly-outreached case well short of
    resolving — the sweep must still close it rather than leave it dangling."""
    case = mandate_revoked_case(id="case_sweep1")
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: [case])

    bs.run_batch("dev", horizon_days=1, live=False, now=NOW, persist="supabase")

    assert fake_repo.cases["case_sweep1"]["state"] == "CLOSED_LOST"


def test_end_of_horizon_sweep_logs_an_audit_row(monkeypatch, fake_repo):
    calls = []
    monkeypatch.setattr(audit_log_module, "record", lambda *a, **k: calls.append((a, k)))
    case = mandate_revoked_case(id="case_sweep2")
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: [case])

    bs.run_batch("dev", horizon_days=1, live=False, now=NOW, persist="supabase")

    sweep_calls = [
        (args, kwargs) for args, kwargs in calls
        if kwargs.get("reasoning", "").startswith("Case was still")
    ]
    assert len(sweep_calls) == 1
    args, kwargs = sweep_calls[0]
    assert args[0] == "case_sweep2"
    assert args[2] == audit_log_module.CLOSED_LOST


def test_sweep_does_not_touch_cases_already_terminal(monkeypatch, fake_repo):
    """A same-day recovery must not get a spurious sweep audit row or have
    its state clobbered."""
    calls = []
    monkeypatch.setattr(audit_log_module, "record", lambda *a, **k: calls.append((a, k)))
    case = make_case(
        id="case_recovered_early", source="checkout", reason_category="checkout_dropoff",
        reason_raw="Order created but not paid within window",
        latent={
            "recoverable": True, "correct_strategy": "nudge_then_offer",
            "responds_to_outreach": True, "reply_intent": "pay_now",
            "reply_text_hinglish": "ok kar deta hun abhi",
            "promise_offset_days": None, "keeps_promise": None,
            "salary_day": None, "resolves_after_days": None,
        },
    )
    monkeypatch.setattr(bs, "_load_dataset", lambda set_name: [case])

    bs.run_batch("dev", horizon_days=1, live=False, now=NOW, persist="supabase")

    assert fake_repo.cases["case_recovered_early"]["state"] == "RECOVERED"
    sweep_calls = [kwargs for _, kwargs in calls if kwargs.get("reasoning", "").startswith("Case was still")]
    assert sweep_calls == []


def test_no_case_finishes_the_real_dev_batch_in_a_non_terminal_state(monkeypatch):
    """The headline check for this fix: with the default 30-day horizon on
    the real 100-case dataset, every case that isn't RECOVERED must be
    CLOSED_LOST or ESCALATED — nothing left DETECTED/OUTREACH_SENT/etc."""
    monkeypatch.setattr(repository_module, "bulk_insert", lambda *a, **k: 0)

    result = bs.run_batch("dev", persist="memory")  # default horizon_days=30

    assert result["recovered_count"] + len(result["exception_list"]) == result["total_cases"]
    stray_states = {
        c["state"] for c in result["exception_list"] if c["state"] not in ("CLOSED_LOST", "ESCALATED")
    }
    assert stray_states == set(), f"cases left non-terminal: {stray_states!r}"
