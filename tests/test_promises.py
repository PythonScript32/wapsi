"""
Tests for app/promises/tracker.py (Feature D, PRD.md §8).

No real DB: a small FakeRepository stands in for app.db.repository, and audit
writes are stubbed (tracker.py's job here is the lifecycle, not audit-log
formatting -- that's covered by tests/test_audit.py-style modules elsewhere).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.config import DEFAULT_POLICY
from app.promises import tracker

NOW = datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)


class FakeRepository:
    def __init__(self):
        self.cases: dict[str, dict] = {}
        self.promises: dict[str, dict] = {}
        self._next_id = 1

    def seed_case(self, case_id: str, **fields) -> None:
        self.cases[case_id] = {"id": case_id, "state": "OUTREACH_SENT", "amount": 499.0, **fields}

    # -- promises --
    def insert_promise(self, row: dict) -> dict:
        pid = f"promise_{self._next_id}"
        self._next_id += 1
        stored = {**row, "id": pid}
        self.promises[pid] = stored
        return dict(stored)

    def promises_for_case(self, case_id: str) -> list[dict]:
        return [dict(p) for p in self.promises.values() if p.get("case_id") == case_id]

    def due_promises(self, on_date: str) -> list[dict]:
        return [
            dict(p) for p in self.promises.values()
            if p["status"] == "pending" and p["promised_date"] <= on_date
        ]

    def active_promise(self, case_id: str) -> dict | None:
        for p in self.promises.values():
            if p.get("case_id") == case_id and p.get("status") == "pending":
                return dict(p)
        return None

    def resolve_promise(self, promise_id: str, status: str) -> None:
        p = self.promises.get(promise_id)
        if p is not None:
            p["status"] = status

    # -- cases --
    def get_case(self, case_id: str) -> dict | None:
        c = self.cases.get(case_id)
        return dict(c) if c is not None else None

    def update_case(self, case_id: str, **fields) -> dict:
        case = self.cases.setdefault(case_id, {"id": case_id})
        case.update(fields)
        return dict(case)

    def mark_recovered(self, case_id: str, amount: float) -> dict:
        return self.update_case(case_id, state="RECOVERED", recovered_amount=amount)


@pytest.fixture
def repo(monkeypatch) -> FakeRepository:
    fake = FakeRepository()
    for name in (
        "insert_promise", "promises_for_case", "due_promises", "active_promise",
        "resolve_promise", "get_case", "update_case", "mark_recovered",
    ):
        monkeypatch.setattr(tracker.repository, name, getattr(fake, name))
    return fake


@pytest.fixture(autouse=True)
def no_audit_writes(monkeypatch):
    monkeypatch.setattr(tracker.audit_log, "record", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# record_promise -- basic creation (FR-D1/D2)
# ---------------------------------------------------------------------------

def test_record_promise_creates_a_pending_promise_and_moves_case_to_promise_made(repo):
    repo.seed_case("c1")
    result = tracker.record_promise("c1", 499.0, date(2026, 9, 20), source="text", policy=DEFAULT_POLICY, now=NOW)

    assert result["created"] is True
    assert result["escalated"] is False
    assert result["promise"]["status"] == "pending"
    assert result["promise"]["promised_date"] == "2026-09-20"
    assert result["promise"]["promised_amount"] == 499.0
    assert repo.cases["c1"]["state"] == "PROMISE_MADE"


def test_reminder_at_is_nine_am_on_the_promised_date():
    assert tracker._reminder_at(date(2026, 9, 20)) == datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)


def test_record_promise_result_carries_the_reminder_time(repo):
    repo.seed_case("c1")
    result = tracker.record_promise("c1", 499.0, date(2026, 9, 20), source="text", policy=DEFAULT_POLICY, now=NOW)
    assert result["reminder_at"] == "2026-09-20T09:00:00+00:00"


# ---------------------------------------------------------------------------
# source tagging -- "voice" is the differentiator PRD wants visible in metrics
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source", ["voice", "text", "inferred"])
def test_record_promise_tags_every_valid_source(repo, source):
    repo.seed_case("c1")
    result = tracker.record_promise("c1", 499.0, date(2026, 9, 20), source=source, policy=DEFAULT_POLICY, now=NOW)
    assert result["promise"]["source"] == source


def test_record_promise_rejects_an_unrecognised_source(repo):
    repo.seed_case("c1")
    with pytest.raises(ValueError):
        tracker.record_promise("c1", 499.0, date(2026, 9, 20), source="carrier_pigeon", policy=DEFAULT_POLICY, now=NOW)


# ---------------------------------------------------------------------------
# capped horizon (FR-D5) -- capped and flagged, never silently accepted
# ---------------------------------------------------------------------------

def test_record_promise_caps_at_the_policy_horizon_and_flags_it(repo):
    repo.seed_case("c1")
    horizon = DEFAULT_POLICY["max_promise_horizon_days"]
    far_future = NOW.date() + timedelta(days=horizon + 30)

    result = tracker.record_promise("c1", 499.0, far_future, source="voice", policy=DEFAULT_POLICY, now=NOW)

    assert result["capped"] is True
    assert result["promise"]["promised_date"] == (NOW.date() + timedelta(days=horizon)).isoformat()
    assert "capped" in result["reasoning"].lower()


def test_record_promise_does_not_flag_a_date_within_the_horizon(repo):
    repo.seed_case("c1")
    near = NOW.date() + timedelta(days=5)

    result = tracker.record_promise("c1", 499.0, near, source="voice", policy=DEFAULT_POLICY, now=NOW)

    assert result["capped"] is False
    assert result["promise"]["promised_date"] == near.isoformat()


# ---------------------------------------------------------------------------
# resolve_due_promises -- kept (FR-D4)
# ---------------------------------------------------------------------------

def test_resolve_due_promises_marks_kept_and_recovers_the_case(repo):
    repo.seed_case("c1", amount=499.0)
    tracker.record_promise("c1", 499.0, NOW.date(), source="text", policy=DEFAULT_POLICY, now=NOW)

    results = tracker.resolve_due_promises(
        NOW.date().isoformat(), DEFAULT_POLICY, is_paid=lambda p: True, now=NOW,
    )

    assert len(results) == 1
    assert results[0]["status"] == "kept"
    assert results[0]["case_state"] == "RECOVERED"
    assert repo.cases["c1"]["state"] == "RECOVERED"
    promise = next(iter(repo.promises.values()))
    assert promise["status"] == "kept"


def test_resolve_due_promises_ignores_promises_not_yet_due(repo):
    repo.seed_case("c1", amount=499.0)
    tracker.record_promise("c1", 499.0, NOW.date() + timedelta(days=5), source="text", policy=DEFAULT_POLICY, now=NOW)

    results = tracker.resolve_due_promises(
        NOW.date().isoformat(), DEFAULT_POLICY, is_paid=lambda p: True, now=NOW,
    )

    assert results == []


def test_resolve_due_promises_default_is_paid_checks_case_state(repo):
    """No is_paid callback supplied: falls back to 'is the case already
    RECOVERED', a reasonable default when the caller has no richer signal."""
    repo.seed_case("c1", amount=499.0)
    tracker.record_promise("c1", 499.0, NOW.date(), source="text", policy=DEFAULT_POLICY, now=NOW)
    repo.update_case("c1", state="RECOVERED")  # e.g. paid through some other path

    results = tracker.resolve_due_promises(NOW.date().isoformat(), DEFAULT_POLICY, now=NOW)

    assert results[0]["status"] == "kept"


def test_resolve_due_promises_default_is_paid_treats_unrecovered_as_broken(repo):
    repo.seed_case("c1", amount=499.0)
    tracker.record_promise("c1", 499.0, NOW.date(), source="text", policy=DEFAULT_POLICY, now=NOW)

    results = tracker.resolve_due_promises(NOW.date().isoformat(), DEFAULT_POLICY, now=NOW)

    assert results[0]["status"] == "broken"


# ---------------------------------------------------------------------------
# resolve_due_promises -- broken (FR-D4)
# ---------------------------------------------------------------------------

def test_resolve_due_promises_marks_broken_and_escalates(repo):
    repo.seed_case("c1", amount=499.0)
    tracker.record_promise("c1", 499.0, NOW.date(), source="text", policy=DEFAULT_POLICY, now=NOW)

    results = tracker.resolve_due_promises(
        NOW.date().isoformat(), DEFAULT_POLICY, is_paid=lambda p: False, now=NOW,
    )

    assert len(results) == 1
    assert results[0]["status"] == "broken"
    assert results[0]["case_state"] == "ESCALATED"
    assert repo.cases["c1"]["state"] == "ESCALATED"
    promise = next(iter(repo.promises.values()))
    assert promise["status"] == "broken"


# ---------------------------------------------------------------------------
# early payment (PRD §13 item 24) -- mark_kept_early
# ---------------------------------------------------------------------------

def test_mark_kept_early_marks_kept_and_recovers_before_the_due_date(repo):
    repo.seed_case("c1", amount=499.0)
    tracker.record_promise("c1", 499.0, NOW.date() + timedelta(days=5), source="text", policy=DEFAULT_POLICY, now=NOW)

    result = tracker.mark_kept_early("c1", now=NOW)

    assert result["status"] == "kept"
    assert result["case_state"] == "RECOVERED"
    assert repo.cases["c1"]["state"] == "RECOVERED"
    promise = next(iter(repo.promises.values()))
    assert promise["status"] == "kept"


def test_mark_kept_early_cancels_the_promise_so_it_is_never_later_swept_up_as_broken(repo):
    repo.seed_case("c1", amount=499.0)
    tracker.record_promise("c1", 499.0, NOW.date() + timedelta(days=5), source="text", policy=DEFAULT_POLICY, now=NOW)
    tracker.mark_kept_early("c1", now=NOW)

    due_date = (NOW.date() + timedelta(days=5)).isoformat()
    results = tracker.resolve_due_promises(due_date, DEFAULT_POLICY, is_paid=lambda p: False, now=NOW)

    # Already resolved (status != 'pending'), so due_promises() no longer
    # returns it -- the whole point of "cancel the scheduled retry".
    assert results == []
    assert repo.cases["c1"]["state"] == "RECOVERED"  # not wrongly re-broken


def test_mark_kept_early_returns_none_with_no_active_promise(repo):
    repo.seed_case("c1")
    assert tracker.mark_kept_early("c1", now=NOW) is None


# ---------------------------------------------------------------------------
# second promise after a broken one -- allowed once, then escalate
# (PRD §13 item 23)
# ---------------------------------------------------------------------------

def test_record_promise_allows_a_second_promise_after_one_broken(repo):
    repo.seed_case("c1", amount=499.0)
    tracker.record_promise("c1", 499.0, NOW.date(), source="text", policy=DEFAULT_POLICY, now=NOW)
    tracker.resolve_due_promises(NOW.date().isoformat(), DEFAULT_POLICY, is_paid=lambda p: False, now=NOW)
    assert repo.cases["c1"]["state"] == "ESCALATED"

    # Customer reaches back out and promises again -- their one allowed
    # second chance.
    result = tracker.record_promise(
        "c1", 499.0, NOW.date() + timedelta(days=3), source="voice", policy=DEFAULT_POLICY, now=NOW,
    )

    assert result["created"] is True
    assert result["escalated"] is False
    assert repo.cases["c1"]["state"] == "PROMISE_MADE"
    assert len(repo.promises_for_case("c1")) == 2


def test_record_promise_refuses_a_third_promise_and_escalates_instead(repo):
    repo.seed_case("c1", amount=499.0)

    # First promise, broken.
    tracker.record_promise("c1", 499.0, NOW.date(), source="text", policy=DEFAULT_POLICY, now=NOW)
    tracker.resolve_due_promises(NOW.date().isoformat(), DEFAULT_POLICY, is_paid=lambda p: False, now=NOW)

    # Second (allowed) chance, also broken.
    day2 = NOW + timedelta(days=3)
    tracker.record_promise("c1", 499.0, day2.date(), source="voice", policy=DEFAULT_POLICY, now=day2)
    tracker.resolve_due_promises(day2.date().isoformat(), DEFAULT_POLICY, is_paid=lambda p: False, now=day2)
    assert repo.cases["c1"]["state"] == "ESCALATED"

    # A third attempt gets no more chances -- no new promise, straight to
    # (already) escalated.
    day3 = NOW + timedelta(days=6)
    result = tracker.record_promise(
        "c1", 499.0, day3.date() + timedelta(days=2), source="voice", policy=DEFAULT_POLICY, now=day3,
    )

    assert result["created"] is False
    assert result["escalated"] is True
    assert result["promise"] is None
    assert repo.cases["c1"]["state"] == "ESCALATED"
    assert len(repo.promises_for_case("c1")) == 2  # the refused attempt created nothing
