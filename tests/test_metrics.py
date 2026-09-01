"""
Tests for app/metrics/compute.py -- every metric in PRD.md Sec.4.

Each Sec 4.3 safety invariant gets two tests: one on clean data (must be 0)
and one that seeds the exact violation the metric exists to catch, proving
the check actually detects it rather than being asserted away.
"""
from __future__ import annotations

import pytest

from app.metrics import compute as metrics

POLICY = {
    "max_discount_pct": 10.0,
    "cost_per_message_inr": {"whatsapp": 0.35, "sms": 0.20, "email": 0.02, "voice": 1.50},
}


def make_case(**overrides) -> dict:
    case = {
        "id": "case_1",
        "reason_category": "insufficient_funds",
        "amount": 500.0,
        "state": "RECOVERED",
        "recovered_amount": 500.0,
        "recovered_at": "2026-01-10T09:00:00+00:00",
        "created_at": "2026-01-05T09:00:00+00:00",
        "attempts_made": 1,
    }
    case.update(overrides)
    return case


# ---------------------------------------------------------------------------
# backward compatibility: existing primary/governance/honesty keys unchanged
# ---------------------------------------------------------------------------

def test_compute_still_returns_the_existing_primary_keys():
    cases = [make_case()]
    result = metrics.compute(cases)
    for key in (
        "total_cases", "at_risk_value", "recovered_count", "recovered_value",
        "recovery_rate_count", "recovery_rate_value", "recovery_lift", "ceiling_capture",
        "recovery_by_reason", "gate_block_counts", "exception_list",
    ):
        assert key in result


def test_compute_works_with_no_optional_data_at_all():
    """Every new param is optional -- an old-style call (just cases) must not
    crash, and the new metrics degrade to their empty-data defaults."""
    result = metrics.compute([make_case(state="ESCALATED", recovered_amount=0.0, recovered_at=None)])
    assert result["kept_promise_rate"] is None
    assert result["false_escalation_rate"] is None
    assert result["double_charge_incidents"] == 0
    assert result["post_opt_out_contacts"] == 0
    assert result["actions_without_audit"] == 0
    assert result["over_cap_discounts"] == 0


# ---------------------------------------------------------------------------
# ceiling_capture -- recovered_value can never exceed the recoverable ceiling
# ---------------------------------------------------------------------------

def test_ceiling_capture_is_none_with_no_ceiling_supplied():
    result = metrics.compute([make_case()])
    assert result["ceiling_capture"] is None


def test_ceiling_capture_computes_normally_within_bounds():
    cases = [make_case(recovered_amount=250.0)]
    ceiling = {"recoverable_count": 1, "recoverable_value": 500.0}
    result = metrics.compute(cases, ceiling=ceiling)
    assert result["ceiling_capture"] == 0.5


def test_ceiling_capture_at_exactly_100_percent_does_not_raise():
    cases = [make_case(amount=500.0, recovered_amount=500.0)]
    ceiling = {"recoverable_count": 1, "recoverable_value": 500.0}
    result = metrics.compute(cases, ceiling=ceiling)
    assert result["ceiling_capture"] == pytest.approx(1.0)


def test_ceiling_capture_above_100_percent_raises():
    """This is a mathematical impossibility, not a policy violation to report
    and move past: recovered cases are by definition a subset of the
    recoverable ones. A caller that lets recovered_value exceed the
    recoverable ceiling (e.g. batch_scanner recovering a case whose
    latent["recoverable"] was False) has a real bug, and compute() must fail
    loudly right where it's computed instead of reporting a nonsensical
    number."""
    cases = [make_case(amount=600.0, recovered_amount=600.0)]
    ceiling = {"recoverable_count": 1, "recoverable_value": 500.0}
    with pytest.raises(ValueError, match="exceeds 100%"):
        metrics.compute(cases, ceiling=ceiling)


# ---------------------------------------------------------------------------
# 4.2 operational
# ---------------------------------------------------------------------------

def test_kept_promise_rate():
    promises = [{"status": "kept"}, {"status": "kept"}, {"status": "broken"}, {"status": "pending"}]
    result = metrics.compute([make_case()], promises=promises)
    assert result["kept_promise_rate"] == 2 / 3


def test_kept_promise_rate_is_none_with_no_resolved_promises():
    result = metrics.compute([make_case()], promises=[{"status": "pending"}])
    assert result["kept_promise_rate"] is None


def test_false_escalation_rate_counts_a_case_recovered_after_escalation():
    audit_rows = [
        {"case_id": "c1", "event_type": "ESCALATED", "ts": "2026-01-05T10:00:00+00:00"},
        {"case_id": "c1", "event_type": "RECOVERED", "ts": "2026-01-06T10:00:00+00:00"},
        {"case_id": "c2", "event_type": "ESCALATED", "ts": "2026-01-05T10:00:00+00:00"},
    ]
    result = metrics.compute([make_case()], audit_rows=audit_rows)
    assert result["false_escalation_rate"] == 0.5  # 1 of 2 escalated cases self-resolved


def test_false_escalation_rate_ignores_a_recovery_that_predates_the_escalation():
    """RECOVERED before ESCALATED isn't a self-resolve -- it's a different
    sequence entirely (e.g. a stale/replayed audit row); must not count."""
    audit_rows = [
        {"case_id": "c1", "event_type": "RECOVERED", "ts": "2026-01-04T10:00:00+00:00"},
        {"case_id": "c1", "event_type": "ESCALATED", "ts": "2026-01-05T10:00:00+00:00"},
    ]
    result = metrics.compute([make_case()], audit_rows=audit_rows)
    assert result["false_escalation_rate"] == 0.0


def test_false_escalation_rate_is_none_with_no_escalations():
    result = metrics.compute([make_case()], audit_rows=[])
    assert result["false_escalation_rate"] is None


def test_avg_time_to_recovery_days():
    cases = [
        make_case(id="a", created_at="2026-01-01T09:00:00+00:00", recovered_at="2026-01-03T09:00:00+00:00"),
        make_case(id="b", created_at="2026-01-01T09:00:00+00:00", recovered_at="2026-01-06T09:00:00+00:00"),
    ]
    result = metrics.compute(cases)
    assert result["avg_time_to_recovery_days"] == 3.5  # mean(2, 5)


def test_avg_time_to_recovery_days_is_none_with_no_recovered_cases():
    result = metrics.compute([make_case(state="CLOSED_LOST", recovered_at=None)])
    assert result["avg_time_to_recovery_days"] is None


def test_interventions_per_recovery():
    attempts = [{"case_id": "c1"}, {"case_id": "c1"}]
    outreach = [{"case_id": "c1", "channel": "whatsapp"}]
    cases = [make_case(), make_case(id="c2", state="CLOSED_LOST", recovered_amount=0.0, recovered_at=None)]
    result = metrics.compute(cases, attempts=attempts, outreach=outreach)
    assert result["interventions_per_recovery"] == 3.0  # (2 attempts + 1 outreach) / 1 recovered


def test_interventions_per_recovery_is_none_with_no_recoveries():
    result = metrics.compute([make_case(state="CLOSED_LOST", recovered_amount=0.0, recovered_at=None)])
    assert result["interventions_per_recovery"] is None


def test_cost_per_recovered_rupee():
    outreach = [
        {"case_id": "c1", "channel": "whatsapp"},  # 0.35
        {"case_id": "c1", "channel": "voice"},      # 1.50
    ]
    result = metrics.compute([make_case(recovered_amount=500.0)], outreach=outreach, policy=POLICY)
    assert result["cost_per_recovered_rupee"] == (0.35 + 1.50) / 500.0


def test_cost_per_recovered_rupee_is_none_with_nothing_recovered():
    result = metrics.compute(
        [make_case(state="CLOSED_LOST", recovered_amount=0.0, recovered_at=None)],
        outreach=[{"case_id": "c1", "channel": "whatsapp"}], policy=POLICY,
    )
    assert result["cost_per_recovered_rupee"] is None


def test_contact_efficiency():
    outreach = [{"case_id": "c1"}, {"case_id": "c2"}]
    cases = [make_case(id="c1"), make_case(id="c2", state="CLOSED_LOST", recovered_amount=0.0, recovered_at=None)]
    result = metrics.compute(cases, outreach=outreach)
    assert result["contact_efficiency"] == 0.5  # 1 recovered / 2 messages


def test_contact_efficiency_is_none_with_no_outreach():
    result = metrics.compute([make_case()], outreach=[])
    assert result["contact_efficiency"] is None


# ---------------------------------------------------------------------------
# 4.3 safety invariants -- clean data -> PASS (0), seeded violation -> caught
# ---------------------------------------------------------------------------

def test_double_charge_incidents_is_zero_on_clean_data():
    attempts = [
        {"case_id": "c1", "idempotency_key": "c1:retry_now:1"},
        {"case_id": "c2", "idempotency_key": "c2:retry_now:1"},
    ]
    result = metrics.compute([make_case()], attempts=attempts)
    assert result["double_charge_incidents"] == 0


def test_double_charge_incidents_detects_a_seeded_duplicate_key():
    attempts = [
        {"case_id": "c1", "idempotency_key": "c1:retry_now:1"},
        {"case_id": "c1", "idempotency_key": "c1:retry_now:1"},  # duplicate -- must never happen
        {"case_id": "c2", "idempotency_key": "c2:retry_now:1"},
    ]
    result = metrics.compute([make_case()], attempts=attempts)
    assert result["double_charge_incidents"] == 1


def test_post_opt_out_contacts_is_zero_when_all_outreach_predates_the_opt_out():
    audit_rows = [
        {"case_id": "c1", "event_type": "REPLY_RECEIVED", "decision": "opt_out", "ts": "2026-01-05T10:00:00+00:00"},
    ]
    outreach = [{"case_id": "c1", "sent_at": "2026-01-04T10:00:00+00:00"}]
    result = metrics.compute([make_case()], outreach=outreach, audit_rows=audit_rows)
    assert result["post_opt_out_contacts"] == 0


def test_post_opt_out_contacts_detects_a_message_sent_after_opt_out():
    audit_rows = [
        {"case_id": "c1", "event_type": "REPLY_RECEIVED", "decision": "opt_out", "ts": "2026-01-05T10:00:00+00:00"},
    ]
    outreach = [
        {"case_id": "c1", "sent_at": "2026-01-04T10:00:00+00:00"},  # before -- fine
        {"case_id": "c1", "sent_at": "2026-01-06T10:00:00+00:00"},  # after opt-out -- violation
    ]
    result = metrics.compute([make_case()], outreach=outreach, audit_rows=audit_rows)
    assert result["post_opt_out_contacts"] == 1


def test_actions_without_audit_is_zero_when_every_action_has_its_row():
    attempts = [{"case_id": "c1"}]
    outreach = [{"case_id": "c1"}]
    audit_rows = [
        {"case_id": "c1", "event_type": "ACTED"},
        {"case_id": "c1", "event_type": "OUTREACH_SENT"},
    ]
    result = metrics.compute([make_case()], attempts=attempts, outreach=outreach, audit_rows=audit_rows)
    assert result["actions_without_audit"] == 0


def test_actions_without_audit_detects_an_attempt_with_no_acted_row():
    attempts = [{"case_id": "c1"}]
    result = metrics.compute([make_case()], attempts=attempts, outreach=[], audit_rows=[])
    assert result["actions_without_audit"] == 1


def test_actions_without_audit_detects_an_outreach_message_with_no_outreach_sent_row():
    outreach = [{"case_id": "c1"}]
    result = metrics.compute([make_case()], attempts=[], outreach=outreach, audit_rows=[])
    assert result["actions_without_audit"] == 1


def test_over_cap_discounts_is_zero_when_every_allowed_discount_is_within_cap():
    audit_rows = [{"event_type": "GATE_ALLOW", "input": {"discount_pct": 8.0}}]
    result = metrics.compute([make_case()], audit_rows=audit_rows, policy=POLICY)
    assert result["over_cap_discounts"] == 0


def test_over_cap_discounts_detects_an_allowed_discount_above_the_cap():
    audit_rows = [{"event_type": "GATE_ALLOW", "input": {"discount_pct": 15.0}}]  # cap is 10.0
    result = metrics.compute([make_case()], audit_rows=audit_rows, policy=POLICY)
    assert result["over_cap_discounts"] == 1


def test_over_cap_discounts_ignores_a_discount_that_g6_actually_blocked():
    """A GATE_BLOCK row above the cap is G6 doing its job, not a violation --
    only a discount that made it through as GATE_ALLOW counts."""
    audit_rows = [{"event_type": "GATE_BLOCK", "input": {"discount_pct": 15.0}}]
    result = metrics.compute([make_case()], audit_rows=audit_rows, policy=POLICY)
    assert result["over_cap_discounts"] == 0


# ---------------------------------------------------------------------------
# 4.4 honesty artifacts -- worst_three_reasons
# ---------------------------------------------------------------------------

def test_worst_three_reasons_orders_by_lowest_recovery_rate():
    cases = [
        make_case(id="a1", reason_category="mandate_revoked", state="CLOSED_LOST", recovered_amount=0.0, recovered_at=None, amount=100.0),
        make_case(id="a2", reason_category="mandate_revoked", state="CLOSED_LOST", recovered_amount=0.0, recovered_at=None, amount=100.0),
        make_case(id="b1", reason_category="expired_card", state="RECOVERED", amount=200.0, recovered_amount=200.0),
        make_case(id="b2", reason_category="expired_card", state="CLOSED_LOST", recovered_amount=0.0, recovered_at=None, amount=200.0),
        make_case(id="c1", reason_category="insufficient_funds", state="RECOVERED", amount=300.0, recovered_amount=300.0),
    ]
    result = metrics.compute(cases)
    worst = result["worst_three_reasons"]
    assert len(worst) == 3
    assert worst[0]["reason_category"] == "mandate_revoked"  # 0% recovery, worst
    assert worst[0]["count"] == 2
    assert worst[0]["rupees_lost"] == 200.0
    assert worst[1]["reason_category"] == "expired_card"  # 50%
    assert worst[2]["reason_category"] == "insufficient_funds"  # 100%, still 3rd since only 3 reasons exist


def test_worst_three_reasons_reports_the_dominant_blocking_gate():
    cases = [make_case(id="c1", reason_category="mandate_revoked", state="ESCALATED", recovered_amount=0.0, recovered_at=None)]
    audit_rows = [
        {"case_id": "c1", "event_type": "GATE_BLOCK", "decision": "BLOCK (G3)"},
        {"case_id": "c1", "event_type": "GATE_BLOCK", "decision": "BLOCK (G3)"},
        {"case_id": "c1", "event_type": "GATE_BLOCK", "decision": "BLOCK (G5)"},
    ]
    result = metrics.compute(cases, audit_rows=audit_rows)
    assert result["worst_three_reasons"][0]["dominant_failure_mode"] == "G3"


def test_worst_three_reasons_dominant_gate_counts_distinct_cases_not_raw_block_rows():
    """Regression: a single case re-blocked many times by one gate (e.g. G10
    firing once per day while a promise is pending -- doing its job, but
    generating many audit rows for the same case) must not out-vote a gate
    that blocked many more DISTINCT cases, each only once."""
    cases = [
        make_case(id=f"g5_{i}", reason_category="expired_card", state="CLOSED_LOST",
                   recovered_amount=0.0, recovered_at=None)
        for i in range(6)
    ] + [
        make_case(id="g10_case", reason_category="expired_card", state="ESCALATED",
                   recovered_amount=0.0, recovered_at=None)
    ]
    audit_rows = (
        [{"case_id": f"g5_{i}", "event_type": "GATE_BLOCK", "decision": "BLOCK (G5)"} for i in range(6)]
        + [{"case_id": "g10_case", "event_type": "GATE_BLOCK", "decision": "BLOCK (G10)"} for _ in range(7)]
    )
    result = metrics.compute(cases, audit_rows=audit_rows)
    assert result["worst_three_reasons"][0]["dominant_failure_mode"] == "G5"


def test_worst_three_reasons_dominant_gate_ignores_blocks_from_cases_that_recovered_anyway():
    """A case can hit a gate on the way to eventually recovering -- that
    block shouldn't count toward "why this reason fails," since the case
    didn't fail."""
    cases = [
        make_case(id="c1", reason_category="checkout_dropoff", state="CLOSED_LOST",
                   recovered_amount=0.0, recovered_at=None),
        make_case(id="c2", reason_category="checkout_dropoff", state="RECOVERED",
                   amount=200.0, recovered_amount=200.0),
    ]
    audit_rows = [
        {"case_id": "c1", "event_type": "GATE_BLOCK", "decision": "BLOCK (G3)"},
        {"case_id": "c2", "event_type": "GATE_BLOCK", "decision": "BLOCK (G10)"},
    ]
    result = metrics.compute(cases, audit_rows=audit_rows)
    worst = next(r for r in result["worst_three_reasons"] if r["reason_category"] == "checkout_dropoff")
    assert worst["dominant_failure_mode"] == "G3"


def test_worst_three_reasons_falls_back_to_terminal_state_with_no_gate_blocks():
    cases = [make_case(id="c1", reason_category="bank_downtime", state="CLOSED_LOST", recovered_amount=0.0, recovered_at=None)]
    result = metrics.compute(cases, audit_rows=[])
    assert result["worst_three_reasons"][0]["dominant_failure_mode"] == "CLOSED_LOST"
