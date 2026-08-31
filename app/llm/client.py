"""
LLM client — the one place in the codebase that talks to a runtime LLM.

Runtime LLM policy per AGENTS.md: Groq if GROQ_API_KEY is set (faster,
generous free tier), else Gemini Flash (free tier). Never a paid API.

This module knows nothing about diagnosis, decisions, or voice — it takes a
fully-formed prompt and returns the raw reply text, unparsed and unvalidated.
Parsing that reply against whatever enum or shape the caller needs is the
caller's job (see app/diagnosis/classifier.py for an example). Keeping this
file caller-agnostic is what lets every future LLM call (decision engine,
voice inbound) share one client and one test seam.

SINGLE SEAM
-----------
call(prompt) -> str is the only function callers should use. Tests mock this
one function so nothing in the suite ever touches the network, regardless of
which provider is configured.
"""
from __future__ import annotations

import re
import sys
import time
from typing import Any, Callable, TypeVar

import httpx

from app import config

T = TypeVar("T")

# Every network call gets the same explicit timeout and the same retry
# policy: 1 initial attempt, then up to 3 retries at these delays (4 attempts
# total) -- only on a transport-level failure (dropped connection, timed-out
# read, failed write). An HTTP error status (4xx or 5xx) is a real answer
# from the server, not a blip, so it is never retried here -- raise_for_status
# and the genai SDK turn those into their own exception types that skip
# straight past _with_network_retry's except clause.
_TIMEOUT_S = 30.0
_RETRY_DELAYS_S: tuple[float, ...] = (1, 4, 10)
_RETRYABLE_EXC = (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteError)


def _with_network_retry(fn: Callable[[], T], label: str) -> T:
    """
    Call fn(), retrying only on ConnectError/ReadTimeout/WriteError. Logs
    every retry to stderr so flakiness is visible instead of silent, and
    raises the last exception once every attempt is exhausted so the caller
    (app.voice.inbound) can degrade cleanly rather than hang or crash.
    """
    attempts = 1 + len(_RETRY_DELAYS_S)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            delay = _RETRY_DELAYS_S[attempt - 2]
            print(
                f"[llm_client] {label}: retrying after {type(last_exc).__name__} "
                f"({last_exc}) -- attempt {attempt}/{attempts}, waiting {delay}s",
                file=sys.stderr,
            )
            time.sleep(delay)
        try:
            return fn()
        except _RETRYABLE_EXC as exc:
            last_exc = exc

    print(
        f"[llm_client] {label}: giving up after {attempts} attempts "
        f"({type(last_exc).__name__}: {last_exc})",
        file=sys.stderr,
    )
    raise last_exc


def _is_gemini_quota_exhausted(exc: Exception) -> bool:
    """A 429 RESOURCE_EXHAUSTED is Gemini's free-tier DAILY quota, not a
    transient blip -- retrying the same call again won't help; only a
    different provider or the next day's reset will. Duck-typed on
    google.genai.errors.APIError's code/status attributes rather than an
    isinstance check, so this never needs to import the SDK just to ask the
    question."""
    return getattr(exc, "code", None) == 429 or getattr(exc, "status", None) == "RESOURCE_EXHAUSTED"


_QUOTA_LIMIT_RE = re.compile(r"limit:\s*(\d+)")


def _quota_phrase(exc: Exception) -> str:
    m = _QUOTA_LIMIT_RE.search(str(exc))
    return f"{m.group(1)} req/day" if m else "daily limit"


def _report_gemini_quota_exhausted(exc: Exception, fallback: bool) -> None:
    outcome = "falling back to Groq" if fallback else "no GROQ_API_KEY configured, cannot fall back"
    print(
        f"[llm_client] Gemini free-tier daily quota exhausted ({_quota_phrase(exc)}) — {outcome}.",
        file=sys.stderr,
    )


def _http_status_of(exc: Exception) -> int | None:
    """Extract an HTTP-ish status code from either an httpx.HTTPStatusError
    (Groq) or a google.genai.errors.APIError (Gemini), without importing
    either error type -- duck-typed so one check covers both providers."""
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    return status_code if status_code is not None else getattr(exc, "code", None)


def _is_not_found(exc: Exception) -> bool:
    return _http_status_of(exc) == 404


def _report_model_not_found(exc: Exception, provider: str, model: str, env_var: str) -> None:
    # Third time a hardcoded/retired model id has silently broken this
    # client (gemini-2.5-flash, then a Gemini free-tier quota swap, then
    # llama-3.3-70b-versatile) -- model ids live in config now, and a 404
    # here always says exactly which one to check, never fails quietly.
    print(
        f"[llm_client] {provider} model {model!r} not found (404) -- check {env_var} in .env. {exc}",
        file=sys.stderr,
    )


def _call_groq(prompt: str, max_tokens: int = 20) -> str:
    """Groq's OpenAI-compatible chat endpoint, called over plain httpx so we
    don't need a dedicated SDK dependency for one call.

    max_tokens defaults to 20 (enough for classifier.py's one-word answers);
    callers expecting a JSON reply pass a larger budget explicitly.
    """
    def _do() -> str:
        resp = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
            json={
                "model": config.GROQ_CHAT_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": max_tokens,
            },
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    try:
        return _with_network_retry(_do, "groq chat")
    except Exception as exc:
        if _is_not_found(exc):
            _report_model_not_found(exc, "Groq", config.GROQ_CHAT_MODEL, "GROQ_CHAT_MODEL")
        raise


# Groq's Whisper endpoint identifies audio format from the uploaded filename's
# extension, not the declared MIME type -- map back to something it recognises.
_EXT_BY_MIME = {
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
}


def _call_groq_whisper(audio_bytes: bytes, mime_type: str) -> str:
    """Groq-hosted Whisper transcription (config.GROQ_WHISPER_MODEL)."""
    ext = _EXT_BY_MIME.get(mime_type, "ogg")

    def _do() -> str:
        resp = httpx.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
            files={"file": (f"audio.{ext}", audio_bytes, mime_type)},
            data={"model": config.GROQ_WHISPER_MODEL},
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        return resp.json()["text"]

    try:
        return _with_network_retry(_do, "groq whisper")
    except Exception as exc:
        if _is_not_found(exc):
            _report_model_not_found(exc, "Groq", config.GROQ_WHISPER_MODEL, "GROQ_WHISPER_MODEL")
        raise


def _extract_gemini_text(resp: Any) -> str:
    """
    Pull text out of a GenerateContentResponse without using resp.text.

    resp.text is a convenience accessor that raises when there is no text
    part — which happens on MAX_TOKENS, safety blocks, and recitation blocks.
    Reading candidates[0].content.parts directly lets us report *why* instead
    of surfacing an opaque accessor error, and gives callers the finish_reason
    they need to diagnose a failure.
    """
    candidates = getattr(resp, "candidates", None) or []
    if not candidates:
        raise RuntimeError("Gemini returned no candidates at all.")

    cand = candidates[0]
    content = getattr(cand, "content", None)
    parts = getattr(content, "parts", None) or []
    text = "".join(getattr(p, "text", None) or "" for p in parts).strip()
    if text:
        return text

    finish_reason = getattr(cand, "finish_reason", None)
    raise RuntimeError(f"Gemini returned no text; finish_reason={finish_reason}.")


def _gemini_client():
    from google import genai
    from google.genai import types

    return genai.Client(api_key=config.GEMINI_API_KEY, http_options=types.HttpOptions(timeout=int(_TIMEOUT_S * 1000)))


def _gemini_generate_config():
    from google.genai import types

    # automatic_function_calling=disable silences the SDK's AFC advisory --
    # this client never hands Gemini any tools/functions to call, so the
    # feature is irrelevant here and the warning is just noise.
    return types.GenerateContentConfig(
        max_output_tokens=512,
        temperature=0,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )


def _call_gemini(prompt: str) -> str:
    """
    Gemini Flash via the google-genai SDK.

    max_output_tokens is set well above what a short answer needs because
    Gemini 3.x spends part of the budget on internal thinking tokens before it
    ever emits the answer — a tight budget silently returns no text
    (finish_reason=MAX_TOKENS) with nothing to parse.

    A 429 (daily quota exhausted) is never retried -- see
    _is_gemini_quota_exhausted -- and falls back to Groq instead if
    GROQ_API_KEY is configured, rather than failing the call outright.
    """
    def _do() -> str:
        client = _gemini_client()
        resp = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=prompt,
            config=_gemini_generate_config(),
        )
        return _extract_gemini_text(resp)

    try:
        return _with_network_retry(_do, "gemini text")
    except Exception as exc:
        if _is_gemini_quota_exhausted(exc):
            _report_gemini_quota_exhausted(exc, fallback=bool(config.GROQ_API_KEY))
            if config.GROQ_API_KEY:
                return _call_groq(prompt)
            raise
        if _is_not_found(exc):
            _report_model_not_found(exc, "Gemini", config.GEMINI_MODEL, "GEMINI_MODEL")
        raise


def call(prompt: str) -> str:
    """
    The single network seam for this module — Groq if configured, else Gemini
    Flash. Returns the raw reply text, unparsed and unvalidated; the caller
    does its own strict parsing against whatever it's expecting back.
    """
    if config.GROQ_API_KEY:
        return _call_groq(prompt)
    return _call_gemini(prompt)


def _call_groq_voice(prompt: str, audio_bytes: bytes, mime_type: str) -> tuple[str, str]:
    """The Groq two-step: Whisper transcribes, then the transcript is
    substituted into `prompt` (which must contain a "{transcript}"
    placeholder) for the text classification call."""
    transcript = _call_groq_whisper(audio_bytes, mime_type)
    raw_reply = _call_groq(prompt.format(transcript=transcript), max_tokens=512)
    return transcript, raw_reply


def _call_gemini_audio(prompt: str, audio_bytes: bytes, mime_type: str) -> tuple[str | None, str]:
    """
    Gemini multimodal call: prompt text plus raw audio bytes in one turn.

    Returns (transcript, raw_reply) -- transcript is None on the normal
    Gemini path (the caller reads it out of raw_reply's own JSON, since
    Gemini transcribes and classifies together); it's the real Whisper
    transcript when a 429 quota fallback below engaged instead.

    A 429 (daily quota exhausted) is never retried and falls back to Groq
    (transcribe + classify) instead if GROQ_API_KEY is configured, rather
    than failing the call outright.
    """
    from google.genai import types

    def _do() -> str:
        client = _gemini_client()
        resp = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=[prompt, types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)],
            config=_gemini_generate_config(),
        )
        return _extract_gemini_text(resp)

    try:
        return None, _with_network_retry(_do, "gemini audio")
    except Exception as exc:
        if _is_gemini_quota_exhausted(exc):
            _report_gemini_quota_exhausted(exc, fallback=bool(config.GROQ_API_KEY))
            if config.GROQ_API_KEY:
                return _call_groq_voice(prompt, audio_bytes, mime_type)
            raise
        if _is_not_found(exc):
            _report_model_not_found(exc, "Gemini", config.GEMINI_MODEL, "GEMINI_MODEL")
        raise


def call_voice(prompt: str, audio_bytes: bytes, mime_type: str) -> tuple[str | None, str]:
    """
    The single network seam for voice-reply understanding (app.voice.inbound).
    Groq if configured, else Gemini — mirrors call()'s provider policy, but
    audio needs its own path since call() is text-only.

    Returns (transcript, raw_reply):
      transcript — ASR ground truth when a dedicated transcription step ran
                   (Groq Whisper); None when transcript and intent came back
                   together from one multimodal call (Gemini), in which case
                   the caller reads "transcript" out of raw_reply's JSON.
      raw_reply  — the provider's raw reply to `prompt`, unparsed and
                   unvalidated; the caller does its own strict JSON parsing.

    Groq path: `prompt` MUST contain a "{transcript}" placeholder. Whisper
    transcribes the audio first — that transcript is the real one, never
    re-guessed by a second call — then it's substituted into `prompt` and
    sent to the same chat model call() uses for text, asking it to classify
    intent/date-phrase/confidence from that text.

    Gemini path: `prompt` and the raw audio bytes are sent together in one
    multimodal call; Gemini transcribes and classifies in a single pass, so
    `prompt` should ask it to return the transcript itself as part of its
    JSON reply.
    """
    if config.GROQ_API_KEY:
        return _call_groq_voice(prompt, audio_bytes, mime_type)
    return _call_gemini_audio(prompt, audio_bytes, mime_type)
