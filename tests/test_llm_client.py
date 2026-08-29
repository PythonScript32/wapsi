"""
The LLM client — app/llm/client.py — is the one place in the codebase that
talks to a runtime LLM. Every caller (diagnosis today, decision/voice later)
mocks call() at this single seam; these tests cover the seam itself: which
provider it dispatches to, and how it turns a raw Gemini response into text.

No test here touches the network.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.llm import client


# ---------------------------------------------------------------------------
# call() dispatch — Groq when configured, else Gemini
# ---------------------------------------------------------------------------

def test_call_uses_groq_when_key_is_set(monkeypatch):
    monkeypatch.setattr(client.config, "GROQ_API_KEY", "test-groq-key")
    monkeypatch.setattr(client, "_call_groq", lambda prompt: "groq said hi")
    monkeypatch.setattr(client, "_call_gemini", lambda prompt: (_ for _ in ()).throw(
        AssertionError("Gemini must not be called when Groq is configured")
    ))
    assert client.call("hello") == "groq said hi"


def test_call_uses_gemini_when_no_groq_key(monkeypatch):
    monkeypatch.setattr(client.config, "GROQ_API_KEY", "")
    monkeypatch.setattr(client, "_call_gemini", lambda prompt: "gemini said hi")
    monkeypatch.setattr(client, "_call_groq", lambda prompt: (_ for _ in ()).throw(
        AssertionError("Groq must not be called when no key is configured")
    ))
    assert client.call("hello") == "gemini said hi"


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
    assert client._extract_gemini_text(resp) == "mandate_revoked"


def test_extract_gemini_text_raises_with_finish_reason_when_no_text():
    resp = _fake_response(parts_text=[], finish_reason="MAX_TOKENS")
    with pytest.raises(RuntimeError, match="MAX_TOKENS"):
        client._extract_gemini_text(resp)


def test_extract_gemini_text_raises_when_no_candidates():
    resp = _fake_response(candidates=False)
    with pytest.raises(RuntimeError, match="no candidates"):
        client._extract_gemini_text(resp)


def test_call_gemini_surfaces_extraction_failure(monkeypatch):
    """_call_gemini must propagate the extraction error (finish_reason and
    all) rather than swallowing it — callers rely on that detail to log a
    meaningful degraded-mode reason."""
    class FakeModels:
        def generate_content(self, model, contents, config):
            return _fake_response(parts_text=[], finish_reason="SAFETY")

    class FakeClient:
        def __init__(self, api_key):
            self.models = FakeModels()

    monkeypatch.setattr(client.config, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(client.config, "GEMINI_MODEL", "gemini-flash-latest")

    import google.genai as real_genai
    monkeypatch.setattr(real_genai, "Client", FakeClient)

    with pytest.raises(RuntimeError, match="SAFETY"):
        client._call_gemini("some prompt")
