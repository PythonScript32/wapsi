"""
Thin wrapper over the Razorpay TEST-mode API.

Every call gets a fixed timeout and lets the SDK's own exception raise
through unmodified on any non-2xx response (the SDK already turns a bad
response into BadRequestError / GatewayError / ServerError). This module
never catches anything itself — app/execution/actions.py is what needs to
react to a failure (back off, then escalate), so the failure must reach it.
"""
from __future__ import annotations

from functools import lru_cache

import razorpay

from app import config

_TIMEOUT_S = 10


@lru_cache(maxsize=1)
def _client() -> razorpay.Client:
    if not config.RAZORPAY_KEY_ID or not config.RAZORPAY_KEY_SECRET:
        raise RuntimeError(
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET missing. Copy .env.example to "
            ".env and fill them in with TEST-mode keys."
        )
    return razorpay.Client(auth=(config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET))


def create_payment_link(amount: float, customer: str, idempotency_key: str) -> dict:
    """
    Create a TEST-mode Razorpay Payment Link for `amount` rupees.

    The Payment Link API has no native idempotency-key header, so the key is
    stashed in reference_id/notes purely as a debugging trail back to the
    originating attempt. It is NOT the safety mechanism — the UNIQUE
    constraint on payment_attempts.idempotency_key is what actually prevents
    a double charge (see app/execution/actions.py).
    """
    paise = int(round(float(amount) * 100))
    data = {
        "amount": paise,
        "currency": "INR",
        "description": f"Wapsi recovery - {customer}",
        "reference_id": idempotency_key,
        "notes": {"idempotency_key": idempotency_key},
    }
    return _client().payment_link.create(data, timeout=_TIMEOUT_S)


def fetch_payment(payment_id: str) -> dict:
    return _client().payment.fetch(payment_id, timeout=_TIMEOUT_S)


def fetch_order(order_id: str) -> dict:
    return _client().order.fetch(order_id, timeout=_TIMEOUT_S)
