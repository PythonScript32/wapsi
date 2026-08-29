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

from typing import Any

from app import config


def _call_groq(prompt: str) -> str:
    """Groq's OpenAI-compatible chat endpoint, called over plain httpx so we
    don't need a dedicated SDK dependency for one call."""
    import httpx

    resp = httpx.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
        json={
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 20,
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


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


def _call_gemini(prompt: str) -> str:
    """
    Gemini Flash via the google-genai SDK.

    max_output_tokens is set well above what a short answer needs because
    Gemini 3.x spends part of the budget on internal thinking tokens before it
    ever emits the answer — a tight budget silently returns no text
    (finish_reason=MAX_TOKENS) with nothing to parse.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=config.GEMINI_API_KEY)
    resp = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(max_output_tokens=512, temperature=0),
    )
    return _extract_gemini_text(resp)


def call(prompt: str) -> str:
    """
    The single network seam for this module — Groq if configured, else Gemini
    Flash. Returns the raw reply text, unparsed and unvalidated; the caller
    does its own strict parsing against whatever it's expecting back.
    """
    if config.GROQ_API_KEY:
        return _call_groq(prompt)
    return _call_gemini(prompt)
