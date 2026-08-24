"""
Detection sensor #1 -- Razorpay webhooks.

Events we care about:
  subscription.charged / subscription.pending / subscription.halted
  payment.failed / payment.captured
  order.paid              (to close checkout cases + prevent chasing payers)

RULES:
- Verify X-Razorpay-Signature (HMAC-SHA256 with the webhook secret) BEFORE
  trusting anything. Reject silently on mismatch.
- Events arrive duplicated and out of order -> dedupe by event id, and never
  regress a case's state (RECOVERED never goes back to RETRYING).
- Handlers stay fast: create/advance the case, write audit, return 200. All
  decisions happen in the pipeline, not in the handler.
"""
from __future__ import annotations

# TODO: def verify_signature(body: bytes, signature: str, secret: str) -> bool
# TODO: def handle_event(event: dict) -> None
