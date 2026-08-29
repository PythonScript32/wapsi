"""
Diagnosis must map raw gateway strings to the right category, deterministically.

Rules run first and are ground truth. The LLM is only a fallback for reasons no
rule recognises, its answer is constrained to the enum, and it can never
override an unambiguous rule match. No test here touches the network — the LLM
call is mocked at its single seam, app.llm.client.call (see app/llm/client.py
and tests/test_llm_client.py for the client itself).
"""
from __future__ import annotations

import pytest

from app.detection.synthetic_data import RAW_REASONS
from app.diagnosis import classifier


@pytest.fixture(autouse=True)
def no_audit_writes(monkeypatch):
    """Audit writes fail soft against a real DB anyway, but silence + isolate
    them here so tests assert on classify()'s return value, not log side effects."""
    monkeypatch.setattr(classifier.audit_log, "record", lambda *a, **k: None)
    monkeypatch.setattr(classifier.audit_log, "error", lambda *a, **k: None)


def make_case(reason_raw: str, **overrides) -> dict:
    case = {"id": "case_test000001", "reason_raw": reason_raw}
    case.update(overrides)
    return case


# ---------------------------------------------------------------------------
# Rule path — every RAW_REASONS string must map to its expected category
# ---------------------------------------------------------------------------

RAW_REASON_CASES = [
    (expected, raw)
    for expected, raws in RAW_REASONS.items()
    for raw in raws
]


@pytest.mark.parametrize("expected, raw", RAW_REASON_CASES, ids=[r for _, r in RAW_REASON_CASES])
def test_every_raw_reason_maps_via_rule(expected, raw):
    category, how = classifier.classify(make_case(raw))
    assert category == expected
    assert how == "rule"


def test_rules_are_case_insensitive():
    category, how = classifier.classify(make_case("INSUFFICIENT BALANCE, please retry"))
    assert category == "insufficient_funds"
    assert how == "rule"


# ---------------------------------------------------------------------------
# LLM path — only reached when no rule matches
# ---------------------------------------------------------------------------

def test_unknown_reason_routes_to_llm(monkeypatch):
    monkeypatch.setattr(classifier.llm_client, "call", lambda prompt: "mandate_revoked")
    category, how = classifier.classify(make_case("some brand new gateway message we've never seen"))
    assert category == "mandate_revoked"
    assert how == "llm"


def test_llm_reply_is_stripped_and_lowercased(monkeypatch):
    monkeypatch.setattr(classifier.llm_client, "call", lambda prompt: "  Bank_Downtime\n")
    category, how = classifier.classify(make_case("a totally novel failure string"))
    assert category == "bank_downtime"
    assert how == "llm"


@pytest.mark.parametrize("garbage", ["", "not_a_real_category", "sorry, I cannot classify this", "insufficient_funds and also expired_card"])
def test_llm_garbage_falls_back_to_technical_other(monkeypatch, garbage):
    monkeypatch.setattr(classifier.llm_client, "call", lambda prompt: garbage)
    category, how = classifier.classify(make_case("a totally novel failure string"))
    assert category == "technical_other"
    assert how == "llm_failed"


def test_llm_call_raising_falls_back_to_technical_other(monkeypatch):
    def boom(prompt):
        raise RuntimeError("network is down")

    monkeypatch.setattr(classifier.llm_client, "call", boom)
    category, how = classifier.classify(make_case("a totally novel failure string"))
    assert category == "technical_other"
    assert how == "llm_failed"


def test_llm_failure_logs_degraded_mode(monkeypatch):
    """Any LLM failure — network, provider, extraction, whatever's behind the
    seam — must be logged as degraded mode, with the underlying reason kept
    in the audit trail for a human to diagnose later."""
    def boom(prompt):
        raise RuntimeError("Gemini returned no text; finish_reason=SAFETY.")

    monkeypatch.setattr(classifier.llm_client, "call", boom)

    logged = {}
    monkeypatch.setattr(
        classifier.audit_log, "error",
        lambda case_id, actor, what, exc: logged.update(what=what, exc=exc),
    )

    category, how = classifier.classify(make_case("a totally novel failure string"))
    assert category == "technical_other"
    assert how == "llm_failed"
    assert "degraded mode" in logged["what"]
    assert "SAFETY" in str(logged["exc"])


def test_rule_match_never_calls_the_llm(monkeypatch):
    def fail_if_called(prompt):
        raise AssertionError("LLM must not be called when a rule matches")

    monkeypatch.setattr(classifier.llm_client, "call", fail_if_called)
    category, how = classifier.classify(make_case("Card has expired"))
    assert category == "expired_card"
    assert how == "rule"


# ---------------------------------------------------------------------------
# source == "checkout" — ground truth ahead of any substring rule
# ---------------------------------------------------------------------------

def test_checkout_source_routes_to_checkout_dropoff_regardless_of_reason(monkeypatch):
    """A real abandoned checkout carries no gateway failure reason at all —
    source is ground truth, not the (synthetic-data-only) reason string."""
    def fail_if_called(prompt):
        raise AssertionError("LLM must not be called when source is checkout")

    monkeypatch.setattr(classifier.llm_client, "call", fail_if_called)
    category, how = classifier.classify(
        make_case("this reason string is nonsense and matches no rule", source="checkout")
    )
    assert category == "checkout_dropoff"
    assert how == "rule"


def test_checkout_source_wins_even_over_a_conflicting_reason_rule(monkeypatch):
    """source is checked before the substring rules, so it wins even if the
    reason string happens to look like a different category."""
    category, how = classifier.classify(
        make_case("Card has expired", source="checkout")
    )
    assert category == "checkout_dropoff"
    assert how == "rule"


# ---------------------------------------------------------------------------
# Audit logging — verify DIAGNOSED actually fires on the rule path
# ---------------------------------------------------------------------------

def test_rule_path_logs_diagnosed(monkeypatch):
    calls = []
    monkeypatch.setattr(
        classifier.audit_log, "record",
        lambda *a, **k: calls.append((a, k)),
    )

    category, how = classifier.classify(make_case("Card has expired", id="case_abc123"))

    assert category == "expired_card"
    assert how == "rule"
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "case_abc123"
    assert args[2] == classifier.audit_log.DIAGNOSED
    assert kwargs["decision"] == "expired_card"
    assert kwargs["inp"]["reason_raw"] == "Card has expired"
    assert kwargs["reasoning"]
