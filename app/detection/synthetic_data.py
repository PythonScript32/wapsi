"""
Synthetic data generator for वापसी (Wapsi).

WHY THIS EXISTS
---------------
The track requires "measured money recovered across a batch". Razorpay test mode
will not hand you 100+ varied real failures, so we manufacture a realistic,
reproducible population of at-risk revenue.

THE DEV / HOLDOUT SPLIT (this is the rigour that earns trust)
-------------------------------------------------------------
  dev     — 100 cases, seed 42.  Build and tune against this. Look at it freely.
  holdout — 300 cases, seed 20260905. Run ONCE at the end. Report these numbers.

Never tune against the holdout. Reporting holdout metrics means your numbers are
an honest estimate of performance on unseen cases, not a number you fitted to.

HIDDEN GROUND TRUTH
-------------------
Each case carries a `latent` block describing how that customer WOULD behave.
The pipeline must NEVER read `latent` — it decides blind, exactly as it would in
production. Only the outcome simulator in batch_scanner.py reads it, to judge
whether an action actually recovered the money. That is what makes it possible
to measure whether the agent's *timing* was smart, and to compute lift over a
naive baseline.

Stdlib only → zero dependencies → nothing external can block you.

Run:
    python -m app.detection.synthetic_data              # writes BOTH sets
    python -m app.detection.synthetic_data --only dev
"""
from __future__ import annotations

import argparse
import json
import os
import random
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Distributions (India-flavoured, weights are relative)
# ---------------------------------------------------------------------------

SUBSCRIPTION_REASONS = {
    "insufficient_funds": 45,   # dominates real UPI AutoPay failures
    "bank_downtime": 15,
    "mandate_revoked": 10,
    "expired_card": 10,
    "technical_other": 10,
}

AMOUNTS = [99, 129, 149, 199, 249, 299, 399, 499, 599, 799, 999, 1499, 1999]

FIRST_NAMES = [
    "Aarav", "Vivaan", "Aditya", "Ishaan", "Kabir", "Ananya", "Diya", "Saanvi",
    "Aisha", "Priya", "Rohan", "Karan", "Neha", "Pooja", "Rahul", "Sneha",
    "Arjun", "Meera", "Nikhil", "Riya", "Sahil", "Tanvi", "Yash", "Zara",
    "Farhan", "Ritika", "Manav", "Divya", "Siddharth", "Kavya",
]
LAST_NAMES = [
    "Sharma", "Verma", "Gupta", "Mehta", "Iyer", "Nair", "Reddy", "Rao",
    "Khan", "Singh", "Patel", "Das", "Bose", "Joshi", "Chopra", "Malhotra",
    "Pillai", "Banerjee", "Kulkarni", "Shetty",
]

# Probability a case is fundamentally recoverable IF the agent acts correctly.
RECOVERABLE_PROB = {
    "insufficient_funds": 0.72,
    "bank_downtime": 0.88,
    "mandate_revoked": 0.40,
    "expired_card": 0.45,
    "technical_other": 0.55,
    "checkout_dropoff": 0.35,
}

# The strategy that actually works for each reason. A smart agent picks this;
# the naive baseline just retries immediately.
CORRECT_STRATEGY = {
    "insufficient_funds": "after_salary_day",
    "bank_downtime": "backoff",
    "mandate_revoked": "request_re_mandate",
    "expired_card": "request_card_update",
    "technical_other": "backoff",
    "checkout_dropoff": "nudge_then_offer",
}

# Raw gateway reason strings — diagnosis must map these, not the clean category.
RAW_REASONS = {
    "insufficient_funds": [
        "Your account has insufficient balance to complete this transaction",
        "INSUFFICIENT_FUNDS",
        "Payment failed due to insufficient funds in the customer account",
    ],
    "bank_downtime": [
        "Bank server is currently unavailable. Please try again",
        "GATEWAY_ERROR: issuing bank down",
        "Payment processing failed at the bank end",
    ],
    "mandate_revoked": [
        "Mandate has been revoked by the customer",
        "AUTOPAY_MANDATE_CANCELLED",
        "The UPI mandate is no longer active",
    ],
    "expired_card": [
        "Card has expired",
        "EXPIRED_CARD",
        "The card used for this payment is no longer valid",
    ],
    "technical_other": [
        "Payment failed due to a technical error",
        "SERVER_ERROR: unable to process",
        "Transaction could not be completed",
    ],
    "checkout_dropoff": [
        "Order created but not paid within window",
    ],
}

HINGLISH_REPLIES = {
    "promise_to_pay": [
        "abhi paise nahi hain bhai, {day} tak kar dunga",
        "salary aane do, {day} ko pay kar deta hun",
        "thoda time do yaar, agle hafte pakka ho jayega",
        "is week thoda tight hai, {day} tak clear kar dunga",
        "abhi possible nahi hai, {day} ko definitely karunga",
    ],
    "already_paid": [
        "maine to already pay kar diya hai",
        "paisa kat gaya mera, check karo",
        "kal hi payment ho gaya tha bhai",
    ],
    "opt_out": [
        "mujhe nahi chahiye ab, band kar do",
        "cancel kar do subscription please",
        "ab use nahi karta, stop kar do",
    ],
    "pay_now": [
        "haan abhi karta hun, link bhejo",
        "ok kar deta hun abhi",
        "link do, paying now",
    ],
    "dispute": [
        "maine ye subscribe hi nahi kiya tha",
        "galat charge lag raha hai mujhe",
    ],
}


@dataclass
class Case:
    id: str
    batch_id: str
    source: str
    customer_ref: str
    customer_phone: str
    amount: float
    currency: str
    reason_raw: str
    reason_category: str
    created_at: str
    latent: dict = field(default_factory=dict)


def _weighted_choice(weights: dict[str, int], rng: random.Random) -> str:
    keys = list(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def _fake_phone(rng: random.Random) -> str:
    """Masked, non-real Indian-style mobile."""
    start = rng.choice("6789")
    return f"{start}{rng.randint(10000, 99999)}**{rng.randint(10, 99)}"


def _make_latent(reason: str, rng: random.Random) -> dict:
    recoverable = rng.random() < RECOVERABLE_PROB[reason]
    responds = rng.random() < 0.40

    intent, reply_text = "none", None
    promise_offset_days = keeps_promise = None

    if responds:
        intent = rng.choices(
            ["promise_to_pay", "pay_now", "already_paid", "opt_out", "dispute"],
            weights=[48, 28, 10, 10, 4],
            k=1,
        )[0]
        if intent == "promise_to_pay":
            promise_offset_days = rng.randint(3, 10)
            day = (datetime.now(timezone.utc) + timedelta(days=promise_offset_days)).strftime("%d %b")
            reply_text = rng.choice(HINGLISH_REPLIES[intent]).format(day=day)
            keeps_promise = rng.random() < 0.65
        else:
            reply_text = rng.choice(HINGLISH_REPLIES[intent])

    # Salary-day cluster: most Indian salaries land on the 1st or month-end.
    salary_day = (
        rng.choice([1, 1, 1, 1, 5, 7, 10, 30, 30, 31])
        if reason == "insufficient_funds"
        else None
    )

    return {
        "recoverable": recoverable,
        "correct_strategy": CORRECT_STRATEGY[reason],
        "responds_to_outreach": responds,
        "reply_intent": intent,
        "reply_text_hinglish": reply_text,
        "promise_offset_days": promise_offset_days,
        "keeps_promise": keeps_promise,
        "salary_day": salary_day,
        # how long the transient condition lasts (bank downtime / low balance)
        "resolves_after_days": rng.randint(1, 3) if reason == "bank_downtime" else None,
        "prefers_voice": rng.random() < 0.30,
    }


def generate(n: int, seed: int, batch_id: str, checkout_share: float = 0.25) -> list[dict]:
    rng = random.Random(seed)
    now = datetime.now(timezone.utc)
    out: list[Case] = []

    for _ in range(n):
        is_checkout = rng.random() < checkout_share
        source = "checkout" if is_checkout else "subscription"
        reason = "checkout_dropoff" if is_checkout else _weighted_choice(SUBSCRIPTION_REASONS, rng)

        out.append(
            Case(
                id=f"case_{uuid.uuid4().hex[:12]}",
                batch_id=batch_id,
                source=source,
                customer_ref=f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
                customer_phone=_fake_phone(rng),
                amount=float(rng.choice(AMOUNTS)),
                currency="INR",
                reason_raw=rng.choice(RAW_REASONS[reason]),
                reason_category=reason,
                created_at=(now - timedelta(hours=rng.randint(0, 72))).isoformat(),
                latent=_make_latent(reason, rng),
            )
        )
    return [asdict(c) for c in out]


def summarise(cases: list[dict], label: str) -> None:
    reasons = Counter(c["reason_category"] for c in cases)
    recoverable = sum(1 for c in cases if c["latent"]["recoverable"])
    promises = sum(1 for c in cases if c["latent"]["reply_intent"] == "promise_to_pay")
    at_risk = sum(c["amount"] for c in cases)
    ceiling = sum(c["amount"] for c in cases if c["latent"]["recoverable"])

    print(f"\n[{label}] {len(cases)} cases")
    print(f"  reasons           : {dict(reasons)}")
    print(f"  recoverable       : {recoverable}/{len(cases)}")
    print(f"  will promise      : {promises}")
    print(f"  Rs at risk        : {at_risk:,.0f}")
    print(f"  Rs ceiling (max)  : {ceiling:,.0f}")


SETS = {
    # label:   (n,   seed,      filename)
    "dev":     (100, 42,        "data/cases_dev.json"),
    "holdout": (300, 20260905,  "data/cases_holdout.json"),
}


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate synthetic at-risk cases for Wapsi.")
    ap.add_argument("--only", choices=["dev", "holdout"], help="generate just one set")
    ap.add_argument("--checkout-share", type=float, default=0.25)
    args = ap.parse_args()

    targets = {args.only: SETS[args.only]} if args.only else SETS

    for label, (n, seed, path) in targets.items():
        cases = generate(n, seed, batch_id=label, checkout_share=args.checkout_share)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cases, f, ensure_ascii=False, indent=2)
        summarise(cases, label)
        print(f"  -> {path}")

    print("\nReminder: tune on dev. Run holdout ONCE, at the end, and report those numbers.")


if __name__ == "__main__":
    main()
