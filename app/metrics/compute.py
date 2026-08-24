"""
Batch metrics + the honest exception list.

Primary:
  recovery_rate_count / recovery_rate_value
  recovery_lift  = ours vs naive_baseline (single immediate retry, no timing
                   intelligence, no voice, no promises)
Secondary:
  kept_promise_rate, false_escalation_rate, avg_time_to_recovery_days,
  interventions_per_recovery, cost_per_recovered_rupee,
  recovery_by_reason (a table: which failures we are good/bad at),
  gate_block_counts (how often each governance gate fired)
Honesty:
  exception_list -- every unrecovered case grouped by reason WITH the why.
  recoverable_ceiling -- from latent ground truth; report ours against it.

Always report dev-set and HOLDOUT numbers separately. Headline numbers come
from the holdout, which must never be used for tuning.
"""
from __future__ import annotations

# TODO: def compute(batch_id: str) -> dict
# TODO: def naive_baseline(cases: list[dict]) -> dict
# TODO: def export_snapshot(batch_id: str, path: str) -> None  # JSON fallback for the dashboard
