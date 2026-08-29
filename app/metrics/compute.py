"""
Batch metrics + the honest exception list.

This module never reads case["latent"] — only app/detection/batch_scanner.py
may. The pieces that genuinely need ground truth (the naive-baseline
comparison and the recoverable ceiling) are computed there and handed in as
plain dicts; this module only aggregates already-simulated case data.

Primary:
  recovery_rate_count / recovery_rate_value
  recovery_lift  = ours vs naive_baseline (single immediate retry, no timing
                   intelligence, no voice, no promises)
  ceiling_capture = recovered / recoverable_ceiling
Governance:
  gate_block_counts (how often each governance gate fired)
Honesty:
  exception_list -- every unrecovered case, with reason/state/amount.
  recovery_by_reason -- per-category recovery rate (which failures we're
                        good/bad at).

Always report dev-set and HOLDOUT numbers separately. Headline numbers come
from the holdout, which must never be used for tuning.
"""
from __future__ import annotations

import json
from collections import defaultdict


def compute(
    cases: list[dict],
    *,
    naive: dict | None = None,
    ceiling: dict | None = None,
    gate_block_counts: dict[str, int] | None = None,
) -> dict:
    """
    Aggregate metrics for an already-simulated batch.

    cases: one dict per case, post-simulation, WITHOUT "latent" — the caller
           strips it before calling this.
    naive: {"recovered_count", "recovered_value", "total_count",
            "at_risk_value"} from batch_scanner.naive_baseline().
    ceiling: {"recoverable_count", "recoverable_value"} — the theoretical
             max, from batch_scanner reading latent.
    gate_block_counts: {gate_id: count}, tallied by batch_scanner from each
                        action's result as the batch ran.
    """
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
    }


def export_snapshot(data: dict, path: str) -> None:
    """JSON fallback for the dashboard — write `data` (typically compute()'s
    return value) to `path`."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
