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


_CUSTOMER_FIELDS = ("name", "contact", "email")


def _customer_payload(customer: dict) -> dict:
    """Keep only the fields Razorpay's customer object understands, and only
    the ones that actually have a value — an empty/None field is worse than
    an absent one (some of Razorpay's validation rejects blank strings)."""
    if not isinstance(customer, dict):
        return {}
    return {k: customer[k] for k in _CUSTOMER_FIELDS if customer.get(k)}


def create_payment_link(amount: float, customer: dict, idempotency_key: str, *, purpose: str = "payment") -> dict:
    """
    Create a TEST-mode Razorpay Payment Link for `amount` rupees.

    customer: {"name": ..., "contact": ..., "email": ...} — passed through as
    Razorpay's own `customer` object so the link pre-fills their details.
    Never interpolated into text: a dict has no business inside `description`.

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
        "description": f"Wapsi recovery - Rs {float(amount):,.0f} {purpose}",
        "customer": _customer_payload(customer),
        "reference_id": idempotency_key,
        "notes": {"idempotency_key": idempotency_key},
    }
    return _client().payment_link.create(data, timeout=_TIMEOUT_S)


def fetch_payment(payment_id: str) -> dict:
    return _client().payment.fetch(payment_id, timeout=_TIMEOUT_S)


def fetch_order(order_id: str) -> dict:
    return _client().order.fetch(order_id, timeout=_TIMEOUT_S)
