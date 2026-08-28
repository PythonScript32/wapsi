"""
Razorpay webhook simulator — test your handler with NO tunnel required.

Why this beats ngrok for development:
  - instant, repeatable, works offline
  - you control the payload, so you can test every failure reason on demand
  - you can replay the SAME event twice to prove idempotency/dedupe works
  - no external service can rate-limit, block, or expire on you

A tunnel is only needed to receive events from Razorpay's actual servers.
Do that once at the end as a live-integration check. Build against this.

Usage:
    python scripts/simulate_webhook.py                      # payment.failed
    python scripts/simulate_webhook.py --event subscription.charged
    python scripts/simulate_webhook.py --reason mandate_revoked
    python scripts/simulate_webhook.py --duplicate           # send twice (dedupe test)
    python scripts/simulate_webhook.py --bad-signature       # must be rejected
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
import uuid

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "test_secret")
URL = os.getenv("WEBHOOK_URL", "http://localhost:8000/webhooks/razorpay")

# Realistic Razorpay error payloads per failure reason.
REASONS = {
    "insufficient_funds": (
        "BAD_REQUEST_ERROR",
        "Your account has insufficient balance to complete this transaction",
    ),
    "bank_downtime": (
        "GATEWAY_ERROR",
        "Payment processing failed because of an error at the bank",
    ),
    "mandate_revoked": (
        "BAD_REQUEST_ERROR",
        "The mandate has been revoked by the customer",
    ),
    "expired_card": (
        "BAD_REQUEST_ERROR",
        "Card has expired",
    ),
    "technical_other": (
        "SERVER_ERROR",
        "Payment failed due to a technical error",
    ),
}


def sign(body: bytes, secret: str) -> str:
    """Razorpay signs the raw body with HMAC-SHA256. Your handler must verify this."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def build_payload(event: str, reason: str, amount_paise: int) -> dict:
    code, desc = REASONS.get(reason, REASONS["technical_other"])
    pay_id = f"pay_{uuid.uuid4().hex[:14]}"
    now = int(time.time())

    payment = {
        "entity": {
            "id": pay_id,
            "entity": "payment",
            "amount": amount_paise,
            "currency": "INR",
            "status": "failed" if "failed" in event else "captured",
            "method": "upi",
            "email": "customer@example.com",
            "contact": "+919876543210",
            "error_code": code,
            "error_description": desc,
            "error_source": "bank",
            "error_reason": reason,
            "created_at": now,
        }
    }

    body = {
        "entity": "event",
        "account_id": "acc_TEST00000000",
        "event": event,
        "contains": ["payment"],
        "payload": {"payment": payment},
        "created_at": now,
    }

    if event.startswith("subscription"):
        body["contains"].append("subscription")
        body["payload"]["subscription"] = {
            "entity": {
                "id": f"sub_{uuid.uuid4().hex[:14]}",
                "entity": "subscription",
                "plan_id": f"plan_{uuid.uuid4().hex[:12]}",
                "status": "halted" if "halted" in event else "active",
                "current_start": now - 2592000,
                "current_end": now,
                "paid_count": 3,
                "customer_notify": True,
            }
        }

    if event == "order.paid":
        body["contains"].append("order")
        body["payload"]["order"] = {
            "entity": {
                "id": f"order_{uuid.uuid4().hex[:12]}",
                "entity": "order",
                "amount": amount_paise,
                "amount_paid": amount_paise,
                "currency": "INR",
                "status": "paid",
                "created_at": now,
            }
        }

    return body


def send(body: dict, *, bad_signature: bool = False) -> None:
    raw = json.dumps(body, separators=(",", ":")).encode()
    signature = sign(raw, SECRET)
    if bad_signature:
        signature = "0" * 64

    headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": signature,
        "X-Razorpay-Event-Id": body.get("_event_id") or uuid.uuid4().hex,
    }

    try:
        r = httpx.post(URL, content=raw, headers=headers, timeout=10)
        print(f"  -> {r.status_code}  {r.text[:200]}")
    except httpx.ConnectError:
        print("  -> CONNECTION FAILED. Is uvicorn running on port 8000?")


def main() -> None:
    ap = argparse.ArgumentParser(description="Send a fake Razorpay webhook to your local handler.")
    ap.add_argument("--event", default="payment.failed",
                    choices=["payment.failed", "payment.captured", "order.paid",
                             "subscription.charged", "subscription.halted"])
    ap.add_argument("--reason", default="insufficient_funds", choices=list(REASONS))
    ap.add_argument("--amount", type=int, default=49900, help="in paise (49900 = Rs 499)")
    ap.add_argument("--duplicate", action="store_true", help="send the same event twice")
    ap.add_argument("--bad-signature", action="store_true", help="should be rejected")
    args = ap.parse_args()

    body = build_payload(args.event, args.reason, args.amount)
    event_id = uuid.uuid4().hex
    body["_event_id"] = event_id

    print(f"POST {URL}")
    print(f"  event  : {args.event}")
    print(f"  reason : {args.reason}")
    print(f"  amount : Rs {args.amount / 100:,.2f}")
    print(f"  secret : {'set' if SECRET != 'test_secret' else 'DEFAULT (set RAZORPAY_WEBHOOK_SECRET in .env)'}")

    if args.bad_signature:
        print("  signature: DELIBERATELY INVALID — handler must reject this")

    send(body, bad_signature=args.bad_signature)

    if args.duplicate:
        print("\nResending the identical event (dedupe test)...")
        print("  Expected: accepted but NOT processed twice — no second case, no second charge.")
        send(body)


if __name__ == "__main__":
    main()
