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
import sys
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
{{"transcript": "<what was said, verbatim>", "intent": "<one of: promise_to_pay, already_paid, opt_out, pay_now, dispute, unclear>", "raw_date_phrase": "<the exact date/timing phrase used, in whatever script it was spoken, or null if none>", "raw_date_phrase_roman": "<the same phrase transliterated to Roman script, or null if raw_date_phrase is null>", "confidence": <float 0.0-1.0>}}

Context: this is a reply from an Indian customer to a payment-recovery outreach message (WhatsApp/SMS/voice) about a failed or abandoned payment. Replies are commonly Hinglish and may come back in either Devanagari or Roman script.

Intent definitions:
- promise_to_pay: customer commits to pay by some date, possibly relative, e.g. "kal kar dunga", "agle hafte pay kar dunga".
- already_paid: customer says they already paid.
- opt_out: customer wants no further contact, e.g. "band kar do", "mujhe mat bhejo", "stop messaging".
- pay_now: customer wants to pay immediately / asks for a payment link right now.
- dispute: customer disputes the charge or says it isn't their transaction.
- unclear: ambiguous, off-topic, or too low-confidence to act on.

raw_date_phrase: copy the EXACT words the customer used for timing, in whatever script they were spoken (Devanagari or Roman) -- e.g. "kal", "परसों", "agle hafte", "अगले हफ्ते", "3 din mein", "agle mahine", "salary ke baad", "mahine ke end tak". Do NOT resolve or calculate an actual date yourself -- that happens afterwards, deterministically. Use null if intent isn't promise_to_pay or no timing was mentioned.

raw_date_phrase_roman: the SAME phrase, transliterated to Roman script (e.g. "अगले हफ्ते" -> "agle hafte"). This is a second, independent attempt at the same answer, used as a fallback if the native-script phrase doesn't match a known pattern. Use null whenever raw_date_phrase is null.

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

# Spelled-out number words, 1-10 -- "teen din mein" (three days) is at least
# as common as "3 din mein" in spoken Hinglish, so the digit regex alone
# misses it. Still a fixed lookup table, never LLM inference.
_ROMAN_NUMBER_WORDS = {
    "ek": 1, "do": 2, "teen": 3, "char": 4,
    "paanch": 5, "panch": 5,
    "chhah": 6, "chhe": 6,
    "saat": 7, "aath": 8, "nau": 9, "das": 10,
}
_N_DIN_WORD_RE = re.compile(
    r"\b(" + "|".join(sorted(_ROMAN_NUMBER_WORDS, key=len, reverse=True)) + r")\s*din\b"
)


def _resolve_roman_phrase(p: str, now: datetime) -> date | None:
    """Roman-script Hinglish date phrases (e.g. "agle hafte"). `p` is already
    stripped/collapsed/lowercased by the caller."""
    m = _N_DIN_RE.search(p)
    if m:
        return (now + timedelta(days=int(m.group(1)))).date()

    m = _N_DIN_WORD_RE.search(p)
    if m:
        return (now + timedelta(days=_ROMAN_NUMBER_WORDS[m.group(1)])).date()

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


# Groq Whisper transcribes Hinglish speech as Devanagari script far more
# often than Roman, so the deterministic resolver needs native-script
# patterns too -- an LLM-guessed date is never acceptable, so these are
# matched exactly the same way as the Roman ones: known substrings only.
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_HALANT = "्"  # ् -- strips so "हफ्ते" and "हफते" normalise to one spelling
_N_DIN_DEVANAGARI_RE = re.compile(r"(\d+)\s*दिन")

# Spelled-out number words, 1-10 -- same fixed lookup as the Roman side, not
# LLM inference. Devanagari has fewer common spelling variants than Roman
# transliteration, but पांच/पाँच and छह/छे both show up in practice.
_DEVANAGARI_NUMBER_WORDS = {
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4,
    "पांच": 5, "पाँच": 5,
    "छह": 6, "छे": 6,
    "सात": 7, "आठ": 8, "नौ": 9, "दस": 10,
}
_N_DIN_DEVANAGARI_WORD_RE = re.compile(
    "(" + "|".join(sorted(_DEVANAGARI_NUMBER_WORDS, key=len, reverse=True)) + r")\s*दिन"
)


def _resolve_devanagari_phrase(p: str, now: datetime) -> date | None:
    """Devanagari-script date phrases. `p` is already stripped/collapsed by
    the caller; digits and the optional halant are normalised here."""
    p = p.translate(_DEVANAGARI_DIGITS).replace(_HALANT, "")

    m = _N_DIN_DEVANAGARI_RE.search(p)
    if m:
        return (now + timedelta(days=int(m.group(1)))).date()

    m = _N_DIN_DEVANAGARI_WORD_RE.search(p)
    if m:
        return (now + timedelta(days=_DEVANAGARI_NUMBER_WORDS[m.group(1)])).date()

    if "अगले महीने" in p or "अगला महीना" in p:
        return _first_of_next_month(now)
    if "महीने के अंत" in p or "महीने के आखिर" in p:
        return _month_end(now)
    if "सैलरी" in p or "सेलरी" in p:
        return decision_engine._next_salary_day(now).date()
    if "अगले हफते" in p:  # halant already stripped, so हफ्ते and हफते both land here
        return (now + timedelta(days=7)).date()
    if "इस हफते" in p:
        return (now + timedelta(days=3)).date()
    if "परसों" in p or "परसो" in p:
        return (now + timedelta(days=2)).date()
    if "कल" in p:
        return (now + timedelta(days=1)).date()

    return None


def _resolve_date_phrase(phrase: str | None, now: datetime) -> date | None:
    """
    The one place a promised date is actually computed. The LLM only reports
    the PHRASE it heard; this maps that phrase to a real date so the same
    words always resolve the same way, run to run, provider to provider,
    script to script.

    Tries the Roman-Hinglish patterns first, then the Devanagari ones --
    independent matchers over the same normalised text, since Whisper may
    hand back either script for the same spoken words.

    Returns None when the phrase is missing or not one we recognise -- the
    caller treats that as "unresolvable", never as a silent default.
    """
    if not phrase:
        return None
    p = re.sub(r"\s+", " ", phrase.strip().lower())
    if not p:
        return None

    return _resolve_roman_phrase(p, now) or _resolve_devanagari_phrase(p, now)


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
        provider = f"groq:{config.GROQ_WHISPER_MODEL}+{config.GROQ_CHAT_MODEL}" if has_audio else f"groq:{config.GROQ_CHAT_MODEL}"
    else:
        provider = f"gemini:{config.GEMINI_MODEL}"
    return f"voice.inbound:{provider}"


_FINISH_REASON_RE = re.compile(r"finish_reason=(\w+)")


def _finish_reason_from_exc(exc: Exception) -> str | None:
    """Pull a Gemini finish_reason out of an exception's message, if it has
    one -- see app.llm.client._extract_gemini_text, which raises
    'finish_reason=SAFETY'-style RuntimeErrors when the model returned no
    text part."""
    m = _FINISH_REASON_RE.search(str(exc))
    return m.group(1) if m else None


def _debug(ctx: dict, msg: str) -> None:
    """Print a diagnostic line when ctx['debug'] is set -- opt-in, off by
    default for real pipeline callers. scripts/test_voice.py turns this on so
    a degraded result never shows up unexplained: which provider ran, the raw
    reply or exception it got back, and why the parsed result ended up the
    way it did."""
    if ctx.get("debug"):
        print(f"[voice.inbound] {msg}", file=sys.stderr)


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
      debug       -- print provider, raw reply/exception, finish_reason, and
                     the downgrade reasoning to stderr as they happen. Off by
                     default; scripts/test_voice.py turns it on so a degraded
                     result is never unexplained.
    """
    ctx = ctx or {}
    case_id = ctx.get("case_id") or ctx.get("id")
    policy = ctx.get("policy") or config.DEFAULT_POLICY
    now = ctx.get("now") or datetime.now(timezone.utc)
    has_audio = audio_bytes is not None

    if not has_audio and not (text and text.strip()):
        result = _empty_result()
        why = "No audio or text supplied -- nothing to understand."
        _log(case_id, "voice.inbound", result, has_audio, why)
        _debug(ctx, why)
        return result

    actor = _actor(has_audio)
    _debug(ctx, f"provider  : {actor}")
    try:
        if has_audio:
            mime_type = ctx.get("mime_type") or "audio/ogg"
            transcript_hint, raw_reply = llm_client.call_voice(
                _TRANSCRIPT_PROMPT if config.GROQ_API_KEY else _AUDIO_PROMPT,
                audio_bytes,
                mime_type,
            )
            _debug(ctx, f"raw reply : {raw_reply!r} (transcript hint: {transcript_hint!r})")
            parsed = _parse_json_reply(raw_reply)
            transcript = transcript_hint if transcript_hint is not None else str(parsed.get("transcript") or "").strip()
        else:
            raw_reply = llm_client.call(_TEXT_PROMPT.format(text=text))
            _debug(ctx, f"raw reply : {raw_reply!r}")
            parsed = _parse_json_reply(raw_reply)
            transcript = text.strip()
    except Exception as exc:
        finish_reason = _finish_reason_from_exc(exc)
        _debug(ctx, f"EXCEPTION : {type(exc).__name__}: {exc}")
        if finish_reason:
            _debug(ctx, f"finish_reason: {finish_reason}")
        audit_log.error(case_id, actor, "Voice/text understanding call failed -- running in degraded mode", exc)
        result = _empty_result()
        why = f"Provider call failed ({exc}); degraded mode, defaulting to unclear so nothing is invented."
        _log(case_id, actor, result, has_audio, why)
        _debug(ctx, f"degraded because: {why}")
        return result

    intent = str(parsed.get("intent") or "").strip().lower()
    if intent not in INTENTS:
        intent = "unclear"

    raw_date_phrase = parsed.get("raw_date_phrase")
    raw_date_phrase = raw_date_phrase.strip() if isinstance(raw_date_phrase, str) and raw_date_phrase.strip() else None

    raw_date_phrase_roman = parsed.get("raw_date_phrase_roman")
    raw_date_phrase_roman = (
        raw_date_phrase_roman.strip() if isinstance(raw_date_phrase_roman, str) and raw_date_phrase_roman.strip() else None
    )

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
    resolution_note = None
    if intent == "promise_to_pay":
        resolved = _resolve_date_phrase(raw_date_phrase, now)
        if resolved is None and raw_date_phrase_roman:
            # Two independent paths to the same answer: our own deterministic
            # matcher on the phrase as heard, and the LLM's own transliteration
            # fed through the same Roman matcher. Still never LLM-guessed --
            # only the transliteration comes from the model, not the date.
            resolved = _resolve_date_phrase(raw_date_phrase_roman, now)
            if resolved is not None:
                resolution_note = f'via the Roman transliteration "{raw_date_phrase_roman}"'
        if resolved is None:
            intent = "unclear"
            downgrades.append(
                f"date phrase {raw_date_phrase!r} (roman: {raw_date_phrase_roman!r}) did not resolve to a known pattern"
            )
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
            f"Promised date resolved to {promised_date}"
            + (" (capped at the policy horizon)" if capped else "")
            + (f", {resolution_note}" if resolution_note else "")
            + "."
        )
    reasoning = " ".join(reasoning_parts)
    _log(case_id, actor, result, has_audio, reasoning)
    _debug(ctx, reasoning)
    return result
