"""
Voice inbound (Feature C) must never invent a promise and must never crash on
a provider failure. Every test here mocks the LLM seam -- app.llm.client.call
for text replies, app.llm.client.call_voice for audio replies -- so nothing
touches the network, regardless of which provider is configured in the
environment. See app/voice/inbound.py for the date-resolution and
confidence rules under test.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app import config
from app.voice import inbound

NOW = datetime(2026, 8, 17, 9, 0, 0, tzinfo=timezone.utc)  # a Monday


@pytest.fixture(autouse=True)
def audit_spy(monkeypatch):
    records: list[dict] = []
    errors: list[dict] = []

    def _record(case_id, actor, event_type, **kw):
        records.append({"case_id": case_id, "actor": actor, "event_type": event_type, **kw})

    def _error(case_id, actor, what, exc):
        errors.append({"case_id": case_id, "actor": actor, "what": what, "exc": exc})

    monkeypatch.setattr(inbound.audit_log, "record", _record)
    monkeypatch.setattr(inbound.audit_log, "error", _error)

    class Spy:
        pass

    spy = Spy()
    spy.records = records
    spy.errors = errors
    return spy


def reply_json(intent="promise_to_pay", transcript="kal kar dunga", raw_date_phrase="kal", confidence=0.9, raw_date_phrase_roman=None):
    return json.dumps({
        "transcript": transcript,
        "intent": intent,
        "raw_date_phrase": raw_date_phrase,
        "raw_date_phrase_roman": raw_date_phrase_roman,
        "confidence": confidence,
    })


def mock_text(monkeypatch, raw_reply):
    monkeypatch.setattr(inbound.llm_client, "call", lambda prompt: raw_reply)


def mock_audio(monkeypatch, transcript, raw_reply):
    monkeypatch.setattr(inbound.llm_client, "call_voice", lambda prompt, audio, mime: (transcript, raw_reply))


# ---------------------------------------------------------------------------
# Every intent, via a representative transcript
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("intent,transcript,phrase", [
    ("promise_to_pay", "kal kar dunga", "kal"),
    ("already_paid", "maine already pay kar diya hai", None),
    ("opt_out", "band kar do, mujhe mat bhejo", None),
    ("pay_now", "abhi link bhejo, main pay karta hoon", None),
    ("dispute", "yeh transaction meri nahi hai", None),
])
def test_each_intent_from_a_representative_text_transcript(monkeypatch, intent, transcript, phrase):
    mock_text(monkeypatch, reply_json(intent=intent, transcript=transcript, raw_date_phrase=phrase, confidence=0.95))
    result = inbound.parse_reply(text=transcript, ctx={"case_id": "c1", "now": NOW})
    assert result["intent"] == intent
    assert result["transcript"] == transcript


def test_unclear_intent_from_the_model_passes_through(monkeypatch):
    mock_text(monkeypatch, reply_json(intent="unclear", transcript="hmm kya?", raw_date_phrase=None, confidence=0.9))
    result = inbound.parse_reply(text="hmm kya?", ctx={"now": NOW})
    assert result["intent"] == "unclear"


def test_opt_out_acceptance_example(monkeypatch):
    mock_text(monkeypatch, reply_json(intent="opt_out", transcript="band kar do", raw_date_phrase=None, confidence=0.95))
    result = inbound.parse_reply(text="band kar do", ctx={"now": NOW})
    assert result["intent"] == "opt_out"


# ---------------------------------------------------------------------------
# Deterministic Hinglish date resolution -- every phrase from the spec
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phrase,expected_offset_days", [
    ("kal", 1),
    ("parso", 2),
    ("agle hafte", 7),
    ("is hafte", 3),
    ("5 din mein", 5),
    ("12 din mein", 12),
])
def test_relative_day_phrases_resolve_correctly(phrase, expected_offset_days):
    resolved = inbound._resolve_date_phrase(phrase, NOW)
    assert resolved == (NOW + timedelta(days=expected_offset_days)).date()


@pytest.mark.parametrize("word,expected_offset_days", [
    ("ek", 1), ("do", 2), ("teen", 3), ("char", 4),
    ("paanch", 5), ("panch", 5),
    ("chhah", 6), ("chhe", 6),
    ("saat", 7), ("aath", 8), ("nau", 9), ("das", 10),
])
def test_roman_spelled_out_number_words_resolve_correctly(word, expected_offset_days):
    resolved = inbound._resolve_date_phrase(f"{word} din mein", NOW)
    assert resolved == (NOW + timedelta(days=expected_offset_days)).date()


def test_roman_unknown_number_word_still_downgrades_to_unclear(monkeypatch):
    assert inbound._resolve_date_phrase("gyarah din mein", NOW) is None  # "eleven" -- not in the lookup
    mock_text(monkeypatch, reply_json(
        intent="promise_to_pay", transcript="gyarah din mein kar dunga",
        raw_date_phrase="gyarah din mein", confidence=0.9,
    ))
    result = inbound.parse_reply(text="gyarah din mein kar dunga", ctx={"now": NOW})
    assert result["intent"] == "unclear"
    assert result["promised_date"] is None


def test_agle_mahine_resolves_to_first_of_next_month():
    assert inbound._resolve_date_phrase("agle mahine", NOW) == date(2026, 9, 1)


def test_mahine_ke_end_resolves_to_month_end():
    assert inbound._resolve_date_phrase("mahine ke end tak", NOW) == date(2026, 8, 31)


def test_salary_ke_baad_resolves_to_next_salary_cluster_date():
    # NOW = 17 Aug 2026: month-end (31 Aug) lands sooner than the 1st of next
    # month, so that's the salary-cluster guess -- same heuristic as
    # decision.engine._next_salary_day.
    assert inbound._resolve_date_phrase("salary ke baad", NOW) == date(2026, 8, 31)


def test_salary_aane_do_also_resolves_to_the_salary_cluster_date():
    assert inbound._resolve_date_phrase("salary aane do", NOW) == date(2026, 8, 31)


def test_unresolvable_phrase_returns_none():
    assert inbound._resolve_date_phrase("jab mann karega", NOW) is None


def test_none_phrase_returns_none():
    assert inbound._resolve_date_phrase(None, NOW) is None


def test_unresolvable_date_phrase_downgrades_promise_to_unclear(monkeypatch):
    mock_text(monkeypatch, reply_json(
        intent="promise_to_pay", transcript="jab time milega kar dunga",
        raw_date_phrase="jab time milega", confidence=0.9,
    ))
    result = inbound.parse_reply(text="jab time milega kar dunga", ctx={"now": NOW})
    assert result["intent"] == "unclear"
    assert result["promised_date"] is None


# ---------------------------------------------------------------------------
# Devanagari-script date phrases -- Groq Whisper transcribes Hinglish speech
# as Devanagari far more often than Roman script, so the resolver needs
# native-script patterns too. Same deterministic logic, independent matchers.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phrase,expected_offset_days", [
    ("कल", 1),
    ("परसों", 2),
    ("अगले हफ्ते", 7),   # with halant
    ("अगले हफते", 7),    # without halant -- same spelling, different word
    ("इस हफ्ते", 3),
    ("५ दिन में", 5),     # Devanagari digits
    ("12 दिन में", 12),   # ASCII digits with the Devanagari word
])
def test_devanagari_relative_day_phrases_resolve_correctly(phrase, expected_offset_days):
    resolved = inbound._resolve_date_phrase(phrase, NOW)
    assert resolved == (NOW + timedelta(days=expected_offset_days)).date()


@pytest.mark.parametrize("word,expected_offset_days", [
    ("एक", 1), ("दो", 2), ("तीन", 3), ("चार", 4),
    ("पांच", 5), ("पाँच", 5),
    ("छह", 6), ("छे", 6),
    ("सात", 7), ("आठ", 8), ("नौ", 9), ("दस", 10),
])
def test_devanagari_spelled_out_number_words_resolve_correctly(word, expected_offset_days):
    resolved = inbound._resolve_date_phrase(f"{word} दिन में", NOW)
    assert resolved == (NOW + timedelta(days=expected_offset_days)).date()


def test_devanagari_teen_din_mein_resolves_like_the_digit_form():
    # The exact real-world transcript that motivated this: Groq Whisper wrote
    # out "तीन" (three) rather than the numeral.
    assert inbound._resolve_date_phrase("तीन दिन में", NOW) == inbound._resolve_date_phrase("3 दिन में", NOW)
    assert inbound._resolve_date_phrase("तीन दिन में", NOW) == (NOW + timedelta(days=3)).date()


def test_devanagari_unknown_number_word_still_downgrades_to_unclear(monkeypatch):
    assert inbound._resolve_date_phrase("ग्यारह दिन में", NOW) is None  # "eleven" -- not in the lookup
    mock_text(monkeypatch, reply_json(
        intent="promise_to_pay", transcript="ग्यारह दिन में कर दूँगा",
        raw_date_phrase="ग्यारह दिन में", confidence=0.9,
    ))
    result = inbound.parse_reply(text="ग्यारह दिन में कर दूँगा", ctx={"now": NOW})
    assert result["intent"] == "unclear"
    assert result["promised_date"] is None


def test_devanagari_agle_mahine_resolves_to_first_of_next_month():
    assert inbound._resolve_date_phrase("अगले महीने", NOW) == date(2026, 9, 1)


def test_devanagari_mahine_ke_ant_resolves_to_month_end():
    assert inbound._resolve_date_phrase("महीने के अंत तक", NOW) == date(2026, 8, 31)


def test_devanagari_salary_ke_baad_resolves_to_next_salary_cluster_date():
    # NOW = 17 Aug 2026: month-end (31 Aug) is the salary-cluster guess, same
    # heuristic as decision.engine._next_salary_day.
    assert inbound._resolve_date_phrase("सैलरी के बाद", NOW) == date(2026, 8, 31)


def test_devanagari_halant_and_no_halant_spellings_are_equivalent():
    with_halant = inbound._resolve_date_phrase("अगले हफ्ते", NOW)
    without_halant = inbound._resolve_date_phrase("अगले हफते", NOW)
    assert with_halant == without_halant == (NOW + timedelta(days=7)).date()


def test_devanagari_unresolvable_phrase_returns_none():
    assert inbound._resolve_date_phrase("जब मन करेगा", NOW) is None


def test_devanagari_relative_day_phrase_downgrades_promise_when_unresolvable(monkeypatch):
    mock_text(monkeypatch, reply_json(
        intent="promise_to_pay", transcript="जब मन करेगा कर दूँगा",
        raw_date_phrase="जब मन करेगा", confidence=0.9,
    ))
    result = inbound.parse_reply(text="जब मन करेगा कर दूँगा", ctx={"now": NOW})
    assert result["intent"] == "unclear"
    assert result["promised_date"] is None


def test_devanagari_phrase_resolves_end_to_end_through_parse_reply(monkeypatch):
    mock_text(monkeypatch, reply_json(
        intent="promise_to_pay", transcript="अगले हफ्ते कर दूँगा",
        raw_date_phrase="अगले हफ्ते", confidence=0.95,
    ))
    result = inbound.parse_reply(text="अगले हफ्ते कर दूँगा", ctx={"now": NOW})
    assert result["intent"] == "promise_to_pay"
    assert result["promised_date"] == (NOW + timedelta(days=7)).date().isoformat()


def test_roman_transliteration_resolves_when_the_native_phrase_does_not(monkeypatch):
    # A Devanagari spelling variant (nuqta on फ़) our own patterns don't
    # cover -- the LLM's own Roman transliteration is the fallback path.
    mock_text(monkeypatch, reply_json(
        intent="promise_to_pay", transcript="agle hafte kar dunga",
        raw_date_phrase="अगले हफ़्ते", raw_date_phrase_roman="agle hafte", confidence=0.9,
    ))
    result = inbound.parse_reply(text="agle hafte kar dunga", ctx={"now": NOW})
    assert result["intent"] == "promise_to_pay"
    assert result["promised_date"] == (NOW + timedelta(days=7)).date().isoformat()


def test_both_native_and_roman_phrase_unresolvable_still_downgrades_to_unclear(monkeypatch):
    """The Roman-transliteration fallback must never turn a genuinely
    unresolvable phrase into an invented promise."""
    mock_text(monkeypatch, reply_json(
        intent="promise_to_pay", transcript="jab mann karega",
        raw_date_phrase="जब मन करेगा", raw_date_phrase_roman="jab mann karega", confidence=0.9,
    ))
    result = inbound.parse_reply(text="jab mann karega", ctx={"now": NOW})
    assert result["intent"] == "unclear"
    assert result["promised_date"] is None


# ---------------------------------------------------------------------------
# Horizon cap
# ---------------------------------------------------------------------------

def test_promised_date_is_capped_at_policy_horizon(monkeypatch):
    mock_text(monkeypatch, reply_json(
        intent="promise_to_pay", transcript="agle mahine kar dunga",
        raw_date_phrase="agle mahine", confidence=0.9,
    ))
    policy = {**config.DEFAULT_POLICY, "max_promise_horizon_days": 5}
    result = inbound.parse_reply(text="agle mahine kar dunga", ctx={"now": NOW, "policy": policy})
    assert result["intent"] == "promise_to_pay"
    assert result["promised_date"] == (NOW + timedelta(days=5)).date().isoformat()


def test_promise_within_horizon_is_not_capped(monkeypatch):
    mock_text(monkeypatch, reply_json(intent="promise_to_pay", transcript="kal kar dunga", raw_date_phrase="kal", confidence=0.9))
    result = inbound.parse_reply(text="kal kar dunga", ctx={"now": NOW})
    assert result["promised_date"] == (NOW + timedelta(days=1)).date().isoformat()


# ---------------------------------------------------------------------------
# Never invent a promise: low confidence / empty transcript
# ---------------------------------------------------------------------------

def test_low_confidence_downgrades_to_unclear(monkeypatch):
    mock_text(monkeypatch, reply_json(intent="already_paid", transcript="shayad pay ho gaya", raw_date_phrase=None, confidence=0.3))
    result = inbound.parse_reply(text="shayad pay ho gaya", ctx={"now": NOW})
    assert result["intent"] == "unclear"


def test_audio_reply_with_empty_transcript_downgrades_to_unclear(monkeypatch):
    mock_audio(monkeypatch, "", reply_json(intent="promise_to_pay", transcript="", raw_date_phrase="kal", confidence=0.9))
    result = inbound.parse_reply(audio_bytes=b"some ogg bytes", ctx={"now": NOW})
    assert result["intent"] == "unclear"
    assert result["transcript"] == ""


# ---------------------------------------------------------------------------
# Audio path: transcript comes from the whisper hint when present (Groq),
# else from the model's own JSON (Gemini one-shot)
# ---------------------------------------------------------------------------

def test_audio_reply_uses_the_whisper_transcript_hint_when_present(monkeypatch):
    mock_audio(monkeypatch, "kal kar dunga", reply_json(intent="promise_to_pay", transcript="IGNORED", raw_date_phrase="kal", confidence=0.9))
    result = inbound.parse_reply(audio_bytes=b"...", ctx={"now": NOW})
    assert result["transcript"] == "kal kar dunga"
    assert result["promised_date"] == (NOW + timedelta(days=1)).date().isoformat()


def test_audio_reply_reads_transcript_from_json_when_hint_is_none(monkeypatch):
    mock_audio(monkeypatch, None, reply_json(intent="promise_to_pay", transcript="kal kar dunga", raw_date_phrase="kal", confidence=0.9))
    result = inbound.parse_reply(audio_bytes=b"...", ctx={"now": NOW, "case_id": "c9"})
    assert result["transcript"] == "kal kar dunga"
    assert result["intent"] == "promise_to_pay"


# ---------------------------------------------------------------------------
# Provider failure -> unclear, degraded mode logged
# ---------------------------------------------------------------------------

def test_text_provider_failure_is_unclear_and_logs_degraded_mode(monkeypatch, audit_spy):
    def boom(prompt):
        raise RuntimeError("network exploded")
    monkeypatch.setattr(inbound.llm_client, "call", boom)

    result = inbound.parse_reply(text="kal kar dunga", ctx={"case_id": "c1", "now": NOW})

    assert result["intent"] == "unclear"
    assert len(audit_spy.errors) == 1
    assert audit_spy.errors[0]["case_id"] == "c1"


def test_malformed_json_reply_is_treated_as_a_provider_failure(monkeypatch, audit_spy):
    monkeypatch.setattr(inbound.llm_client, "call", lambda prompt: "not json at all")

    result = inbound.parse_reply(text="kal kar dunga", ctx={"now": NOW})

    assert result["intent"] == "unclear"
    assert len(audit_spy.errors) == 1


def test_audio_provider_failure_is_unclear_and_degraded(monkeypatch, audit_spy):
    def boom(prompt, audio, mime):
        raise RuntimeError("groq down")
    monkeypatch.setattr(inbound.llm_client, "call_voice", boom)

    result = inbound.parse_reply(audio_bytes=b"...", ctx={"now": NOW})

    assert result["intent"] == "unclear"
    assert len(audit_spy.errors) == 1


def test_no_audio_and_no_text_is_unclear_without_calling_the_provider(monkeypatch):
    called = []
    monkeypatch.setattr(inbound.llm_client, "call", lambda prompt: called.append(1))
    monkeypatch.setattr(inbound.llm_client, "call_voice", lambda *a: called.append(1))

    result = inbound.parse_reply(ctx={"now": NOW})

    assert result["intent"] == "unclear"
    assert called == []


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

def test_reply_received_is_logged_with_transcript_and_intent(monkeypatch, audit_spy):
    mock_text(monkeypatch, reply_json(intent="promise_to_pay", transcript="kal kar dunga", raw_date_phrase="kal", confidence=0.9))

    inbound.parse_reply(text="kal kar dunga", ctx={"case_id": "c1", "now": NOW})

    assert len(audit_spy.records) == 1
    rec = audit_spy.records[0]
    assert rec["event_type"] == inbound.audit_log.REPLY_RECEIVED
    assert rec["decision"] == "promise_to_pay"
    assert rec["result"]["transcript"] == "kal kar dunga"


# ---------------------------------------------------------------------------
# MIME type detection
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Debug visibility: a degraded (or any) result must always be explainable
# from the console, not just the audit trail -- ctx={"debug": True}
# ---------------------------------------------------------------------------

def test_debug_off_by_default_prints_nothing(monkeypatch, capsys):
    mock_text(monkeypatch, reply_json())
    inbound.parse_reply(text="kal kar dunga", ctx={"now": NOW})
    assert capsys.readouterr().err == ""


def test_debug_prints_provider_raw_reply_and_reasoning_on_success(monkeypatch, capsys):
    mock_text(monkeypatch, reply_json(intent="promise_to_pay", transcript="kal kar dunga", raw_date_phrase="kal", confidence=0.9))
    inbound.parse_reply(text="kal kar dunga", ctx={"now": NOW, "debug": True})
    err = capsys.readouterr().err
    assert "provider  :" in err
    assert "raw reply :" in err
    assert "kal kar dunga" in err
    assert "Intent: promise_to_pay" in err


def test_debug_prints_the_exception_and_which_provider_on_failure(monkeypatch, capsys):
    def boom(prompt):
        raise RuntimeError("network exploded")
    monkeypatch.setattr(inbound.llm_client, "call", boom)

    result = inbound.parse_reply(text="kal kar dunga", ctx={"now": NOW, "debug": True})

    err = capsys.readouterr().err
    assert result["intent"] == "unclear"
    assert "provider  :" in err
    assert "EXCEPTION : RuntimeError: network exploded" in err
    assert "degraded because:" in err


def test_debug_prints_finish_reason_when_present_in_the_exception(monkeypatch, capsys):
    def boom(prompt, audio, mime):
        raise RuntimeError("Gemini returned no text; finish_reason=SAFETY.")
    monkeypatch.setattr(inbound.llm_client, "call_voice", boom)

    inbound.parse_reply(audio_bytes=b"...", ctx={"now": NOW, "debug": True})

    err = capsys.readouterr().err
    assert "finish_reason: SAFETY" in err


def test_debug_prints_why_a_low_confidence_reply_was_downgraded(monkeypatch, capsys):
    mock_text(monkeypatch, reply_json(intent="already_paid", transcript="shayad", raw_date_phrase=None, confidence=0.2))
    inbound.parse_reply(text="shayad", ctx={"now": NOW, "debug": True})
    err = capsys.readouterr().err
    assert "Downgraded because" in err
    assert "confidence" in err


@pytest.mark.parametrize("path,expected", [
    ("data/voice_samples/promise.ogg", "audio/ogg"),
    ("voice.m4a", "audio/mp4"),
    ("voice.mp3", "audio/mpeg"),
    ("voice.wav", "audio/wav"),
    ("voice.unknownext", "audio/ogg"),
])
def test_mime_type_for_path(path, expected):
    assert inbound.mime_type_for_path(path) == expected
