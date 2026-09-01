"""
Demo: graceful failure, not a silent drop.

Walks through, on the terminal, exactly what Wapsi does when Razorpay itself
won't cooperate -- a case, timeout after timeout, escalation, and proof that
none of it double-charges the customer. This is the "GRACEFUL FAILURE" path
documented in app/execution/actions.py:

    Razorpay timeout/5xx -> exponential backoff (1s, 4s, 10s) -> still
    failing -> mark the attempt 'pending' (NOT 'failed' -- we don't actually
    know), audit ERROR + ESCALATED with full case context, and set the case
    to ESCALATED. Never silently drop. Never double-charge: the idempotency
    key protects us even if an earlier call actually succeeded server-side.

Runs entirely against app/db/memory_repository.py -- no real Supabase, no
real Razorpay key needed (the client call is replaced outright), no network,
no side effects on any real system. The only "real" thing here is time: the
backoff actually sleeps the policy's configured delays (1s, 4s, 10s by
default) so what's on screen is the genuine timing, not a fake speedup.

Usage:
    python scripts/demo_graceful_failure.py
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Scripts run standalone (python scripts/demo_graceful_failure.py), so the
# repo root -- not just this file's directory -- needs to be on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.audit import log as audit_log  # noqa: E402
from app.db import repository  # noqa: E402
from app.db.memory_repository import MemoryRepository  # noqa: E402
from app.decision import engine  # noqa: E402
from app.execution import actions  # noqa: E402
from app.execution import razorpay_client  # noqa: E402

NOW = datetime(2026, 9, 2, 9, 0, 0, tzinfo=timezone.utc)

# Every function app.db.repository exposes -- swapping these for a
# MemoryRepository's bound methods is what makes actions.py, the decision
# engine's audit calls, and audit_log itself all run against plain dicts and
# lists instead of a real database. Mirrors app/detection/batch_scanner.py's
# own _repository_backend swap.
_REPOSITORY_FUNCTIONS = (
    "clear_batch", "insert_case", "upsert_case", "get_case", "list_cases", "update_case",
    "mark_recovered", "increment_attempts", "insert_attempt", "get_attempt_by_key",
    "attempts_for_case", "update_attempt", "insert_outreach", "last_outreach_at",
    "outreach_for_case", "record_reply", "insert_promise", "active_promise", "due_promises",
    "resolve_promise", "all_promises", "promises_for_case", "append_audit", "audit_for_case",
    "audit_by_event", "gate_context",
)


def _banner(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _step(n: int, title: str) -> None:
    print()
    print(f"--- Step {n}: {title} ---")


def _now_hms() -> str:
    return time.strftime("%H:%M:%S")


def main() -> int:
    backend = MemoryRepository()
    originals = {name: getattr(repository, name) for name in _REPOSITORY_FUNCTIONS}
    for name in _REPOSITORY_FUNCTIONS:
        setattr(repository, name, getattr(backend, name))

    real_create_payment_link = razorpay_client.create_payment_link
    real_sleep = time.sleep

    try:
        _run_demo()
    finally:
        for name, fn in originals.items():
            setattr(repository, name, fn)
        actions.razorpay_client.create_payment_link = real_create_payment_link
        actions.time.sleep = real_sleep

    return 0


def _run_demo() -> None:
    _banner("WAPSI -- graceful failure & escalation demo")
    print("Running against app/db/memory_repository.py: no real Supabase, no")
    print("real Razorpay key, no network. The only genuine thing here is time --")
    print("the backoff below actually sleeps the configured delays.")

    # -------------------------------------------------------------------
    # Step 1: set up a case and make a recovery decision
    # -------------------------------------------------------------------
    _step(1, "Set up a case and make a recovery decision")

    case = {
        "id": "case_demo_graceful_failure",
        "batch_id": "demo",
        "source": "checkout",
        "customer_ref": "Rohan Verma",
        "customer_phone": "98765**43",
        "amount": 799.0,
        "currency": "INR",
        "reason_raw": "Order created but not paid within window",
        "reason_category": "checkout_dropoff",
        "state": "OUTREACH_SENT",
        "attempts_made": 1,  # this will be the SECOND touch: send_link_with_offer
        "opted_out": False,
        "recovered_amount": 0.0,
        "recovered_at": None,
        "created_at": NOW.isoformat(),
    }
    repository.insert_case(case)
    print(f"  case          : {case['id']}  ({case['customer_ref']}, Rs {case['amount']:,.0f})")
    print(f"  reason        : {case['reason_category']}  (checkout abandoned, already touched once)")

    decision = engine.decide(case, [], config.DEFAULT_POLICY, now=NOW)
    print(f"  decision      : {decision['intervention']}  (a Rs {case['amount']:,.0f} payment link with a "
          f"{decision['discount_pct']:.0f}% offer -- a real money action, so Razorpay is called)")
    print(f"  reasoning     : {decision['reasoning']}")

    # -------------------------------------------------------------------
    # Step 2: force the Razorpay call to fail
    # -------------------------------------------------------------------
    _step(2, "Force the Razorpay call to fail (inject a client that times out)")

    delays = config.DEFAULT_POLICY.get("action_retry_delays_s", [1, 4, 10])
    total_wait = sum(delays)
    print(f"  policy.action_retry_delays_s = {delays}  --  {len(delays)} attempts, "
          f"~{total_wait:.0f}s of real backoff below. Watch the clock on each line.")

    attempt_counter = {"n": 0}

    def failing_create_payment_link(amount, customer, idempotency_key, *, purpose="payment"):
        attempt_counter["n"] += 1
        print(
            f"  [{_now_hms()}] [razorpay] attempt {attempt_counter['n']}/{len(delays)} "
            f"(idempotency_key={idempotency_key}) -> TimeoutError: Razorpay did not respond (simulated)"
        )
        raise TimeoutError("Simulated Razorpay network timeout for this demo")

    real_sleep = time.sleep

    def narrated_sleep(seconds: float) -> None:
        if seconds:
            print(f"  [{_now_hms()}] ... backing off {seconds}s before the next attempt (exponential backoff) ...")
        real_sleep(seconds)

    actions.razorpay_client.create_payment_link = failing_create_payment_link
    actions.time.sleep = narrated_sleep

    # -------------------------------------------------------------------
    # Step 3 + 4: run the action for real -- backoff retries, then escalate
    # -------------------------------------------------------------------
    _step(3, "Run the action: exponential backoff retries, then escalation")

    result = actions.execute(decision, case, config.DEFAULT_POLICY, live=True, now=NOW)

    actions.razorpay_client.create_payment_link = razorpay_client.create_payment_link
    actions.time.sleep = real_sleep

    print()
    print(f"  execute() returned: executed={result.get('executed')}  escalated={result.get('escalated')}")
    print(f"  reason            : {result.get('reason')}")

    _step(4, "What actually happened after the final failure")

    updated_case = repository.get_case(case["id"])
    print(f"  case state       : {updated_case['state']}  (escalated to a human, not silently dropped)")

    attempts = repository.attempts_for_case(case["id"])
    attempt = attempts[0] if attempts else None
    if attempt is not None:
        print(f"  attempt result   : {attempt['result']!r}  "
              f"(NOT 'failed' -- Razorpay may have gone through server-side; we genuinely don't know)")
        print(f"  idempotency_key  : {attempt['idempotency_key']}")

    # -------------------------------------------------------------------
    # Step 5: print the full audit trail for this case
    # -------------------------------------------------------------------
    _step(5, "Full audit trail for this case")
    audit_log.print_trail(case["id"])

    # -------------------------------------------------------------------
    # Step 6: prove no double charge
    # -------------------------------------------------------------------
    _step(6, "Prove no double charge")

    print(f"  Razorpay was called {attempt_counter['n']} times (every one failed).")
    print(f"  payment_attempt rows for this case: {len(attempts)}")
    if attempt is not None:
        print(f"  idempotency key on that one row   : {attempt['idempotency_key']}")
    if len(attempts) == 1:
        print("  PASS -- exactly one attempt row exists. The idempotency key made every retry")
        print("          reuse the same row instead of inserting a new one, so even if one of")
        print("          those 3 failed calls actually succeeded server-side, Razorpay would")
        print("          reject a repeat with the same key rather than charging twice.")
    else:
        print(f"  FAIL -- expected exactly 1 attempt row, found {len(attempts)}. This should never happen.")

    _banner("Demo complete. Nothing above touched a real database or a real payment gateway.")


if __name__ == "__main__":
    raise SystemExit(main())
