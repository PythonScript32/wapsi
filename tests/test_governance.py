"""
Tests for the governance gate.

Every gate gets at least one blocking test and one passing test. This is
possible without any database because check() is a pure function — see the
design note in app/governance/policy_gate.py.

Run:  pytest tests/test_governance.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import DEFAULT_POLICY
from app.governance.policy_gate import check

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def make_case(**overrides) -> dict:
    """A clean, healthy case that passes every gate. Override one field per
    test so each test isolates exactly one gate."""
    case = {
        "id": "case_test000001",
        "state": "DIAGNOSED",
        "source": "subscription",
        "reason_category": "insufficient_funds",
        "amount": 499.0,
        "attempts_made": 0,
        "opted_out": False,
        "created_at": (NOW - timedelta(days=1)).isoformat(),
    }
    case.update(overrides)
    return case


def make_action(**overrides) -> dict:
    action = {
        "type": "charge",
        "intervention": "retry_after_date",
        "idempotency_key": "case_test000001:retry_after_date:1",
        "amount": 499.0,
        "discount_pct": 0,
        "is_mandate_debit": False,
    }
    action.update(overrides)
    return action


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

def test_healthy_case_passes_all_gates():
    result = check(make_action(), make_case(), DEFAULT_POLICY, now=NOW)
    assert result.allowed
    assert result.gate is None
    assert bool(result) is True   # GateResult is truthy when allowed


# ---------------------------------------------------------------------------
# G0 — fail closed
# ---------------------------------------------------------------------------

def test_g0_unknown_action_type_blocks():
    result = check(make_action(type="teleport"), make_case(), DEFAULT_POLICY, now=NOW)
    assert not result.allowed
    assert result.gate == "G0"


@pytest.mark.parametrize("bad", [None, "string", 42, []])
def test_g0_malformed_input_blocks(bad):
    assert not check(bad, make_case(), DEFAULT_POLICY, now=NOW).allowed
    assert not check(make_action(), bad, DEFAULT_POLICY, now=NOW).allowed


# ---------------------------------------------------------------------------
# G1 — terminal states
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["RECOVERED", "CLOSED_LOST"])
def test_g1_terminal_state_blocks_everything(state):
    result = check(make_action(), make_case(state=state), DEFAULT_POLICY, now=NOW)
    assert not result.allowed
    assert result.gate == "G1"


def test_g1_blocks_outreach_too_not_just_money():
    result = check(
        make_action(type="outreach"),
        make_case(state="RECOVERED"),
        DEFAULT_POLICY,
        now=NOW,
    )
    assert not result.allowed
    assert result.gate == "G1"


# ---------------------------------------------------------------------------
# G2 — opt-out
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action_type", ["charge", "retry", "outreach", "offer", "voice"])
def test_g2_opt_out_blocks_every_action_type(action_type):
    result = check(
        make_action(type=action_type),
        make_case(opted_out=True),
        DEFAULT_POLICY,
        now=NOW,
    )
    assert not result.allowed
    assert result.gate == "G2"


def test_g2_runs_before_attempt_cap():
    """Opt-out must win even when other gates would also block — the reason
    given to a human should be the most important one."""
    result = check(
        make_action(),
        make_case(opted_out=True, attempts_made=99),
        DEFAULT_POLICY,
        now=NOW,
    )
    assert result.gate == "G2"


# ---------------------------------------------------------------------------
# G3 — attempt caps
# ---------------------------------------------------------------------------

def test_g3_blocks_at_cap():
    # insufficient_funds allows 3 attempts
    result = check(make_action(), make_case(attempts_made=3), DEFAULT_POLICY, now=NOW)
    assert not result.allowed
    assert result.gate == "G3"


def test_g3_allows_below_cap():
    assert check(make_action(), make_case(attempts_made=2), DEFAULT_POLICY, now=NOW).allowed


def test_g3_mandate_revoked_allows_only_one_attempt():
    """A revoked mandate cannot be charged at all — retrying is pointless, so
    the cap is 1."""
    case = make_case(reason_category="mandate_revoked", attempts_made=1)
    result = check(make_action(), case, DEFAULT_POLICY, now=NOW)
    assert not result.allowed
    assert result.gate == "G3"


def test_g3_unknown_reason_fails_closed():
    case = make_case(reason_category="something_new")
    result = check(make_action(), case, DEFAULT_POLICY, now=NOW)
    assert not result.allowed
    assert result.gate == "G3"


def test_g3_does_not_apply_to_pure_outreach():
    """Attempt caps bound charges, not messages. Messages are bounded by G4."""
    case = make_case(attempts_made=99)
    assert check(make_action(type="outreach"), case, DEFAULT_POLICY, now=NOW).allowed


# ---------------------------------------------------------------------------
# G4 — contact gap
# ---------------------------------------------------------------------------

def test_g4_blocks_contact_inside_gap():
    result = check(
        make_action(type="outreach"),
        make_case(),
        DEFAULT_POLICY,
        now=NOW,
        last_contact_at=(NOW - timedelta(hours=3)).isoformat(),
    )
    assert not result.allowed
    assert result.gate == "G4"


def test_g4_allows_contact_after_gap():
    result = check(
        make_action(type="outreach"),
        make_case(),
        DEFAULT_POLICY,
        now=NOW,
        last_contact_at=(NOW - timedelta(hours=25)).isoformat(),
    )
    assert result.allowed


def test_g4_first_contact_always_allowed():
    result = check(make_action(type="outreach"), make_case(), DEFAULT_POLICY,
                   now=NOW, last_contact_at=None)
    assert result.allowed


def test_g4_unparseable_timestamp_does_not_crash():
    """A corrupt timestamp must not throw — the gate degrades, it never breaks."""
    result = check(make_action(type="outreach"), make_case(), DEFAULT_POLICY,
                   now=NOW, last_contact_at="not-a-date")
    assert isinstance(result.allowed, bool)


# ---------------------------------------------------------------------------
# G5 — grace period
# ---------------------------------------------------------------------------

def test_g5_blocks_past_grace_period():
    case = make_case(created_at=(NOW - timedelta(days=20)).isoformat())
    result = check(make_action(), case, DEFAULT_POLICY, now=NOW)
    assert not result.allowed
    assert result.gate == "G5"


def test_g5_allows_inside_grace_period():
    case = make_case(created_at=(NOW - timedelta(days=10)).isoformat())
    assert check(make_action(), case, DEFAULT_POLICY, now=NOW).allowed


def test_g5_missing_created_at_fails_closed():
    case = make_case(created_at=None)
    result = check(make_action(), case, DEFAULT_POLICY, now=NOW)
    assert not result.allowed
    assert result.gate == "G5"


# ---------------------------------------------------------------------------
# G6 — discount cap
# ---------------------------------------------------------------------------

def test_g6_blocks_excessive_discount():
    result = check(
        make_action(type="offer", discount_pct=25),
        make_case(reason_category="checkout_dropoff"),
        DEFAULT_POLICY,
        now=NOW,
    )
    assert not result.allowed
    assert result.gate == "G6"


def test_g6_allows_discount_at_cap():
    result = check(
        make_action(type="offer", discount_pct=10),
        make_case(reason_category="checkout_dropoff"),
        DEFAULT_POLICY,
        now=NOW,
    )
    assert result.allowed


# ---------------------------------------------------------------------------
# G7 — exposure cap
# ---------------------------------------------------------------------------

def test_g7_blocks_large_amount():
    result = check(make_action(amount=9999), make_case(amount=9999),
                   DEFAULT_POLICY, now=NOW)
    assert not result.allowed
    assert result.gate == "G7"


def test_g7_allows_normal_amount():
    assert check(make_action(amount=499), make_case(), DEFAULT_POLICY, now=NOW).allowed


# ---------------------------------------------------------------------------
# G8 — idempotency
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", [None, "", "   "])
def test_g8_blocks_money_without_idempotency_key(key):
    result = check(make_action(idempotency_key=key), make_case(), DEFAULT_POLICY, now=NOW)
    assert not result.allowed
    assert result.gate == "G8"


def test_g8_outreach_does_not_need_idempotency_key():
    """Messages are not money. A duplicate message is annoying; a duplicate
    charge is a refund and a lost customer."""
    result = check(make_action(type="outreach", idempotency_key=None),
                   make_case(), DEFAULT_POLICY, now=NOW)
    assert result.allowed


# ---------------------------------------------------------------------------
# G9 — RBI pre-debit notice
# ---------------------------------------------------------------------------

def test_g9_blocks_mandate_debit_without_notice():
    result = check(make_action(is_mandate_debit=True), make_case(),
                   DEFAULT_POLICY, now=NOW, pre_debit_notice_at=None)
    assert not result.allowed
    assert result.gate == "G9"


def test_g9_blocks_when_notice_too_recent():
    result = check(
        make_action(is_mandate_debit=True), make_case(), DEFAULT_POLICY, now=NOW,
        pre_debit_notice_at=(NOW - timedelta(hours=5)).isoformat(),
    )
    assert not result.allowed
    assert result.gate == "G9"


def test_g9_allows_after_24h_notice():
    result = check(
        make_action(is_mandate_debit=True), make_case(), DEFAULT_POLICY, now=NOW,
        pre_debit_notice_at=(NOW - timedelta(hours=25)).isoformat(),
    )
    assert result.allowed


def test_g9_not_required_for_non_mandate_charges():
    result = check(make_action(is_mandate_debit=False), make_case(),
                   DEFAULT_POLICY, now=NOW, pre_debit_notice_at=None)
    assert result.allowed


# ---------------------------------------------------------------------------
# G10 — active promise pauses other outreach
# ---------------------------------------------------------------------------

def test_g10_active_promise_blocks_other_outreach():
    result = check(make_action(type="outreach", intervention="send_link"),
                   make_case(), DEFAULT_POLICY, now=NOW, has_active_promise=True)
    assert not result.allowed
    assert result.gate == "G10"


def test_g10_allows_the_promised_retry_itself():
    result = check(make_action(intervention="promise_retry"), make_case(),
                   DEFAULT_POLICY, now=NOW, has_active_promise=True)
    assert result.allowed


# ---------------------------------------------------------------------------
# Cross-cutting
# ---------------------------------------------------------------------------

def test_every_block_explains_itself():
    """A block with no reason is useless in an audit trail."""
    blocks = [
        check(make_action(), make_case(state="RECOVERED"), DEFAULT_POLICY, now=NOW),
        check(make_action(), make_case(opted_out=True), DEFAULT_POLICY, now=NOW),
        check(make_action(idempotency_key=None), make_case(), DEFAULT_POLICY, now=NOW),
        check(make_action(amount=99999), make_case(amount=99999), DEFAULT_POLICY, now=NOW),
    ]
    for b in blocks:
        assert not b.allowed
        assert b.gate
        assert len(b.reason) > 20, "reason must be human-readable, not a code"


def test_policy_bounds_come_from_config_not_hardcoded():
    """Tightening policy must change behaviour with no code edit."""
    strict = {**DEFAULT_POLICY, "max_exposure_inr": 100}
    assert check(make_action(amount=499), make_case(), DEFAULT_POLICY, now=NOW).allowed
    assert not check(make_action(amount=499), make_case(), strict, now=NOW).allowed
