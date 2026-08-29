"""
Diagnosis must map raw gateway strings to the right category, deterministically.

Rules run first and are ground truth. The LLM is only a fallback for reasons no
rule recognises, its answer is constrained to the enum, and it can never
override an unambiguous rule match. No test here touches the network — the LLM
call is mocked at its single seam, classifier._call_llm.
"""
from __future__ import annotations

from types import SimpleNamespace

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
    monkeypatch.setattr(classifier, "_call_llm", lambda reason_raw: "mandate_revoked")
    category, how = classifier.classify(make_case("some brand new gateway message we've never seen"))
    assert category == "mandate_revoked"
    assert how == "llm"


def test_llm_reply_is_stripped_and_lowercased(monkeypatch):
    monkeypatch.setattr(classifier, "_call_llm", lambda reason_raw: "  Bank_Downtime\n")
    category, how = classifier.classify(make_case("a totally novel failure string"))
    assert category == "bank_downtime"
    assert how == "llm"


@pytest.mark.parametrize("garbage", ["", "not_a_real_category", "sorry, I cannot classify this", "insufficient_funds and also expired_card"])
def test_llm_garbage_falls_back_to_technical_other(monkeypatch, garbage):
    monkeypatch.setattr(classifier, "_call_llm", lambda reason_raw: garbage)
    category, how = classifier.classify(make_case("a totally novel failure string"))
    assert category == "technical_other"
    assert how == "llm_failed"


def test_llm_call_raising_falls_back_to_technical_other(monkeypatch):
    def boom(reason_raw):
        raise RuntimeError("network is down")

    monkeypatch.setattr(classifier, "_call_llm", boom)
    category, how = classifier.classify(make_case("a totally novel failure string"))
    assert category == "technical_other"
    assert how == "llm_failed"


def test_rule_match_never_calls_the_llm(monkeypatch):
    def fail_if_called(reason_raw):
        raise AssertionError("LLM must not be called when a rule matches")

    monkeypatch.setattr(classifier, "_call_llm", fail_if_called)
    category, how = classifier.classify(make_case("Card has expired"))
    assert category == "expired_card"
    assert how == "rule"


# ---------------------------------------------------------------------------
# Gemini text extraction — reads candidates[0].content.parts, not resp.text,
# and reports finish_reason when there is nothing to extract. No SDK objects
# involved: SimpleNamespace duck-types the parts of the response we touch.
# ---------------------------------------------------------------------------

def _fake_response(parts_text=("mandate_revoked",), finish_reason=None, candidates=True):
    if not candidates:
        return SimpleNamespace(candidates=[])
    parts = [SimpleNamespace(text=t) for t in parts_text]
    return SimpleNamespace(candidates=[
        SimpleNamespace(content=SimpleNamespace(parts=parts), finish_reason=finish_reason)
    ])


def test_extract_gemini_text_joins_parts():
    resp = _fake_response(parts_text=["mandate", "_revoked"])
    assert classifier._extract_gemini_text(resp) == "mandate_revoked"


def test_extract_gemini_text_raises_with_finish_reason_when_no_text():
    resp = _fake_response(parts_text=[], finish_reason="MAX_TOKENS")
    with pytest.raises(RuntimeError, match="MAX_TOKENS"):
        classifier._extract_gemini_text(resp)


def test_extract_gemini_text_raises_when_no_candidates():
    resp = _fake_response(candidates=False)
    with pytest.raises(RuntimeError, match="no candidates"):
        classifier._extract_gemini_text(resp)


def test_gemini_extraction_failure_falls_back_to_technical_other_and_logs_degraded_mode(monkeypatch):
    """The Gemini path (mocked at _call_gemini, not _call_llm) must, on any
    extraction failure, fall back to technical_other and record the degraded
    mode via audit_log.error — never crash the classifier."""
    monkeypatch.setattr(classifier.config, "GROQ_API_KEY", "")

    logged = {}

    def fake_error(case_id, actor, what, exc):
        logged["what"] = what
        logged["exc"] = exc

    monkeypatch.setattr(classifier.audit_log, "error", fake_error)

    def boom(reason_raw):
        resp = _fake_response(parts_text=[], finish_reason="SAFETY")
        return classifier._extract_gemini_text(resp)

    monkeypatch.setattr(classifier, "_call_gemini", boom)

    category, how = classifier.classify(make_case("a totally novel failure string"))
    assert category == "technical_other"
    assert how == "llm_failed"
    assert "degraded mode" in logged["what"]
    assert "SAFETY" in str(logged["exc"])


# ---------------------------------------------------------------------------
# source == "checkout" — ground truth ahead of any substring rule
# ---------------------------------------------------------------------------

def test_checkout_source_routes_to_checkout_dropoff_regardless_of_reason(monkeypatch):
    """A real abandoned checkout carries no gateway failure reason at all —
    source is ground truth, not the (synthetic-data-only) reason string."""
    def fail_if_called(reason_raw):
        raise AssertionError("LLM must not be called when source is checkout")

    monkeypatch.setattr(classifier, "_call_llm", fail_if_called)
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


