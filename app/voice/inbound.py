"""
Feature C (inbound) -- Hinglish voice/chat understanding.

Customer replies to outreach with a voice note or text. Gemini Flash handles
BOTH transcription and intent extraction in one multimodal call (free tier).

Return:
  {
    "transcript": str,
    "intent": "promise_to_pay" | "already_paid" | "opt_out" | "pay_now"
              | "dispute" | "unclear",
    "promised_date": "YYYY-MM-DD" | None,
    "confidence": float
  }

RULES:
- Hinglish is the default register: "abhi paise nahi, agle hafte kar dunga".
- Resolve relative dates ("agle hafte", "salary ke baad", "3 din mein") against
  today's date, and against the customer's salary day when known.
- Low confidence or ambiguity -> 'unclear'. NEVER guess a promise into
  existence; a wrong promise silently delays recovery.
- 'opt_out' must halt all outreach immediately and permanently for that case.
"""
from __future__ import annotations

# TODO: def parse_reply(audio_bytes: bytes | None, text: str | None, ctx: dict) -> dict
