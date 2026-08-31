"""
The LLM client — app/llm/client.py — is the one place in the codebase that
talks to a runtime LLM. Every caller (diagnosis today, decision/voice later)
mocks call() at this single seam; these tests cover the seam itself: which
provider it dispatches to, and how it turns a raw Gemini response into text.

No test here touches the network.
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.llm import client


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    """Every retry test below fires all 3 backoff delays -- don't actually
    wait 1s + 4s + 10s per test."""
    monkeypatch.setattr(client.time, "sleep", lambda seconds: None)


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
# call_voice() dispatch — same provider policy as call(), but audio needs its
# own path since call() is text-only (app.voice.inbound is the caller).
# ---------------------------------------------------------------------------

def test_call_voice_uses_groq_whisper_then_groq_text_when_key_is_set(monkeypatch):
    monkeypatch.setattr(client.config, "GROQ_API_KEY", "test-groq-key")
    monkeypatch.setattr(client, "_call_groq_whisper", lambda audio, mime: "kal kar dunga")

    captured = {}
    def fake_call_groq(prompt, max_tokens=20):
        captured["prompt"] = prompt
        captured["max_tokens"] = max_tokens
        return '{"intent": "promise_to_pay"}'
    monkeypatch.setattr(client, "_call_groq", fake_call_groq)
    monkeypatch.setattr(client, "_call_gemini_audio", lambda prompt, audio, mime: (_ for _ in ()).throw(
        AssertionError("Gemini must not be called when Groq is configured")
    ))

    transcript, raw_reply = client.call_voice("Customer said: {transcript}", b"audio-bytes", "audio/ogg")

    assert transcript == "kal kar dunga"
    assert raw_reply == '{"intent": "promise_to_pay"}'
    assert captured["prompt"] == "Customer said: kal kar dunga"
    assert captured["max_tokens"] == 512


def test_call_voice_uses_gemini_when_no_groq_key(monkeypatch):
    monkeypatch.setattr(client.config, "GROQ_API_KEY", "")
    monkeypatch.setattr(client, "_call_gemini_audio", lambda prompt, audio, mime: (None, "gemini said hi"))
    monkeypatch.setattr(client, "_call_groq_whisper", lambda audio, mime: (_ for _ in ()).throw(
        AssertionError("Groq must not be called when no key is configured")
    ))

    transcript, raw_reply = client.call_voice("prompt text", b"audio-bytes", "audio/ogg")

    assert transcript is None
    assert raw_reply == "gemini said hi"


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


# ---------------------------------------------------------------------------
# _with_network_retry -- the shared resilience layer under every call
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exc_type", [httpx.ConnectError, httpx.ReadTimeout, httpx.WriteError])
def test_with_network_retry_retries_transient_transport_errors_and_then_succeeds(exc_type):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise exc_type("transient")
        return "ok"

    assert client._with_network_retry(flaky, "test") == "ok"
    assert len(calls) == 3


def test_with_network_retry_gives_up_after_exhausting_all_retries():
    calls = []

    def always_fails():
        calls.append(1)
        raise httpx.ConnectError("still down")

    with pytest.raises(httpx.ConnectError, match="still down"):
        client._with_network_retry(always_fails, "test")

    assert len(calls) == 4  # 1 initial attempt + 3 retries


def test_with_network_retry_sleeps_the_specified_backoff_between_attempts(monkeypatch):
    delays = []
    monkeypatch.setattr(client.time, "sleep", lambda seconds: delays.append(seconds))

    def always_fails():
        raise httpx.ReadTimeout("slow")

    with pytest.raises(httpx.ReadTimeout):
        client._with_network_retry(always_fails, "test")

    assert delays == [1, 4, 10]


def test_with_network_retry_does_not_retry_an_http_status_error():
    """A 4xx/5xx is a real answer from the server -- raise_for_status() raises
    HTTPStatusError, which is not in the retryable set, so it must fail on
    the very first attempt."""
    calls = []

    def bad_status():
        calls.append(1)
        request = httpx.Request("POST", "https://api.groq.com/x")
        response = httpx.Response(400, request=request)
        raise httpx.HTTPStatusError("bad request", request=request, response=response)

    with pytest.raises(httpx.HTTPStatusError):
        client._with_network_retry(bad_status, "test")

    assert len(calls) == 1


def test_with_network_retry_logs_each_retry_and_the_final_giveup_to_stderr(capsys):
    def always_fails():
        raise httpx.ConnectError("down")

    with pytest.raises(httpx.ConnectError):
        client._with_network_retry(always_fails, "groq chat")

    err = capsys.readouterr().err
    assert err.count("retrying after ConnectError") == 3
    assert "giving up after 4 attempts" in err
    assert "groq chat" in err


# ---------------------------------------------------------------------------
# Explicit 30s timeout on every httpx call
# ---------------------------------------------------------------------------

def test_call_groq_uses_the_30s_timeout(monkeypatch):
    monkeypatch.setattr(client.config, "GROQ_API_KEY", "test-key")
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        request = httpx.Request("POST", url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]}, request=request)

    monkeypatch.setattr(httpx, "post", fake_post)
    client._call_groq("hello")
    assert captured["timeout"] == 30.0


def test_call_groq_sends_the_configured_chat_model_not_a_hardcoded_one(monkeypatch):
    monkeypatch.setattr(client.config, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(client.config, "GROQ_CHAT_MODEL", "some/other-model")
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        request = httpx.Request("POST", url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]}, request=request)

    monkeypatch.setattr(httpx, "post", fake_post)
    client._call_groq("hello")
    assert captured["json"]["model"] == "some/other-model"


def test_call_groq_whisper_sends_the_configured_whisper_model(monkeypatch):
    monkeypatch.setattr(client.config, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(client.config, "GROQ_WHISPER_MODEL", "some/other-whisper")
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        request = httpx.Request("POST", url)
        return httpx.Response(200, json={"text": "hi"}, request=request)

    monkeypatch.setattr(httpx, "post", fake_post)
    client._call_groq_whisper(b"audio", "audio/ogg")
    assert captured["data"]["model"] == "some/other-whisper"


def test_call_groq_whisper_uses_the_30s_timeout(monkeypatch):
    monkeypatch.setattr(client.config, "GROQ_API_KEY", "test-key")
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        request = httpx.Request("POST", url)
        return httpx.Response(200, json={"text": "hi"}, request=request)

    monkeypatch.setattr(httpx, "post", fake_post)
    client._call_groq_whisper(b"audio", "audio/ogg")
    assert captured["timeout"] == 30.0


def test_gemini_client_passes_a_30s_http_options_timeout(monkeypatch):
    monkeypatch.setattr(client.config, "GEMINI_API_KEY", "fake-key")
    captured = {}

    class FakeClient:
        def __init__(self, api_key, http_options=None):
            captured["http_options"] = http_options

    import google.genai as real_genai
    monkeypatch.setattr(real_genai, "Client", FakeClient)

    client._gemini_client()
    assert captured["http_options"].timeout == 30000


def test_gemini_generate_config_disables_automatic_function_calling():
    cfg = client._gemini_generate_config()
    assert cfg.automatic_function_calling.disable is True


# ---------------------------------------------------------------------------
# Gemini 429 (daily quota exhausted) -- never retried, always a clear stderr
# message, and an automatic fallback to Groq when GROQ_API_KEY is configured.
# ---------------------------------------------------------------------------

class _FakeQuotaError(Exception):
    """Duck-types google.genai.errors.ClientError's code/status attributes
    without needing the real SDK exception (whose constructor takes an
    HTTP response object)."""
    def __init__(self, message, code=429, status="RESOURCE_EXHAUSTED"):
        super().__init__(message)
        self.code = code
        self.status = status


_QUOTA_MESSAGE = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'Quota exceeded for metric: "
    "generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, "
    "model: gemini-3.7-flash', 'status': 'RESOURCE_EXHAUSTED'}}"
)


def test_is_gemini_quota_exhausted_recognises_the_real_error_shape():
    assert client._is_gemini_quota_exhausted(_FakeQuotaError(_QUOTA_MESSAGE)) is True


def test_is_gemini_quota_exhausted_is_false_for_an_unrelated_error():
    assert client._is_gemini_quota_exhausted(RuntimeError("something else entirely")) is False


def test_quota_phrase_extracts_the_daily_limit_from_the_error_message():
    assert client._quota_phrase(_FakeQuotaError(_QUOTA_MESSAGE)) == "20 req/day"


def test_quota_phrase_falls_back_when_no_limit_is_present():
    assert client._quota_phrase(_FakeQuotaError("429 RESOURCE_EXHAUSTED, no details")) == "daily limit"


def test_call_gemini_does_not_retry_a_429_and_raises_when_no_groq_key(monkeypatch):
    monkeypatch.setattr(client.config, "GROQ_API_KEY", "")
    calls = []

    class FakeModels:
        def generate_content(self, model, contents, config):
            calls.append(1)
            raise _FakeQuotaError(_QUOTA_MESSAGE)

    class FakeClient:
        def __init__(self, api_key, http_options=None):
            self.models = FakeModels()

    monkeypatch.setattr(client, "_gemini_client", lambda: FakeClient(api_key="fake-key"))

    with pytest.raises(_FakeQuotaError):
        client._call_gemini("some prompt")

    assert len(calls) == 1  # never retried


def test_call_gemini_429_falls_back_to_groq_when_groq_key_is_set(monkeypatch, capsys):
    monkeypatch.setattr(client.config, "GROQ_API_KEY", "test-groq-key")
    monkeypatch.setattr(client, "_gemini_client", lambda: (_ for _ in ()).throw(_FakeQuotaError(_QUOTA_MESSAGE)))
    monkeypatch.setattr(client, "_call_groq", lambda prompt, max_tokens=20: "groq took over")

    result = client._call_gemini("some prompt")

    assert result == "groq took over"
    err = capsys.readouterr().err
    assert "Gemini free-tier daily quota exhausted (20 req/day)" in err
    assert "falling back to Groq" in err


def test_call_gemini_429_raises_when_no_groq_key_and_reports_no_fallback(monkeypatch, capsys):
    monkeypatch.setattr(client.config, "GROQ_API_KEY", "")
    monkeypatch.setattr(client, "_gemini_client", lambda: (_ for _ in ()).throw(_FakeQuotaError(_QUOTA_MESSAGE)))

    with pytest.raises(_FakeQuotaError):
        client._call_gemini("some prompt")

    err = capsys.readouterr().err
    assert "Gemini free-tier daily quota exhausted (20 req/day)" in err
    assert "no GROQ_API_KEY configured" in err


def test_call_gemini_audio_429_falls_back_to_groq_voice_when_groq_key_is_set(monkeypatch, capsys):
    monkeypatch.setattr(client.config, "GROQ_API_KEY", "test-groq-key")
    monkeypatch.setattr(client, "_gemini_client", lambda: (_ for _ in ()).throw(_FakeQuotaError(_QUOTA_MESSAGE)))
    monkeypatch.setattr(client, "_call_groq_voice", lambda prompt, audio, mime: ("kal kar dunga", '{"intent": "promise_to_pay"}'))

    transcript, raw_reply = client._call_gemini_audio("prompt with {transcript}", b"audio-bytes", "audio/ogg")

    assert transcript == "kal kar dunga"
    assert raw_reply == '{"intent": "promise_to_pay"}'
    assert "falling back to Groq" in capsys.readouterr().err


def test_call_gemini_audio_429_raises_when_no_groq_key(monkeypatch):
    monkeypatch.setattr(client.config, "GROQ_API_KEY", "")
    monkeypatch.setattr(client, "_gemini_client", lambda: (_ for _ in ()).throw(_FakeQuotaError(_QUOTA_MESSAGE)))

    with pytest.raises(_FakeQuotaError):
        client._call_gemini_audio("prompt", b"audio-bytes", "audio/ogg")


def test_a_non_quota_gemini_error_is_never_treated_as_a_fallback_trigger(monkeypatch):
    """A different 4xx (e.g. an invalid API key, 400) must propagate exactly
    as before -- the fallback is specific to quota exhaustion, not any
    Gemini failure."""
    monkeypatch.setattr(client.config, "GROQ_API_KEY", "test-groq-key")
    monkeypatch.setattr(client, "_gemini_client", lambda: (_ for _ in ()).throw(RuntimeError("400 INVALID_ARGUMENT")))
    monkeypatch.setattr(client, "_call_groq", lambda prompt, max_tokens=20: (_ for _ in ()).throw(
        AssertionError("Groq must not be called for a non-quota error")
    ))

    with pytest.raises(RuntimeError, match="INVALID_ARGUMENT"):
        client._call_gemini("some prompt")


# ---------------------------------------------------------------------------
# 404 model-not-found -- must never fail silently, for either provider.
# We've been bitten by a retired model id three times now (gemini-2.5-flash,
# a Gemini quota swap, llama-3.3-70b-versatile); model ids live in config,
# and a 404 always names the provider, the model, and which env var to check.
# ---------------------------------------------------------------------------

def test_is_not_found_recognises_an_httpx_404(monkeypatch):
    request = httpx.Request("POST", "https://api.groq.com/x")
    response = httpx.Response(404, request=request)
    exc = httpx.HTTPStatusError("not found", request=request, response=response)
    assert client._is_not_found(exc) is True


def test_is_not_found_recognises_a_gemini_style_code_attribute():
    assert client._is_not_found(_FakeQuotaError("404 NOT_FOUND", code=404, status="NOT_FOUND")) is True


def test_is_not_found_is_false_for_other_statuses():
    request = httpx.Request("POST", "https://api.groq.com/x")
    response = httpx.Response(500, request=request)
    exc = httpx.HTTPStatusError("server error", request=request, response=response)
    assert client._is_not_found(exc) is False


def test_call_groq_404_prints_provider_model_and_env_var_then_raises(monkeypatch, capsys):
    monkeypatch.setattr(client.config, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(client.config, "GROQ_CHAT_MODEL", "retired/model")

    def fake_post(url, **kwargs):
        request = httpx.Request("POST", url)
        return httpx.Response(404, text="model not found", request=request)

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(httpx.HTTPStatusError):
        client._call_groq("hello")

    err = capsys.readouterr().err
    assert "Groq model 'retired/model' not found (404)" in err
    assert "GROQ_CHAT_MODEL" in err


def test_call_groq_whisper_404_prints_provider_model_and_env_var_then_raises(monkeypatch, capsys):
    monkeypatch.setattr(client.config, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(client.config, "GROQ_WHISPER_MODEL", "retired/whisper")

    def fake_post(url, **kwargs):
        request = httpx.Request("POST", url)
        return httpx.Response(404, text="model not found", request=request)

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(httpx.HTTPStatusError):
        client._call_groq_whisper(b"audio", "audio/ogg")

    err = capsys.readouterr().err
    assert "Groq model 'retired/whisper' not found (404)" in err
    assert "GROQ_WHISPER_MODEL" in err


def test_call_gemini_404_prints_provider_model_and_env_var_then_raises(monkeypatch, capsys):
    monkeypatch.setattr(client.config, "GROQ_API_KEY", "")
    monkeypatch.setattr(client.config, "GEMINI_MODEL", "retired-gemini-model")
    monkeypatch.setattr(client, "_gemini_client", lambda: (_ for _ in ()).throw(
        _FakeQuotaError("404 NOT_FOUND", code=404, status="NOT_FOUND")
    ))

    with pytest.raises(_FakeQuotaError):
        client._call_gemini("some prompt")

    err = capsys.readouterr().err
    assert "Gemini model 'retired-gemini-model' not found (404)" in err
    assert "GEMINI_MODEL" in err


def test_call_gemini_audio_404_prints_provider_model_and_env_var_then_raises(monkeypatch, capsys):
    monkeypatch.setattr(client.config, "GROQ_API_KEY", "")
    monkeypatch.setattr(client.config, "GEMINI_MODEL", "retired-gemini-model")
    monkeypatch.setattr(client, "_gemini_client", lambda: (_ for _ in ()).throw(
        _FakeQuotaError("404 NOT_FOUND", code=404, status="NOT_FOUND")
    ))

    with pytest.raises(_FakeQuotaError):
        client._call_gemini_audio("prompt", b"audio-bytes", "audio/ogg")

    err = capsys.readouterr().err
    assert "Gemini model 'retired-gemini-model' not found (404)" in err


def test_call_gemini_surfaces_extraction_failure(monkeypatch):
    """_call_gemini must propagate the extraction error (finish_reason and
    all) rather than swallowing it — callers rely on that detail to log a
    meaningful degraded-mode reason."""
    class FakeModels:
        def generate_content(self, model, contents, config):
            return _fake_response(parts_text=[], finish_reason="SAFETY")

    class FakeClient:
        def __init__(self, api_key, http_options=None):
            self.models = FakeModels()

    monkeypatch.setattr(client.config, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(client.config, "GEMINI_MODEL", "gemini-flash-latest")

    import google.genai as real_genai
    monkeypatch.setattr(real_genai, "Client", FakeClient)

    with pytest.raises(RuntimeError, match="SAFETY"):
        client._call_gemini("some prompt")
