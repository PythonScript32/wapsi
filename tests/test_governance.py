"""
Start here. The governance gate is the most important module in the repo and
the easiest to test in isolation (pure policy logic, no network).

Each test below maps to a gate in app/governance/policy_gate.py.
"""

# TODO: G1 a RECOVERED case is never charged again
# TODO: G2 an opted-out case blocks every outreach and every charge
# TODO: G3 attempts beyond the per-reason cap are blocked and escalate
# TODO: G4 contacting inside min_contact_gap_hours is blocked
# TODO: G5 a case past grace_period_days closes as CLOSED_LOST
# TODO: G6 a discount above max_discount_pct is blocked
# TODO: G7 an amount above max_exposure_inr requires human approval
# TODO: G8 a charge without an idempotency key is blocked
# TODO: G9 a mandate debit without a 24h pre-debit notice is blocked
# TODO: an unevaluable check fails CLOSED (blocks), never opens
