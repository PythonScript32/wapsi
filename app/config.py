"""
Central configuration.

Secrets come from .env (never hardcoded, never committed). Policy bounds live
here as data so the governance gate reads limits from ONE place — never
hardcode a limit inside pipeline logic.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

# --- Razorpay (TEST mode only) ---------------------------------------------
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

# --- Supabase ---------------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")  # backend ONLY

# --- LLM / voice ------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")            # runtime brain (free tier)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")                # optional, faster free tier; preferred over Gemini when set
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")    # outbound voice (stretch)
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")

# --- Recovery policy — the bounds the governance gate enforces --------------
DEFAULT_POLICY = {
    # hard bounds
    "max_retries": 3,                    # per case, across all strategies
    "max_discount_pct": 10.0,            # never offer more
    "max_exposure_inr": 5000.0,          # above this, require human approval
    "min_contact_gap_hours": 24,         # anti-harassment
    "grace_period_days": 14,             # after this, close as CLOSED_LOST
    "rbi_pre_debit_notice_hours": 24,    # notify before any mandate debit
    "max_promise_horizon_days": 14,      # cap how far out a promise may be
    "backoff_hours": [4, 12, 24],        # exponential backoff schedule, by attempt number
    "default_offer_pct": 10.0,           # checkout touch-2 offer, before the max_discount_pct clamp

    # per-reason strategy + attempt caps
    "retry_rules": {
        "insufficient_funds": {"strategy": "after_salary_day",    "max_attempts": 3},
        "bank_downtime":      {"strategy": "backoff",             "max_attempts": 3},
        "mandate_revoked":    {"strategy": "request_re_mandate",  "max_attempts": 1},
        "expired_card":       {"strategy": "request_card_update", "max_attempts": 2},
        "technical_other":    {"strategy": "backoff",             "max_attempts": 2},
        "checkout_dropoff":   {"strategy": "nudge_then_offer",    "max_attempts": 2},
    },

    # channel preference order (India: WhatsApp + UPI link outperforms email)
    "channel_priority": ["whatsapp", "voice", "sms", "email"],

    # nominal costs, used for cost_per_recovered_rupee
    "cost_per_message_inr": {"whatsapp": 0.35, "sms": 0.20, "email": 0.02, "voice": 1.50},
}
