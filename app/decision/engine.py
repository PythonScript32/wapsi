"""
Decision engine (the brain): category + case history + policy -> an intervention.

Returns a Decision:
  {
    "intervention": "retry_now" | "retry_after_date" | "request_re_mandate"
                    | "request_card_update" | "send_link"
                    | "send_link_with_offer" | "escalate" | "close_lost",
    "scheduled_for": iso8601 | None,
    "channel": "whatsapp" | "sms" | "email" | "voice",
    "message": "<Hinglish copy>",
    "discount_pct": float,          # 0 unless an offer, always <= policy cap
    "reasoning": "<why, plain language>"
  }

TIMING INTELLIGENCE (the thing that beats a naive retry):
  insufficient_funds -> wait for the customer's salary day (1st / month-end
                        cluster), then retry. Retrying immediately mostly fails.
  bank_downtime      -> short exponential backoff (hours), retry soon.
  mandate_revoked    -> NEVER silently retry; request re-mandate.
  expired_card       -> request card update; payment link as fallback.
  technical_other    -> backoff, capped attempts.
  checkout_dropoff   -> nudge first; bounded offer only on the second touch.

The LLM writes the Hinglish copy and reasons about edge cases, but every field
is clamped to policy before it leaves this module, and the result must still
pass governance.policy_gate.check() before execution.
"""
from __future__ import annotations

# TODO: def decide(case: dict, history: list[dict], policy: dict) -> dict
# TODO: def _next_salary_day(today, salary_day_hint: int | None) -> date
