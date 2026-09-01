"""
Batch metrics -- every metric in PRD.md Sec.4, plus the honest exception list.

This module never reads case["latent"] -- only app/detection/batch_scanner.py
may. The pieces that genuinely need ground truth (the naive-baseline
comparison and the recoverable ceiling) are computed there and handed in as
plain dicts; this module only aggregates already-simulated case data plus the
rows batch_scanner gathered from the repository (payment_attempts, outreach,
promises, audit_log).

Sec 4.1 Primary:
  recovery_rate_count / recovery_rate_value
  recovery_lift  = ours vs naive_baseline (single immediate retry, no timing
                   intelligence, no voice, no promises)
  ceiling_capture = recovered / recoverable_ceiling
Sec 4.2 Operational:
  kept_promise_rate, false_escalation_rate, avg_time_to_recovery_days,
  interventions_per_recovery, cost_per_recovered_rupee, contact_efficiency,
  recovery_by_reason
Sec 4.3 Governance & safety -- each MUST be 0. Computed from actual rows, not
  asserted, so a real regression shows up here rather than being asserted
  away:
  gate_block_counts, double_charge_incidents, post_opt_out_contacts,
  actions_without_audit, over_cap_discounts
Sec 4.4 Honesty artifacts:
  exception_list -- every unrecovered case, with reason/state/amount.
  worst_three_reasons -- the 3 categories we're worst at, with the why.

Always report dev-set and HOLDOUT numbers separately. Headline numbers come
from the holdout, which must never be used for tuning.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _assert_ceiling_not_exceeded(ceiling_capture: float, recovered_value: float, recoverable_value: float) -> None:
    """
    ceiling_capture > 100% is not a policy violation to report and move on
    from (unlike the Sec 4.3 safety invariants below, which measure whether
    something the pipeline is supposed to prevent actually happened) -- it's
    a mathematical impossibility given correct bookkeeping: recovered cases
    are by definition a subset of the recoverable ones, so recovered_value
    can never exceed recoverable_value. A violation means either the
    simulator recovered a case whose latent["recoverable"] was False (see
    app/detection/batch_scanner.py's _maybe_route_reply and the promise
    is_paid check -- both must gate on "recoverable" the same way
    _matches_correct_strategy does), or this module and the ceiling
    calculation disagree on what they're summing. Either way, raise loudly
    right where it's computed rather than silently reporting a nonsensical
    number the dashboard would otherwise just print as-is.
    """
    if ceiling_capture > 1.0 + 1e-9:
        raise ValueError(
            f"ceiling_capture={ceiling_capture:.4f} exceeds 100%: recovered_value "
            f"(Rs {recovered_value:,.2f}) is greater than the recoverable ceiling "
            f"(Rs {recoverable_value:,.2f}). This is impossible by definition and indicates "
            "a bug upstream, not a real result -- fix the cause, don't suppress this check."
        )


def compute(
    cases: list[dict],
    *,
    naive: dict | None = None,
    ceiling: dict | None = None,
    gate_block_counts: dict[str, int] | None = None,
    attempts: list[dict] | None = None,
    outreach: list[dict] | None = None,
    promises: list[dict] | None = None,
    audit_rows: list[dict] | None = None,
    policy: dict | None = None,
) -> dict:
    """
    Aggregate metrics for an already-simulated batch.

    cases: one dict per case, post-simulation, WITHOUT "latent" -- the caller
           strips it before calling this.
    naive: {"recovered_count", "recovered_value", "total_count",
            "at_risk_value"} from batch_scanner.naive_baseline().
    ceiling: {"recoverable_count", "recoverable_value"} -- the theoretical
             max, from batch_scanner reading latent.
    gate_block_counts: {gate_id: count}, tallied by batch_scanner from each
                        action's result as the batch ran.
    attempts: every payment_attempts row for the batch.
    outreach: every outreach row for the batch (includes pre_debit_notice).
    promises: every promises row for the batch.
    audit_rows: every audit_log row for the batch's cases.
    policy: config.DEFAULT_POLICY (or a merchant override) -- read for
            cost_per_message_inr (M9) and max_discount_pct (M16).
    """
    attempts = attempts or []
    outreach = outreach or []
    promises = promises or []
    audit_rows = audit_rows or []
    policy = policy or {}

    total = len(cases)
    at_risk_value = sum(float(c.get("amount") or 0) for c in cases)

    recovered = [c for c in cases if c.get("state") == "RECOVERED"]
    recovered_count = len(recovered)
    recovered_value = sum(
        float(c.get("recovered_amount") or c.get("amount") or 0) for c in recovered
    )

    recovery_rate_count = recovered_count / total if total else 0.0
    recovery_rate_value = recovered_value / at_risk_value if at_risk_value else 0.0

    naive_recovery_rate_count = None
    recovery_lift = None
    if naive and naive.get("total_count"):
        naive_recovery_rate_count = naive.get("recovered_count", 0) / naive["total_count"]
        if naive_recovery_rate_count:
            recovery_lift = (recovery_rate_count - naive_recovery_rate_count) / naive_recovery_rate_count

    ceiling_capture = None
    if ceiling and ceiling.get("recoverable_value"):
        ceiling_capture = recovered_value / ceiling["recoverable_value"]
        _assert_ceiling_not_exceeded(ceiling_capture, recovered_value, ceiling["recoverable_value"])

    by_reason: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "recovered": 0, "amount": 0.0, "recovered_amount": 0.0}
    )
    for c in cases:
        row = by_reason[c.get("reason_category") or "unknown"]
        row["count"] += 1
        row["amount"] += float(c.get("amount") or 0)
        if c.get("state") == "RECOVERED":
            row["recovered"] += 1
            row["recovered_amount"] += float(c.get("recovered_amount") or c.get("amount") or 0)
    recovery_by_reason = {
        reason: {**row, "rate": (row["recovered"] / row["count"]) if row["count"] else 0.0}
        for reason, row in by_reason.items()
    }

    exception_list = [
        {
            "case_id": c.get("id"),
            "reason_category": c.get("reason_category"),
            "state": c.get("state"),
            "amount": c.get("amount"),
            "attempts_made": c.get("attempts_made"),
        }
        for c in cases
        if c.get("state") != "RECOVERED"
    ]

    # ---- 4.2 operational ---------------------------------------------------
    kept_promise_rate = _kept_promise_rate(promises)
    false_escalation_rate = _false_escalation_rate(audit_rows)
    avg_time_to_recovery_days = _avg_time_to_recovery_days(recovered)
    interventions_per_recovery = _interventions_per_recovery(attempts, outreach, recovered_count)
    cost_per_recovered_rupee = _cost_per_recovered_rupee(outreach, policy, recovered_value)
    contact_efficiency = recovered_count / len(outreach) if outreach else None

    # ---- 4.3 safety invariants -- MUST be 0 --------------------------------
    double_charge_incidents = _double_charge_incidents(attempts)
    post_opt_out_contacts = _post_opt_out_contacts(outreach, audit_rows)
    actions_without_audit = _actions_without_audit(attempts, outreach, audit_rows)
    over_cap_discounts = _over_cap_discounts(audit_rows, policy)

    # ---- 4.4 honesty artifacts ----------------------------------------------
    worst_three_reasons = _worst_three_reasons(recovery_by_reason, cases, audit_rows)

    return {
        "total_cases": total,
        "at_risk_value": at_risk_value,
        "recovered_count": recovered_count,
        "recovered_value": recovered_value,
        "recovery_rate_count": recovery_rate_count,
        "recovery_rate_value": recovery_rate_value,
        "naive": naive,
        "naive_recovery_rate_count": naive_recovery_rate_count,
        "recovery_lift": recovery_lift,
        "ceiling": ceiling,
        "ceiling_capture": ceiling_capture,
        "recovery_by_reason": recovery_by_reason,
        "gate_block_counts": dict(gate_block_counts or {}),
        "exception_list": exception_list,
        # operational (4.2)
        "kept_promise_rate": kept_promise_rate,
        "false_escalation_rate": false_escalation_rate,
        "avg_time_to_recovery_days": avg_time_to_recovery_days,
        "interventions_per_recovery": interventions_per_recovery,
        "cost_per_recovered_rupee": cost_per_recovered_rupee,
        "contact_efficiency": contact_efficiency,
        # safety invariants (4.3) -- each MUST be 0
        "double_charge_incidents": double_charge_incidents,
        "post_opt_out_contacts": post_opt_out_contacts,
        "actions_without_audit": actions_without_audit,
        "over_cap_discounts": over_cap_discounts,
        # honesty artifacts (4.4)
        "worst_three_reasons": worst_three_reasons,
    }


# ---------------------------------------------------------------------------
# 4.2 operational
# ---------------------------------------------------------------------------

def _kept_promise_rate(promises: list[dict]) -> float | None:
    counts = Counter(p.get("status") for p in promises)
    kept, broken = counts.get("kept", 0), counts.get("broken", 0)
    return kept / (kept + broken) if (kept + broken) else None


def _false_escalation_rate(audit_rows: list[dict]) -> float | None:
    """escalated cases that later self-resolved (a RECOVERED audit row after
    the ESCALATED one for the same case) / escalated cases."""
    events_by_case: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for row in audit_rows:
        case_id = row.get("case_id")
        if case_id is None:
            continue
        events_by_case[case_id][row.get("event_type")].append(str(row.get("ts") or ""))

    escalated_cases = [cid for cid, events in events_by_case.items() if events.get("ESCALATED")]
    if not escalated_cases:
        return None

    self_resolved = 0
    for cid in escalated_cases:
        escalated_ts = min(events_by_case[cid]["ESCALATED"])
        recovered_ts_list = events_by_case[cid].get("RECOVERED") or []
        if any(ts > escalated_ts for ts in recovered_ts_list):
            self_resolved += 1
    return self_resolved / len(escalated_cases)


def _avg_time_to_recovery_days(recovered: list[dict]) -> float | None:
    diffs = []
    for c in recovered:
        created = _parse_ts(c.get("created_at"))
        recovered_at = _parse_ts(c.get("recovered_at"))
        if created is not None and recovered_at is not None:
            diffs.append((recovered_at - created).total_seconds() / 86400)
    return sum(diffs) / len(diffs) if diffs else None


def _interventions_per_recovery(attempts: list[dict], outreach: list[dict], recovered_count: int) -> float | None:
    if not recovered_count:
        return None
    return (len(attempts) + len(outreach)) / recovered_count


def _cost_per_recovered_rupee(outreach: list[dict], policy: dict, recovered_value: float) -> float | None:
    if not recovered_value:
        return None
    cost_map = policy.get("cost_per_message_inr", {})
    total_cost = sum(float(cost_map.get(o.get("channel"), 0.0)) for o in outreach)
    return total_cost / recovered_value


# ---------------------------------------------------------------------------
# 4.3 safety invariants -- each MUST be 0
# ---------------------------------------------------------------------------

def _double_charge_incidents(attempts: list[dict]) -> int:
    """payment_attempts grouped by idempotency_key where count > 1. Under
    correct operation the repository's own UNIQUE-key dedup (real Postgres
    constraint, or MemoryRepository's dict-keyed guard) never lets a second
    row land for the same key -- this stays 0 by construction, not by
    assertion. It only moves off 0 if that protection is ever bypassed."""
    key_counts = Counter(a.get("idempotency_key") for a in attempts if a.get("idempotency_key"))
    return sum(1 for _, count in key_counts.items() if count > 1)


def _opt_out_ts_by_case(audit_rows: list[dict]) -> dict[str, str]:
    """Earliest opt-out REPLY_RECEIVED timestamp per case."""
    ts_by_case: dict[str, str] = {}
    for row in audit_rows:
        if row.get("event_type") != "REPLY_RECEIVED" or row.get("decision") != "opt_out":
            continue
        case_id = row.get("case_id")
        ts = str(row.get("ts") or "")
        if not case_id or not ts:
            continue
        if case_id not in ts_by_case or ts < ts_by_case[case_id]:
            ts_by_case[case_id] = ts
    return ts_by_case


def _post_opt_out_contacts(outreach: list[dict], audit_rows: list[dict]) -> int:
    """outreach rows with sent_at later than that case's opt-out timestamp."""
    opt_out_ts = _opt_out_ts_by_case(audit_rows)
    count = 0
    for row in outreach:
        ts = opt_out_ts.get(row.get("case_id"))
        if ts and str(row.get("sent_at") or "") > ts:
            count += 1
    return count


def _actions_without_audit(attempts: list[dict], outreach: list[dict], audit_rows: list[dict]) -> int:
    """executed actions (payment_attempts + outreach) with no corresponding
    ACTED / OUTREACH_SENT audit row for that case. Compared per case: if a
    case has more attempts than ACTED rows (or more outreach than
    OUTREACH_SENT rows), the shortfall is counted -- a case with 3 attempts
    but only 1 ACTED row contributes 2, not just a boolean "missing"."""
    acted_counts = Counter(r.get("case_id") for r in audit_rows if r.get("event_type") == "ACTED")
    outreach_sent_counts = Counter(r.get("case_id") for r in audit_rows if r.get("event_type") == "OUTREACH_SENT")
    attempt_counts = Counter(a.get("case_id") for a in attempts)
    outreach_counts = Counter(o.get("case_id") for o in outreach)

    missing = 0
    for case_id, n in attempt_counts.items():
        missing += max(0, n - acted_counts.get(case_id, 0))
    for case_id, n in outreach_counts.items():
        missing += max(0, n - outreach_sent_counts.get(case_id, 0))
    return missing


def _over_cap_discounts(audit_rows: list[dict], policy: dict) -> int:
    """Any GATE_ALLOW row (an action that actually went through, not one G6
    blocked) whose recorded discount_pct exceeds policy's cap. Should always
    be 0 -- G6 exists precisely to stop this before it happens; this metric
    proves it, from the actual gate audit trail, rather than asserting it."""
    max_discount = float(policy.get("max_discount_pct", 10.0))
    count = 0
    for row in audit_rows:
        if row.get("event_type") != "GATE_ALLOW":
            continue
        discount = (row.get("input") or {}).get("discount_pct")
        if discount is not None and float(discount) > max_discount:
            count += 1
    return count


# ---------------------------------------------------------------------------
# 4.4 honesty artifacts -- failure analysis
# ---------------------------------------------------------------------------

def _gate_id_from_decision(decision: str | None) -> str:
    """audit_log.gate() writes decision="BLOCK (G3)" for a block -- pull the
    gate id back out."""
    decision = decision or ""
    if "(" in decision and decision.endswith(")"):
        return decision[decision.index("(") + 1: -1]
    return decision


def _per_reason_gate_counts(cases: list[dict], audit_rows: list[dict]) -> dict[str, Counter]:
    """
    Distinct UNRECOVERED cases blocked by each gate, per reason category --
    NOT raw GATE_BLOCK row counts. A case sitting on a live promise-to-pay is
    correctly re-blocked by G10 once per day it's due for re-attempt (G10
    doing exactly its job: pausing outreach while the promise is live), which
    racks up many repeat GATE_BLOCK rows for that ONE case. Counting rows
    would let that single case's routine daily re-blocking swamp a gate like
    G3 or G5 that fires once per case and immediately ends it -- inflating a
    gate that's mostly cosmetic noise over the one actually deciding most
    cases' fate. Counting distinct cases, and only among the ones that never
    recovered, fixes both: the "dominant" gate reflects how many cases it
    actually stopped, not how many times it happened to reprocess one.
    """
    reason_by_case = {c.get("id"): c.get("reason_category") or "unknown" for c in cases}
    unrecovered_ids = {c.get("id") for c in cases if c.get("state") != "RECOVERED"}
    seen: set[tuple[Any, str]] = set()
    counts: dict[str, Counter] = defaultdict(Counter)
    for row in audit_rows:
        if row.get("event_type") != "GATE_BLOCK":
            continue
        case_id = row.get("case_id")
        if case_id not in unrecovered_ids:
            continue
        gate_id = _gate_id_from_decision(row.get("decision"))
        key = (case_id, gate_id)
        if key in seen:
            continue
        seen.add(key)
        counts[reason_by_case.get(case_id, "unknown")][gate_id] += 1
    return counts


def _dominant_failure_mode(reason: str, cases: list[dict], gate_counts: Counter) -> str:
    if gate_counts:
        gate_id, _ = gate_counts.most_common(1)[0]
        return gate_id
    states = Counter(
        c.get("state") for c in cases
        if (c.get("reason_category") or "unknown") == reason and c.get("state") != "RECOVERED"
    )
    if states:
        state, _ = states.most_common(1)[0]
        return state
    return "unknown"


def _worst_three_reasons(recovery_by_reason: dict[str, dict], cases: list[dict], audit_rows: list[dict]) -> list[dict]:
    """The 3 reason categories with the lowest recovery rate, each with
    count, rupees lost, and the dominant blocking gate or failure mode."""
    gate_counts = _per_reason_gate_counts(cases, audit_rows)
    candidates = [(reason, row) for reason, row in recovery_by_reason.items() if row["count"] > 0]
    candidates.sort(key=lambda item: item[1]["rate"])

    return [
        {
            "reason_category": reason,
            "count": row["count"],
            "recovery_rate": row["rate"],
            "rupees_lost": row["amount"] - row["recovered_amount"],
            "dominant_failure_mode": _dominant_failure_mode(reason, cases, gate_counts.get(reason, Counter())),
        }
        for reason, row in candidates[:3]
    ]


def export_snapshot(data: dict, path: str) -> None:
    """JSON fallback for the dashboard -- write `data` (typically compute()'s
    return value) to `path`."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
