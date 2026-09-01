"""
Tests for app/scheduler/jobs.py -- SimulatedClock, and the Scheduler whose
tick() is the ONE shared implementation both batch (SimulatedClock) and live
(APScheduler) drive. No real Supabase, no real Razorpay, no network: a fresh
app.db.memory_repository.MemoryRepository backs every test (same swap
app/detection/batch_scanner.py's own _repository_backend performs), and
live=False keeps actions.execute() in its simulated (no real API call) path.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.db.repository as repository_module
from app.db.memory_repository import MemoryRepository
from app.scheduler import jobs

NOW = datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)  # mid-month, safely inside every grace window

# Mirrors app/detection/batch_scanner.py's _REPOSITORY_FUNCTIONS -- every
# function app.db.repository exposes, swapped to a fresh MemoryRepository's
# bound methods so the whole pipeline (audit log, actions, classifier-free
# here, the scheduler itself) runs against plain dicts and lists.
_REPOSITORY_FUNCTIONS = (
    "clear_batch", "insert_case", "upsert_case", "get_case", "list_cases", "update_case",
    "mark_recovered", "increment_attempts", "insert_attempt", "get_attempt_by_key",
    "attempts_for_case", "update_attempt", "insert_outreach", "last_outreach_at",
    "outreach_for_case", "record_reply", "insert_promise", "active_promise", "due_promises",
    "resolve_promise", "all_promises", "promises_for_case", "append_audit", "audit_for_case",
    "audit_by_event", "gate_context",
)


@pytest.fixture
def memory_repo(monkeypatch) -> MemoryRepository:
    backend = MemoryRepository()
    for name in _REPOSITORY_FUNCTIONS:
        monkeypatch.setattr(repository_module, name, getattr(backend, name))
    return backend


def make_case(**overrides) -> dict:
    case = {
        "id": "case_test000001",
        "batch_id": "demo",
        "source": "checkout",
        "customer_ref": "Test Customer",
        "customer_phone": "98765**43",
        "amount": 499.0,
        "currency": "INR",
        "reason_raw": "Order created but not paid within window",
        "reason_category": "checkout_dropoff",
        "state": "OUTREACH_SENT",
        "attempts_made": 1,  # next touch will be the SECOND: send_link_with_offer
        "opted_out": False,
        "recovered_amount": 0.0,
        "recovered_at": None,
        "created_at": NOW.isoformat(),
    }
    case.update(overrides)
    return case


# ---------------------------------------------------------------------------
# SimulatedClock
# ---------------------------------------------------------------------------

def test_simulated_clock_advances_by_one_day_by_default():
    clock = jobs.SimulatedClock(NOW)
    clock.advance()
    assert clock.now == NOW + timedelta(days=1)


def test_simulated_clock_advances_by_n_days():
    clock = jobs.SimulatedClock(NOW)
    clock.advance(5)
    assert clock.now == NOW + timedelta(days=5)


def test_simulated_clock_advance_is_cumulative():
    clock = jobs.SimulatedClock(NOW)
    clock.advance(2)
    clock.advance(3)
    assert clock.now == NOW + timedelta(days=5)


# ---------------------------------------------------------------------------
# Scheduler.tick() -- per-case processing
# ---------------------------------------------------------------------------

def test_tick_processes_a_due_case_and_advances_its_state(memory_repo):
    case = make_case()
    memory_repo.insert_case(case)
    scheduler = jobs.Scheduler({case["id"]: case})

    summary = scheduler.tick(NOW)

    assert summary["cases_processed"] == 1
    assert case["state"] == "OUTREACH_SENT"  # send_link_with_offer's next state
    assert case["attempts_made"] == 2


def test_tick_skips_a_case_not_yet_due(memory_repo):
    case = make_case()
    memory_repo.insert_case(case)
    scheduler = jobs.Scheduler({case["id"]: case})
    scheduler.next_action_at[case["id"]] = NOW + timedelta(days=5)

    summary = scheduler.tick(NOW)

    assert summary["cases_processed"] == 0
    assert case["attempts_made"] == 1  # untouched


@pytest.mark.parametrize("state", ["RECOVERED", "CLOSED_LOST", "ESCALATED"])
def test_tick_skips_terminal_cases(memory_repo, state):
    case = make_case(state=state)
    memory_repo.insert_case(case)
    scheduler = jobs.Scheduler({case["id"]: case})

    summary = scheduler.tick(NOW)

    assert summary["cases_processed"] == 0
    assert case["state"] == state


def test_tick_tallies_a_gate_block(memory_repo):
    """attempts_made already at the checkout_dropoff cap (2): the very first
    tick should hit G3 and close the case, with the block tallied."""
    case = make_case(attempts_made=2)
    memory_repo.insert_case(case)
    scheduler = jobs.Scheduler({case["id"]: case})

    scheduler.tick(NOW)

    assert case["state"] == "CLOSED_LOST"
    assert scheduler.gate_block_counts["G3"] == 1


def test_tick_returns_the_expected_summary_shape(memory_repo):
    case = make_case()
    memory_repo.insert_case(case)
    scheduler = jobs.Scheduler({case["id"]: case})

    summary = scheduler.tick(NOW)

    assert set(summary) == {"promises_resolved", "cases_processed", "active", "recovered"}


def test_tick_uses_the_on_action_executed_callback_to_decide_recovery(memory_repo):
    case = make_case()
    memory_repo.insert_case(case)
    scheduler = jobs.Scheduler({case["id"]: case}, on_action_executed=lambda c, d, now: True)

    scheduler.tick(NOW)

    assert case["state"] == "RECOVERED"
    assert case["recovered_amount"] == 499.0


def test_tick_with_no_on_action_executed_leaves_case_state_for_later_resolution(memory_repo):
    """Live mode's default (on_action_executed=None): a successfully executed
    action doesn't get graded synchronously -- it's left in its natural
    post-execution state for a real webhook to resolve later."""
    case = make_case()
    memory_repo.insert_case(case)
    scheduler = jobs.Scheduler({case["id"]: case})  # on_action_executed defaults to None

    scheduler.tick(NOW)

    assert case["state"] == "OUTREACH_SENT"
    assert case["state"] != "RECOVERED"


# ---------------------------------------------------------------------------
# Scheduler.tick() -- promise resolution
# ---------------------------------------------------------------------------

def test_tick_resolves_a_due_promise_as_kept(memory_repo):
    case = make_case(state="PROMISE_MADE")
    memory_repo.insert_case(case)
    memory_repo.insert_promise({
        "case_id": case["id"], "promised_amount": 499.0,
        "promised_date": NOW.date().isoformat(), "status": "pending", "source": "text",
    })
    scheduler = jobs.Scheduler({case["id"]: case}, promise_is_paid=lambda p: True)

    summary = scheduler.tick(NOW)

    assert summary["promises_resolved"] == 1
    assert case["state"] == "RECOVERED"
    assert case["recovered_amount"] == 499.0


def test_tick_resolves_a_due_promise_as_broken(memory_repo):
    case = make_case(state="PROMISE_MADE")
    memory_repo.insert_case(case)
    memory_repo.insert_promise({
        "case_id": case["id"], "promised_amount": 499.0,
        "promised_date": NOW.date().isoformat(), "status": "pending", "source": "text",
    })
    scheduler = jobs.Scheduler({case["id"]: case}, promise_is_paid=lambda p: False)

    summary = scheduler.tick(NOW)

    assert summary["promises_resolved"] == 1
    assert case["state"] == "ESCALATED"


def test_tick_default_promise_is_paid_checks_case_state(memory_repo):
    """No promise_is_paid callback (live mode's default): tracker's own
    fallback checks whether the case is already RECOVERED."""
    case = make_case(state="RECOVERED")
    memory_repo.insert_case(case)
    memory_repo.insert_promise({
        "case_id": case["id"], "promised_amount": 499.0,
        "promised_date": NOW.date().isoformat(), "status": "pending", "source": "text",
    })
    scheduler = jobs.Scheduler({case["id"]: case})  # promise_is_paid defaults to None

    scheduler.tick(NOW)

    promise = memory_repo.promises_for_case(case["id"])[0]
    assert promise["status"] == "kept"


# ---------------------------------------------------------------------------
# load_new_cases -- live mode only
# ---------------------------------------------------------------------------

def test_load_new_cases_adds_only_unseen_case_ids(memory_repo):
    existing = make_case(id="c1")
    scheduler = jobs.Scheduler({"c1": existing})

    added = scheduler.load_new_cases([
        {"id": "c1", "state": "DIFFERENT"},  # already tracked -- must not clobber
        {"id": "c2", "state": "DETECTED"},
    ])

    assert added == 1
    assert scheduler.cases["c1"] is existing  # untouched, same object
    assert "c2" in scheduler.cases


# ---------------------------------------------------------------------------
# sweep_unresolved -- batch-only, never called from tick()
# ---------------------------------------------------------------------------

def test_sweep_unresolved_closes_non_terminal_cases(memory_repo):
    case = make_case(state="OUTREACH_SENT")
    memory_repo.insert_case(case)
    scheduler = jobs.Scheduler({case["id"]: case})

    swept = scheduler.sweep_unresolved(NOW)

    assert swept == 1
    assert case["state"] == "CLOSED_LOST"
    audit_rows = memory_repo.audit_for_case(case["id"])
    assert any(r["event_type"] == "CLOSED_LOST" for r in audit_rows)


def test_sweep_unresolved_leaves_terminal_cases_alone(memory_repo):
    case = make_case(state="RECOVERED")
    memory_repo.insert_case(case)
    scheduler = jobs.Scheduler({case["id"]: case})

    swept = scheduler.sweep_unresolved(NOW)

    assert swept == 0
    assert case["state"] == "RECOVERED"


def test_tick_never_sweeps_a_case_past_a_horizon(memory_repo):
    """sweep_unresolved is a distinct method the caller invokes explicitly --
    tick() itself has no concept of a horizon and must never close a case
    just because it's still non-terminal."""
    case = make_case(state="OUTREACH_SENT")
    memory_repo.insert_case(case)
    scheduler = jobs.Scheduler({case["id"]: case})
    scheduler.next_action_at[case["id"]] = NOW + timedelta(days=5)  # not due yet

    scheduler.tick(NOW)

    assert case["state"] == "OUTREACH_SENT"


# ---------------------------------------------------------------------------
# the actual point of this refactor: simulated and live ticks share the
# exact same resolution logic
# ---------------------------------------------------------------------------

def test_simulated_and_live_drivers_produce_identical_results_via_the_same_tick(memory_repo):
    """Drive two independently-seeded Schedulers through the same 3-tick
    sequence -- one via SimulatedClock.advance() (how batch_scanner drives
    it), the other via plain datetime increments (how the live daemon's own
    loop drives it, see app.scheduler.jobs.run_daemon). Both must reach the
    identical case outcome, because both call the exact same tick() -- there
    is only one implementation of "what happens on a tick," not two that
    happen to agree.
    """
    case_a = make_case(id="case_simulated")
    case_b = make_case(id="case_live")
    memory_repo.insert_case(case_a)
    memory_repo.insert_case(case_b)

    scheduler_a = jobs.Scheduler({case_a["id"]: case_a})
    clock = jobs.SimulatedClock(NOW)
    for _ in range(3):
        scheduler_a.tick(clock.now)
        clock.advance(1)

    scheduler_b = jobs.Scheduler({case_b["id"]: case_b})
    live_now = NOW
    for _ in range(3):
        scheduler_b.tick(live_now)
        live_now = live_now + timedelta(days=1)

    assert case_a["state"] == case_b["state"] == "CLOSED_LOST"  # G3: cap reached on tick 2
    assert case_a["attempts_made"] == case_b["attempts_made"] == 2
    assert dict(scheduler_a.gate_block_counts) == dict(scheduler_b.gate_block_counts)


# ---------------------------------------------------------------------------
# live mode: _load_active_cases, make_live_scheduler, run_once
# ---------------------------------------------------------------------------

def test_load_active_cases_filters_out_terminal_states(monkeypatch):
    rows = [
        {"id": "c1", "state": "OUTREACH_SENT"},
        {"id": "c2", "state": "RECOVERED"},
        {"id": "c3", "state": "CLOSED_LOST"},
        {"id": "c4", "state": "PROMISE_MADE"},
    ]
    monkeypatch.setattr(repository_module, "list_cases", lambda batch_id=None, limit=1000: rows)

    cases = jobs._load_active_cases()

    assert set(cases) == {"c1", "c4"}


def test_make_live_scheduler_is_wired_for_real_razorpay_with_no_simulation(monkeypatch):
    monkeypatch.setattr(repository_module, "list_cases", lambda batch_id=None, limit=1000: [])

    scheduler = jobs.make_live_scheduler()

    assert scheduler.live is True
    assert scheduler.on_action_executed is None
    assert scheduler.promise_is_paid is None


def test_run_once_checks_schema_before_doing_anything_else(monkeypatch):
    calls = []
    monkeypatch.setattr(repository_module, "verify_schema", lambda: calls.append("verify_schema"))
    monkeypatch.setattr(
        repository_module, "list_cases",
        lambda batch_id=None, limit=1000: calls.append("list_cases") or [],
    )
    # tick() resolves due promises globally regardless of how many cases were
    # loaded -- must not reach the real client even with zero cases.
    monkeypatch.setattr(repository_module, "due_promises", lambda on_date: [])

    jobs.run_once()

    assert calls == ["verify_schema", "list_cases"]


def test_run_once_returns_a_summary_including_cases_loaded(memory_repo, monkeypatch):
    # verify_schema isn't part of _REPOSITORY_FUNCTIONS (it checks the real
    # Supabase project's schema, not something MemoryRepository has a notion
    # of) -- stub it separately so this test never touches the real client.
    monkeypatch.setattr(repository_module, "verify_schema", lambda: None)
    # make_live_scheduler() always sets live=True (real Razorpay) -- this
    # env has real keys configured, so without this stub a money-action case
    # would actually call out to Razorpay's live API and its real backoff
    # delays. Stub it the same way scripts/demo_graceful_failure.py does.
    import app.execution.actions as actions_module
    monkeypatch.setattr(actions_module.razorpay_client, "create_payment_link", lambda **kw: {"id": "plink_test"})
    memory_repo.insert_case(make_case(id="c1"))

    summary = jobs.run_once()

    assert summary["cases_loaded"] == 1
    assert "gate_block_counts" in summary
    assert "active" in summary and "recovered" in summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_requires_exactly_one_of_once_or_daemon(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["jobs.py"])
    with pytest.raises(SystemExit):
        jobs.main()
    assert "required" in capsys.readouterr().err.lower()


def test_cli_rejects_both_once_and_daemon(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["jobs.py", "--once", "--daemon"])
    with pytest.raises(SystemExit):
        jobs.main()
    assert "not allowed with" in capsys.readouterr().err.lower()


def test_cli_once_calls_run_once_and_exits_cleanly(monkeypatch):
    monkeypatch.setattr("sys.argv", ["jobs.py", "--once"])
    monkeypatch.setattr(
        jobs, "run_once",
        lambda batch_id=None: {
            "cases_loaded": 0, "cases_processed": 0, "promises_resolved": 0,
            "active": 0, "recovered": 0, "gate_block_counts": {},
        },
    )

    assert jobs.main() == 0


def test_cli_daemon_calls_run_daemon(monkeypatch):
    calls = []
    monkeypatch.setattr("sys.argv", ["jobs.py", "--daemon", "--interval-minutes", "5"])
    monkeypatch.setattr(jobs, "run_daemon", lambda interval_minutes=None, batch_id=None: calls.append(interval_minutes))

    assert jobs.main() == 0
    assert calls == [5]
