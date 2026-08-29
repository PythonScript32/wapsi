"""
Decision engine tests. The engine proposes; governance.policy_gate still has
final say before anything executes — these tests only check that the engine's
proposals are the reason-appropriate, bounded ones described in PRD.md §5 and
AGENTS.md's golden rules.

No test here touches the network: use_llm defaults to False, and where a test
does pass use_llm=True, the LLM call is mocked at its single seam,
app.llm.client.call.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import DEFAULT_POLICY
from app.decision import engine
from app.decision.engine import decide

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)  # safely mid-month


@pytest.fixture(autouse=True)
def no_audit_writes(monkeypatch):
    monkeypatch.setattr(engine.audit_log, "record", lambda *a, **k: None)


def make_case(**overrides) -> dict:
    case = {
        "id": "case_test000001",
        "source": "subscription",
        "reason_category": "insufficient_funds",
        "amount": 499.0,
        "attempts_made": 0,
        "opted_out": False,
        "customer_ref": "Priya Sharma",
        "created_at": (NOW - timedelta(days=1)).isoformat(),
    }
    case.update(overrides)
    return case


def scheduled_dt(result: dict) -> datetime:
    return datetime.fromisoformat(result["scheduled_for"])


# ---------------------------------------------------------------------------
# insufficient_funds — never today, never before the next salary day
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hour", [0, 6, 9, 12, 18, 23])
def test_insufficient_funds_never_scheduled_today(hour):
    now = NOW.replace(hour=hour)
    result = decide(make_case(), [], DEFAULT_POLICY, now=now)
    assert result["intervention"] == "retry_after_date"
    assert scheduled_dt(result).date() > now.date()


def test_insufficient_funds_picks_month_end_or_first_whichever_is_sooner():
    now = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)  # month-end is 10 days out
    case = make_case(created_at=(now - timedelta(days=1)).isoformat())
    result = decide(case, [], DEFAULT_POLICY, now=now)
    sched = scheduled_dt(result)
    assert sched.date() == datetime(2026, 9, 30).date()  # month-end comes first


def test_insufficient_funds_on_month_end_rolls_to_next_month():
    """If today already IS the salary-cluster date, guessing 'today' again is
    useless — the next candidate must be the 1st of the following month."""
    now = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)  # last day of Sep
    case = make_case(created_at=(now - timedelta(days=1)).isoformat())
    result = decide(case, [], DEFAULT_POLICY, now=now)
    sched = scheduled_dt(result)
    assert sched.date() == datetime(2026, 10, 1).date()
    assert sched.date() > now.date()


def test_insufficient_funds_is_mandate_debit():
    result = decide(make_case(), [], DEFAULT_POLICY, now=NOW)
    assert result["is_mandate_debit"] is True


# ---------------------------------------------------------------------------
# mandate_revoked — never a charge, never a silent retry
# ---------------------------------------------------------------------------

def test_mandate_revoked_never_returns_a_charge_type_intervention():
    result = decide(make_case(reason_category="mandate_revoked"), [], DEFAULT_POLICY, now=NOW)
    assert result["intervention"] not in {"retry_now", "retry_after_date"}
    assert result["intervention"] == "request_re_mandate"


def test_mandate_revoked_is_never_a_mandate_debit():
    result = decide(make_case(reason_category="mandate_revoked"), [], DEFAULT_POLICY, now=NOW)
    assert result["is_mandate_debit"] is False


def test_mandate_revoked_still_proposes_request_re_mandate_past_its_attempt_cap():
    """The engine no longer checks the attempt cap — it proposes the same
    reason-appropriate intervention regardless of attempts_made. Cap
    enforcement is the gate's job now (G3, see test_governance.py), which
    execution.actions.py translates into ESCALATED."""
    case = make_case(reason_category="mandate_revoked", attempts_made=1)  # cap is 1
    result = decide(case, [], DEFAULT_POLICY, now=NOW)
    assert result["intervention"] == "request_re_mandate"


# ---------------------------------------------------------------------------
# bank_downtime — exponential backoff, increases with attempt number
# ---------------------------------------------------------------------------

def test_bank_downtime_backoff_increases_with_attempt_number():
    times = []
    for attempts_made in (0, 1, 2):
        case = make_case(reason_category="bank_downtime", attempts_made=attempts_made)
        result = decide(case, [], DEFAULT_POLICY, now=NOW)
        assert result["intervention"] == "retry_after_date"
        times.append(scheduled_dt(result))

    assert times[0] < times[1] < times[2]


def test_bank_downtime_matches_configured_backoff_schedule():
    policy = {**DEFAULT_POLICY, "backoff_hours": [4, 12, 24]}
    case = make_case(reason_category="bank_downtime", attempts_made=1)  # attempt 2
    result = decide(case, [], policy, now=NOW)
    assert scheduled_dt(result) == NOW + timedelta(hours=12)


# ---------------------------------------------------------------------------
# discount_pct — proposed as-is; the engine no longer clamps it
# ---------------------------------------------------------------------------

def test_discount_pct_is_not_clamped_by_the_engine_even_over_a_tighter_policy_cap():
    """G6 (the gate) is now the sole authority on the discount cap —
    execution.actions.py re-proposes without the discount (or escalates) on
    a G6 block. The engine itself must NOT quietly fix an over-cap proposal;
    doing so would make the gate's own G6 check unreachable, same as the
    attempt-cap and grace-period bounds."""
    strict_policy = {**DEFAULT_POLICY, "max_discount_pct": 3.0}  # tighter than default_offer_pct (10%)
    case = make_case(reason_category="checkout_dropoff", source="checkout", attempts_made=1)
    result = decide(case, [], strict_policy, now=NOW)
    assert result["intervention"] == "send_link_with_offer"
    assert result["discount_pct"] == 10.0  # unclamped — policy["default_offer_pct"], not the 3% cap


def test_discount_pct_is_never_negative():
    case = make_case(reason_category="insufficient_funds")
    result = decide(case, [], DEFAULT_POLICY, now=NOW)
    assert result["discount_pct"] >= 0.0


# ---------------------------------------------------------------------------
# checkout_dropoff — nudge first (no discount), bounded offer only on touch 2
# ---------------------------------------------------------------------------

def test_checkout_dropoff_touch_one_has_zero_discount():
    case = make_case(reason_category="checkout_dropoff", source="checkout", attempts_made=0)
    result = decide(case, [], DEFAULT_POLICY, now=NOW)
    assert result["intervention"] == "send_link"
    assert result["discount_pct"] == 0.0


def test_checkout_dropoff_touch_two_offers_a_discount():
    case = make_case(reason_category="checkout_dropoff", source="checkout", attempts_made=1)
    result = decide(case, [], DEFAULT_POLICY, now=NOW)
    assert result["intervention"] == "send_link_with_offer"
    assert result["discount_pct"] > 0.0


def test_checkout_dropoff_never_offers_to_an_already_paid_customer():
    """FR-B4: never offer to someone who already paid. Decision engine can
    only enforce this via the caller not invoking it on a RECOVERED case —
    verified here as: a fresh, unpaid case never gets skipped, i.e. the touch
    sequencing itself is well-defined and deterministic."""
    case = make_case(reason_category="checkout_dropoff", source="checkout", attempts_made=0)
    result = decide(case, [], DEFAULT_POLICY, now=NOW)
    assert result["intervention"] == "send_link"


def test_checkout_dropoff_still_proposes_the_offer_past_its_touch_cap():
    """FR-B5's "max 2 touches, then CLOSED_LOST" is now enforced by the gate
    (G3) and translated by execution.actions.py — the engine itself just
    keeps proposing touch 2's offer regardless of how many touches have
    already happened; it doesn't self-limit."""
    case = make_case(reason_category="checkout_dropoff", source="checkout", attempts_made=2)  # cap is 2
    result = decide(case, [], DEFAULT_POLICY, now=NOW)
    assert result["intervention"] == "send_link_with_offer"


# ---------------------------------------------------------------------------
# expired_card — request update, then a link fallback
# ---------------------------------------------------------------------------

def test_expired_card_first_attempt_requests_card_update():
    result = decide(make_case(reason_category="expired_card"), [], DEFAULT_POLICY, now=NOW)
    assert result["intervention"] == "request_card_update"


def test_expired_card_second_attempt_falls_back_to_link():
    case = make_case(reason_category="expired_card", attempts_made=1)
    result = decide(case, [], DEFAULT_POLICY, now=NOW)
    assert result["intervention"] == "send_link"


# ---------------------------------------------------------------------------
# attempt caps and grace period — the engine no longer checks either; the
# gate (G3, G5) is the sole authority. See test_governance.py for gate-level
# coverage and test_actions.py for how execution.actions.py translates a
# block into ESCALATED / CLOSED_LOST.
# ---------------------------------------------------------------------------

def test_attempt_cap_is_not_checked_by_the_engine():
    case = make_case(reason_category="bank_downtime", attempts_made=3)  # cap is 3
    result = decide(case, [], DEFAULT_POLICY, now=NOW)
    assert result["intervention"] == "retry_after_date"


def test_grace_period_is_not_checked_by_the_engine():
    case = make_case(created_at=(NOW - timedelta(days=20)).isoformat())  # cap is 14 days
    result = decide(case, [], DEFAULT_POLICY, now=NOW)
    assert result["intervention"] == "retry_after_date"


def test_scheduled_for_is_not_clamped_to_grace_period():
    """A case created 13 days ago with a 14-day grace period leaves only 1 day
    of runway, but the engine proposes the honest salary-day guess anyway —
    however far out it lands. It's the gate's job (G5) to block the eventual
    attempt once the case is actually past grace, not the engine's job to
    quietly pull the schedule back inside a window it isn't checking."""
    case = make_case(created_at=(NOW - timedelta(days=13)).isoformat())
    result = decide(case, [], DEFAULT_POLICY, now=NOW)
    deadline = NOW - timedelta(days=13) + timedelta(days=DEFAULT_POLICY["grace_period_days"])
    assert scheduled_dt(result) > deadline


def test_unknown_reason_fails_safe_to_escalate():
    case = make_case(reason_category="something_new")
    result = decide(case, [], DEFAULT_POLICY, now=NOW)
    assert result["intervention"] == "escalate"


# ---------------------------------------------------------------------------
# history vs attempts_made — whichever implies more attempts wins
# ---------------------------------------------------------------------------

def test_history_length_can_drive_the_attempt_number_past_the_case_counter():
    case = make_case(reason_category="bank_downtime", attempts_made=0)
    history = [{"attempt_no": 1}, {"attempt_no": 2}]  # 2 prior attempts on record
    result = decide(case, history, DEFAULT_POLICY, now=NOW)
    # attempt 3 backoff (24h), not attempt 1 (4h)
    assert scheduled_dt(result) == NOW + timedelta(hours=24)


# ---------------------------------------------------------------------------
# messages — template-first, complete without the LLM, no em dashes
# ---------------------------------------------------------------------------

REASONS = ["insufficient_funds", "bank_downtime", "mandate_revoked", "expired_card"]


@pytest.mark.parametrize("reason", REASONS)
def test_template_message_has_no_em_or_en_dash(reason):
    result = decide(make_case(reason_category=reason), [], DEFAULT_POLICY, now=NOW)
    assert "—" not in result["message"]
    assert "–" not in result["message"]


def test_use_llm_false_never_touches_the_llm_client(monkeypatch):
    def fail_if_called(prompt):
        raise AssertionError("use_llm=False must never call the LLM client")

    monkeypatch.setattr(engine.llm_client, "call", fail_if_called)
    result = decide(make_case(), [], DEFAULT_POLICY, now=NOW)
    assert result["message"]  # template still produced a complete message


def test_use_llm_true_personalizes_the_message(monkeypatch):
    monkeypatch.setattr(engine.llm_client, "call", lambda prompt: "Aapka payment jald retry hoga.")
    result = decide(make_case(), [], DEFAULT_POLICY, now=NOW, use_llm=True)
    assert result["message"] == "Aapka payment jald retry hoga."


def test_use_llm_failure_falls_back_to_the_template(monkeypatch):
    def boom(prompt):
        raise RuntimeError("LLM is down")

    monkeypatch.setattr(engine.llm_client, "call", boom)
    result = decide(make_case(), [], DEFAULT_POLICY, now=NOW, use_llm=True)
    assert "salary" in result["message"] or "Rs" in result["message"]  # template survived


# ---------------------------------------------------------------------------
# audit logging
# ---------------------------------------------------------------------------

def test_decide_logs_decided(monkeypatch):
    calls = []
    monkeypatch.setattr(engine.audit_log, "record", lambda *a, **k: calls.append((a, k)))

    result = decide(make_case(id="case_abc123"), [], DEFAULT_POLICY, now=NOW)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "case_abc123"
    assert args[2] == engine.audit_log.DECIDED
    assert kwargs["decision"] == result["intervention"]
    assert kwargs["reasoning"]
