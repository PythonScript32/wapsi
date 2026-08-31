"""
Feature C (inbound) -- Hinglish voice/chat understanding.

Customer replies to outreach with a voice note or typed text. The LLM's ONLY
jobs are: transcribe (if audio) and classify intent + pull out the raw date
PHRASE the customer used. Resolving that phrase into an actual calendar date
is deterministic Python, never the LLM -- see _resolve_date_phrase().

PROVIDER: Groq (Whisper transcription + a text classification call) when
GROQ_API_KEY is set, else Gemini (one multimodal call does both). Either way
the network call itself lives in app.llm.client -- call_voice() for audio,
call() for text -- so this module never touches genai/httpx directly and
tests mock exactly those two functions.

RULES:
- Low confidence, an empty transcript, or an intent outside the enum ->
  "unclear". NEVER invent a promise; a wrong promise silently delays recovery
  by a week.
- promise_to_pay with a date phrase that doesn't resolve -> "unclear" too --
  a promise with no date isn't actionable.
- promised_date is capped at policy["max_promise_horizon_days"].
- Every call logs REPLY_RECEIVED with the transcript AND the detected intent.
  The transcript is the artefact that makes voice auditable -- a judge must
  be able to read what the customer said and what the agent understood.
- Any provider failure (network, malformed reply) -> intent "unclear", logged
  via audit_log.error as degraded mode. Voice must never crash the pipeline.
- Never reads case["latent"].
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app import config
from app.audit import log as audit_log
from app.decision import engine as decision_engine
from app.llm import client as llm_client

INTENTS = ("promise_to_pay", "already_paid", "opt_out", "pay_now", "dispute", "unclear")

# Below this, a "confident-sounding" answer is treated as a coin flip and
# downgraded -- better to ask again than to act on a guess.
_MIN_CONFIDENCE = 0.5

_MIME_BY_EXT = {
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
}


def mime_type_for_path(path: str) -> str:
    """Guess the audio MIME type from a file extension. WhatsApp voice notes
    are OGG/Opus, but never assume that format for whatever's actually on
    disk -- read the extension."""
    ext = os.path.splitext(path)[1].lower()
    return _MIME_BY_EXT.get(ext, "audio/ogg")


# ---------------------------------------------------------------------------
# Prompts -- caller-supplied to the ONE LLM seam per provider (app.llm.client).
# All three ask for the same strict JSON shape so downstream parsing is
# identical regardless of which path produced it.
# ---------------------------------------------------------------------------

_JSON_INSTRUCTIONS = """Respond with STRICT JSON only -- no markdown fences, no commentary -- matching exactly this shape:
{{"transcript": "<what was said, verbatim>", "intent": "<one of: promise_to_pay, already_paid, opt_out, pay_now, dispute, unclear>", "raw_date_phrase": "<the exact date/timing phrase used, or null if none>", "confidence": <float 0.0-1.0>}}

Context: this is a reply from an Indian customer to a payment-recovery outreach message (WhatsApp/SMS/voice) about a failed or abandoned payment. Replies are commonly Hinglish (Hindi written in Roman script, mixed with English).

Intent definitions:
- promise_to_pay: customer commits to pay by some date, possibly relative, e.g. "kal kar dunga", "agle hafte pay kar dunga".
- already_paid: customer says they already paid.
- opt_out: customer wants no further contact, e.g. "band kar do", "mujhe mat bhejo", "stop messaging".
- pay_now: customer wants to pay immediately / asks for a payment link right now.
- dispute: customer disputes the charge or says it isn't their transaction.
- unclear: ambiguous, off-topic, or too low-confidence to act on.

raw_date_phrase: copy the EXACT words the customer used for timing (e.g. "kal", "parso", "agle hafte", "3 din mein", "agle mahine", "salary ke baad", "mahine ke end tak"). Do NOT resolve or calculate an actual date yourself -- that happens afterwards, deterministically. Use null if intent isn't promise_to_pay or no timing was mentioned.

confidence: your genuine confidence in the intent classification, 0.0-1.0. Use LOW confidence (below 0.5) whenever the audio/text is mumbled, cut off, or could plausibly mean more than one thing -- prefer honesty about uncertainty over a confident-sounding guess."""

_AUDIO_PROMPT = (
    _JSON_INSTRUCTIONS
    + "\n\nTranscribe the attached audio (it may be Hinglish) and classify it per the instructions above."
)

_TRANSCRIPT_PROMPT = (
    _JSON_INSTRUCTIONS
    + '\n\nCustomer\'s message (already transcribed): "{transcript}"\n\n'
    'Set "transcript" in your JSON to this exact text, and classify it per the instructions above.'
)

_TEXT_PROMPT = (
    _JSON_INSTRUCTIONS
    + '\n\nCustomer\'s message (typed): "{text}"\n\n'
    'Set "transcript" in your JSON to this exact text, and classify it per the instructions above.'
)


# ---------------------------------------------------------------------------
# Deterministic Hinglish date resolution
# ---------------------------------------------------------------------------

def _first_of_next_month(now: datetime) -> date:
    if now.month == 12:
        return date(now.year + 1, 1, 1)
    return date(now.year, now.month + 1, 1)


def _month_end(now: datetime) -> date:
    return _first_of_next_month(now) - timedelta(days=1)


_N_DIN_RE = re.compile(r"(\d+)\s*din")


def _resolve_date_phrase(phrase: str | None, now: datetime) -> date | None:
    """
    The one place a promised date is actually computed. The LLM only reports
    the PHRASE it heard; this maps that phrase to a real date so the same
    words always resolve the same way, run to run, provider to provider.

    Returns None when the phrase is missing or not one we recognise -- the
    caller treats that as "unresolvable", never as a silent default.
    """
    if not phrase:
        return None
    p = re.sub(r"\s+", " ", phrase.strip().lower())
    if not p:
        return None

    m = _N_DIN_RE.search(p)
    if m:
        return (now + timedelta(days=int(m.group(1)))).date()

    if "agle mahine" in p or "agla mahina" in p or "next month" in p:
        return _first_of_next_month(now)
    if "mahine ke end" in p or "mahina khatam" in p or "mahine ke aakhir" in p or "month end" in p:
        return _month_end(now)
    if "salary" in p:
        # Same heuristic the decision engine uses for insufficient_funds
        # retries -- the agent stays consistent with itself, and still never
        # sees the customer's real salary day.
        return decision_engine._next_salary_day(now).date()
    if "agle hafte" in p:
        return (now + timedelta(days=7)).date()
    if "is hafte" in p:
        return (now + timedelta(days=3)).date()
    if "parso" in p:
        return (now + timedelta(days=2)).date()
    if "kal" in p:
        return (now + timedelta(days=1)).date()

    return None


def _cap_to_horizon(resolved: date, now: datetime, policy: dict) -> tuple[date, bool]:
    horizon_days = policy.get("max_promise_horizon_days", 14)
    cap = (now + timedelta(days=horizon_days)).date()
    if resolved > cap:
        return cap, True
    return resolved, False


# ---------------------------------------------------------------------------
# JSON parsing of the LLM reply
# ---------------------------------------------------------------------------

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _parse_json_reply(raw: str) -> dict:
    if not raw or not raw.strip():
        raise ValueError("empty reply from provider")
    cleaned = _CODE_FENCE_RE.sub("", raw.strip())
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("reply JSON was not an object")
    return parsed


def _actor(has_audio: bool) -> str:
    if config.GROQ_API_KEY:
        provider = "groq:whisper-large-v3-turbo+llama-3.3-70b-versatile" if has_audio else "groq:llama-3.3-70b-versatile"
    else:
        provider = f"gemini:{config.GEMINI_MODEL}"
    return f"voice.inbound:{provider}"


def _clamp_confidence(value: Any) -> float:
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, conf))


def _log(case_id: str | None, actor: str, result: dict, has_audio: bool, reasoning: str) -> None:
    audit_log.record(
        case_id,
        actor,
        audit_log.REPLY_RECEIVED,
        inp={"channel": "voice" if has_audio else "text"},
        decision=result["intent"],
        reasoning=reasoning,
        result=result,
    )


def _empty_result(transcript: str = "") -> dict:
    return {"transcript": transcript, "intent": "unclear", "promised_date": None,
            "raw_date_phrase": None, "confidence": 0.0}


def parse_reply(audio_bytes: bytes | None = None, text: str | None = None, ctx: dict | None = None) -> dict:
    """
    Understand one customer reply -- audio, or typed text, never both.

    ctx (all optional):
      case_id     -- for audit attribution
      mime_type   -- audio MIME type (default "audio/ogg", WhatsApp's format)
      policy      -- for max_promise_horizon_days (default config.DEFAULT_POLICY)
      now         -- injectable clock for deterministic date resolution
    """
    ctx = ctx or {}
    case_id = ctx.get("case_id") or ctx.get("id")
    policy = ctx.get("policy") or config.DEFAULT_POLICY
    now = ctx.get("now") or datetime.now(timezone.utc)
    has_audio = audio_bytes is not None

    if not has_audio and not (text and text.strip()):
        result = _empty_result()
        _log(case_id, "voice.inbound", result, has_audio, "No audio or text supplied -- nothing to understand.")
        return result

    actor = _actor(has_audio)
    try:
        if has_audio:
            mime_type = ctx.get("mime_type") or "audio/ogg"
            transcript_hint, raw_reply = llm_client.call_voice(
                _TRANSCRIPT_PROMPT if config.GROQ_API_KEY else _AUDIO_PROMPT,
                audio_bytes,
                mime_type,
            )
            parsed = _parse_json_reply(raw_reply)
            transcript = transcript_hint if transcript_hint is not None else str(parsed.get("transcript") or "").strip()
        else:
            raw_reply = llm_client.call(_TEXT_PROMPT.format(text=text))
            parsed = _parse_json_reply(raw_reply)
            transcript = text.strip()
    except Exception as exc:
        audit_log.error(case_id, actor, "Voice/text understanding call failed -- running in degraded mode", exc)
        result = _empty_result()
        _log(case_id, actor, result, has_audio,
             f"Provider call failed ({exc}); degraded mode, defaulting to unclear so nothing is invented.")
        return result

    intent = str(parsed.get("intent") or "").strip().lower()
    if intent not in INTENTS:
        intent = "unclear"

    raw_date_phrase = parsed.get("raw_date_phrase")
    raw_date_phrase = raw_date_phrase.strip() if isinstance(raw_date_phrase, str) and raw_date_phrase.strip() else None

    confidence = _clamp_confidence(parsed.get("confidence"))

    downgrades: list[str] = []
    if not transcript.strip():
        intent = "unclear"
        downgrades.append("transcript came back empty")
    elif confidence < _MIN_CONFIDENCE:
        intent = "unclear"
        downgrades.append(f"confidence {confidence:.2f} is below the {_MIN_CONFIDENCE:.2f} threshold to act on")

    promised_date = None
    capped = False
    if intent == "promise_to_pay":
        resolved = _resolve_date_phrase(raw_date_phrase, now)
        if resolved is None:
            intent = "unclear"
            downgrades.append(f"date phrase {raw_date_phrase!r} did not resolve to a known pattern")
        else:
            capped_date, capped = _cap_to_horizon(resolved, now, policy)
            promised_date = capped_date.isoformat()

    result = {
        "transcript": transcript,
        "intent": intent,
        "promised_date": promised_date,
        "raw_date_phrase": raw_date_phrase,
        "confidence": confidence,
    }

    reasoning_parts = [f'{"Voice note" if has_audio else "Text reply"} transcript: "{transcript}". Intent: {intent}.']
    if downgrades:
        reasoning_parts.append("Downgraded because " + "; ".join(downgrades) + ".")
    if promised_date:
        reasoning_parts.append(
            f"Promised date resolved to {promised_date}" + (" (capped at the policy horizon)." if capped else ".")
        )
    _log(case_id, actor, result, has_audio, " ".join(reasoning_parts))
    return result
