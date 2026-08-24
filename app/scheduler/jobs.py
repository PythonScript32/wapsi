"""
Scheduler -- fires time-based work: due retries, due promises, follow-ups.

Two modes:
  - live:  APScheduler, runs every N minutes against real timestamps.
  - batch: a simulated clock that fast-forwards days so a 14-day recovery
           sequence can be evaluated in seconds. The batch runner uses this.

The simulated clock is what makes measurable batch metrics possible at all --
we cannot wait real weeks for salary-day retries to land.
"""
from __future__ import annotations

# TODO: def start_scheduler() -> None
# TODO: def tick(now) -> None          # process everything due at `now`
# TODO: class SimulatedClock: advance(days)
