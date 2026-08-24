"""
Thin wrapper around the Razorpay TEST-mode API.

Used for: create payment link, fetch payment/subscription/order, retry a charge,
refund (edge cases). Auth = Basic Auth with RAZORPAY_KEY_ID / SECRET.

RULES:
- Every state-changing call carries an idempotency key.
- Wrap calls with timeout + exponential backoff (see actions.py).
- Test mode only. Never put live keys in this repo.
"""
from __future__ import annotations

# TODO: class RazorpayClient:
#         create_payment_link(amount, customer, idempotency_key) -> dict
#         fetch_payment(payment_id) -> dict
#         retry_subscription_charge(subscription_id, idempotency_key) -> dict
#         refund(payment_id, amount, idempotency_key) -> dict
