"""
Feature C (outbound) -- ElevenLabs Hinglish voice reminders.

Turns the decision engine's Hinglish message into audio for a voice reminder.
Voice reminders outperform plain text for recovery, and this is the moment that
makes the demo memorable.

RULES:
- Pick a multilingual voice that handles Devanagari-flavoured Hinglish; write
  the script in Roman Hinglish for the most natural pronunciation.
- Cache generated audio by hash(text, voice_id) -- the free tier is
  character-limited, so never regenerate the same line twice.
- This is a STRETCH feature. If the API/quota fails, fall back to text outreach
  and log the fallback. It must never block a recovery.
- Keep scripts under ~30 seconds, one clear ask, one link.
"""
from __future__ import annotations

# TODO: def synthesize(text: str, voice_id: str | None = None) -> bytes | None
# TODO: def voice_reminder(case: dict, message: str) -> dict  # returns audio ref
