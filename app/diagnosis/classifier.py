"""
Diagnosis: map a raw gateway failure reason (or abandonment context) to one
canonical category.

Categories:
  insufficient_funds | expired_card | mandate_revoked | bank_downtime |
  technical_other | checkout_dropoff

GOLDEN RULE: the Razorpay reason code/description is ground truth. Rule-based
mapping runs FIRST. The LLM is only consulted when the rules cannot classify,
and its answer is constrained to the enum above. The LLM must never invent a
reason that contradicts the code.

Always log: raw reason in, category out, and which path decided (rule vs llm).
"""
from __future__ import annotations

# TODO: RULES: dict[str, str]  # substring/code -> category
# TODO: def classify(case: dict) -> tuple[str, str]  # (category, how)
