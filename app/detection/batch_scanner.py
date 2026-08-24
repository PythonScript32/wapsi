"""
Detection sensor #2 -- the batch runner. This is how the track's
"measured money recovered across a batch" bar is met.

run_batch():
  load the dataset -> insert cases -> run the full pipeline for each ->
  fast-forward the simulated clock day by day so scheduled retries and promised
  dates actually resolve -> compute metrics -> export a JSON snapshot.

The outcome simulator lives here too: it reads case['latent'] (hidden ground
truth) to decide whether an executed action ACTUALLY recovers the money.
This is the ONLY module allowed to read `latent`. The pipeline decides blind.

Recovery rule of thumb: an action recovers if the case is recoverable AND the
chosen strategy matches latent['correct_strategy'] (and, for promises, the
customer keeps it). Timing matters: an insufficient_funds retry before the
salary day should fail.
"""
from __future__ import annotations

# TODO: def run_batch(path: str, batch_id: str, horizon_days: int = 21) -> dict
# TODO: def _simulate_outcome(case: dict, decision: dict, clock) -> bool
