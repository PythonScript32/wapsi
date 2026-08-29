"""
Diagnosis: map a raw gateway failure reason (or abandonment context) to one
canonical category.

Categories:
  insufficient_funds | expired_card | mandate_revoked | bank_downtime |
  technical_other | checkout_dropoff

GOLDEN RULE: the Razorpay reason code/description is ground truth. Rule-based
mapping runs FIRST. The LLM is only consulted when the rules cannot classify,
and its answer is constrained to the enum above. The LLM must never invent a
reason that contradicts the code, and can never override an unambiguous rule
match — the rule branch returns before the LLM is ever called.

Always log: raw reason in, category out, and which path decided (rule vs llm vs
llm_failed — a degraded-mode fallback, not a real LLM classification).

Never reads case["latent"] — diagnosis decides blind, exactly as it would in
production. Only app/detection/batch_scanner.py may read that field.
"""
from __future__ import annotations

from typing import Any

from app import config
from app.audit import log as audit_log

CATEGORIES = (
    "insufficient_funds",
    "expired_card",
    "mandate_revoked",
    "bank_downtime",
    "technical_other",
    "checkout_dropoff",
)
_CATEGORY_SET = set(CATEGORIES)

# Substrings, matched case-insensitively against case["reason_raw"], that
# unambiguously identify a category. Keep these specific — a substring that
# could plausibly appear in more than one category's real-world message is a
# bug waiting to misdiagnose a case.
RULES: dict[str, str] = {
    "insufficient": "insufficient_funds",

    "expired": "expired_card",
    "no longer valid": "expired_card",

    "revoked": "mandate_revoked",
    "mandate_cancelled": "mandate_revoked",
    "no longer active": "mandate_revoked",

    "unavailable": "bank_downtime",
    "bank down": "bank_downtime",
    "gateway_error": "bank_downtime",
    "bank end": "bank_downtime",

    "technical error": "technical_other",
    "server_error": "technical_other",
    "could not be completed": "technical_other",

    "not paid within window": "checkout_dropoff",
}

_LLM_PROMPT = """Classify this payment/checkout failure reason into exactly one category.

Valid categories (respond with exactly one of these words, nothing else):
insufficient_funds
expired_card
mandate_revoked
bank_downtime
technical_other
checkout_dropoff

Reason: "{reason}"

Respond with only the category word."""


def _match_rule(reason_lower: str) -> tuple[str, str] | None:
    """First substring hit wins. Returns (category, matched substring)."""
    for substring, category in RULES.items():
        if substring in reason_lower:
            return category, substring
    return None


def _call_groq(reason_raw: str) -> str:
    """Groq's OpenAI-compatible chat endpoint, called over plain httpx so we
    don't need a dedicated SDK dependency for one call."""
    import httpx

    resp = httpx.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
        json={
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": _LLM_PROMPT.format(reason=reason_raw)}],
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


def _call_gemini(reason_raw: str) -> str:
    """
    Gemini Flash via the google-genai SDK.

    max_output_tokens is set well above what the answer needs (a single word)
    because Gemini 3.x spends part of the budget on internal thinking tokens
    before it ever emits the answer — a tight budget silently returns no text
    (finish_reason=MAX_TOKENS) with nothing to parse.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=config.GEMINI_API_KEY)
    resp = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=_LLM_PROMPT.format(reason=reason_raw),
        config=types.GenerateContentConfig(max_output_tokens=512, temperature=0),
    )
    return _extract_gemini_text(resp)


def _call_llm(reason_raw: str) -> str:
    """
    The single network seam for this module — Groq if configured (faster,
    generous free tier), else Gemini Flash. Returns the raw reply text,
    unparsed and unvalidated; classify() does the strict parsing.

    Tests mock this function directly so no test ever touches the network.
    """
    if config.GROQ_API_KEY:
        return _call_groq(reason_raw)
    return _call_gemini(reason_raw)


def classify(case: dict) -> tuple[str, str]:
    """
    Map case["reason_raw"] to one of the six canonical categories.

    Returns (category, how):
      "rule"       — a deterministic signal decided it (source, or a reason
                     substring). Ground truth; the LLM was never consulted.
      "llm"        — no rule matched, and the LLM classified it successfully.
      "llm_failed" — no rule matched, and the LLM call failed or returned
                     something outside the enum. Falls back to
                     technical_other; distinguish this from "llm" in metrics
                     so degraded-mode fallbacks don't look like real LLM use.
    """
    case_id = case.get("id")
    reason_raw = str(case.get("reason_raw") or "")
    reason_lower = reason_raw.lower()

    # A real abandoned checkout has no gateway failure reason at all — source
    # is the ground-truth signal, the reason string is a synthetic-data
    # artifact. Check this before the substring rules, which stay as a
    # fallback for any checkout case that somehow lacks this field.
    if case.get("source") == "checkout":
        category = "checkout_dropoff"
        audit_log.record(
            case_id,
            "diagnosis.classifier",
            audit_log.DIAGNOSED,
            inp={"reason_raw": reason_raw, "source": "checkout"},
            decision=category,
            reasoning=(
                "Case source is checkout. An abandoned checkout has no gateway "
                "failure reason, so source is the ground-truth signal here, not "
                "the reason string. Matched by rule; the LLM was not consulted."
            ),
        )
        return category, "rule"

    matched = _match_rule(reason_lower)
    if matched is not None:
        category, substring = matched
        audit_log.record(
            case_id,
            "diagnosis.classifier",
            audit_log.DIAGNOSED,
            inp={"reason_raw": reason_raw},
            decision=category,
            reasoning=(
                f'Raw reason contains "{substring}", a known indicator of {category}. '
                "Matched by rule; the LLM was not consulted."
            ),
        )
        return category, "rule"

    provider = "groq:llama-3.3-70b-versatile" if config.GROQ_API_KEY else f"gemini:{config.GEMINI_MODEL}"
    actor = f"diagnosis.classifier:{provider}"

    try:
        raw_reply = _call_llm(reason_raw)
    except Exception as exc:
        audit_log.error(case_id, actor, "LLM classification call failed — running in degraded mode", exc)
        category = "technical_other"
        audit_log.record(
            case_id,
            actor,
            audit_log.DIAGNOSED,
            inp={"reason_raw": reason_raw},
            decision=category,
            reasoning=(
                "No rule matched the raw reason, and the LLM call itself failed "
                f"({exc}). Running in degraded mode: defaulting to technical_other "
                "to fail safe rather than guessing."
            ),
        )
        return category, "llm_failed"

    cleaned = (raw_reply or "").strip().lower()
    if cleaned in _CATEGORY_SET:
        category = cleaned
        how = "llm"
        reasoning = (
            f'No rule matched the raw reason "{reason_raw}". The LLM classified it as '
            f"{category}."
        )
    else:
        category = "technical_other"
        how = "llm_failed"
        reasoning = (
            f"No rule matched the raw reason, and the LLM's reply {raw_reply!r} was not "
            "one of the six valid categories. Running in degraded mode: defaulting to "
            "technical_other to fail safe."
        )

    audit_log.record(
        case_id,
        actor,
        audit_log.DIAGNOSED,
        inp={"reason_raw": reason_raw},
        decision=category,
        reasoning=reasoning,
    )
    return category, how
