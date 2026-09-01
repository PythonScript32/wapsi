"""
Scheduler -- fires time-based work: due retries, due promises, follow-ups
(PRD.md Sec.9 architecture diagram: "Scheduler (loops back)").

ONE tick(), TWO drivers:
  - SimulatedClock + app.detection.batch_scanner: advances day by day so a
    14-day recovery sequence evaluates in seconds. batch_scanner calls
    Scheduler.tick(clock.now) once per simulated day.
  - APScheduler (this module's --daemon CLI): calls Scheduler.tick(now) every
    SCHEDULER_INTERVAL_MINUTES against the real wall clock.

Both call the exact same Scheduler.tick() method -- that's what makes "live
and batch can never diverge" true by construction, not by discipline: there
is only one implementation of "what happens on a tick," and it lives here.

WHAT'S SHARED vs WHAT ISN'T
----------------------------
Shared (this module): diagnose-if-needed, decide, gate-check, execute the
outreach/charge attempt, resolve due promises, advance case state. None of
this reads case["latent"] -- it decides exactly as it would in production.

NOT shared, by design: whether an executed action actually recovered the
money. In a batch run that's decided by hidden ground truth
(case["latent"]) -- only app/detection/batch_scanner.py may read that field,
so batch_scanner injects an `on_action_executed` callback that does the
grading. In live mode there is no such callback: a real charge's outcome
arrives later via a Razorpay webhook (a separate sensor -- see the PRD's
architecture diagram), not synchronously from tick() itself. Passing
on_action_executed=None (live mode's default) leaves an executed case in
whatever state actions.py's own _NEXT_STATE mapping puts it, exactly the
same as batch would for any case tick() couldn't grade.

Similarly, resolving a due promise needs a paid/not-paid verdict this module
must never determine from latent -- promise_is_paid is the same kind of
injected callback (batch supplies one reading latent; live mode's default,
straight from app.promises.tracker.resolve_due_promises, checks whether the
case is already RECOVERED).

sweep_unresolved() is deliberately NOT part of tick(): "no case may still be
active when the batch's fixed observation window ends" is a batch-only
concept (a live scheduler has no horizon -- cases just keep ticking until a
governance gate closes them). batch_scanner calls it once, after the day
loop, exactly as before.

Usage:
    python -m app.scheduler.jobs --once      # one live tick against real
                                              # Supabase, then exit
    python -m app.scheduler.jobs --daemon    # runs forever, one tick every
                                              # SCHEDULER_INTERVAL_MINUTES
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app import config
from app.audit import log as audit_log
from app.db import repository
from app.decision import engine
from app.diagnosis import classifier
from app.execution import actions
from app.promises import tracker

_ACTOR = "scheduler.jobs"
TERMINAL_STATES = {"RECOVERED", "CLOSED_LOST", "ESCALATED"}


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


# ---------------------------------------------------------------------------
# SimulatedClock -- extracted from app/detection/batch_scanner.py, unchanged.
# ---------------------------------------------------------------------------

@dataclass
class SimulatedClock:
    """Fast-forwards days so a 14-day recovery sequence evaluates in seconds.
    Used only by batch_scanner: a live scheduler advances against real time
    instead (datetime.now(timezone.utc), driven by APScheduler)."""

    now: datetime

    def advance(self, days: int = 1) -> None:
        self.now += timedelta(days=days)


# ---------------------------------------------------------------------------
# Scheduler -- the shared tick() both modes drive
# ---------------------------------------------------------------------------

OnActionExecuted = Callable[[dict, dict, datetime], bool]
PromiseIsPaid = Callable[[dict], bool]


@dataclass
class Scheduler:
    """
    Owns the in-process scheduling state for a set of cases: which one is
    next due, what decision it's currently pursuing while it waits on a
    mandate-debit notice/schedule, and the running gate-block tally.

    cases: dict[case_id, case_row]. The SAME dict object the caller holds --
    tick() mutates entries in place, so a caller's own reference (e.g.
    batch_scanner's `cases`) always reflects the current state without any
    extra syncing.

    live: passed straight through to app.execution.actions.execute(...,
    live=...) -- whether to hit the real Razorpay API. Independent of
    whether THIS Scheduler is being driven by a SimulatedClock or real time;
    a batch run can pass --live to hit real Razorpay while still simulating
    the clock, and the live daemon always passes live=True.

    on_action_executed(case, decision, now) -> bool: called once a money/
    outreach action has actually executed, to decide whether it recovered
    the case. None (the default, and always live mode's value) means "don't
    know yet" -- the case is left in its natural post-execution state for a
    later webhook or reply to resolve.

    promise_is_paid(promise) -> bool: passed straight through to
    app.promises.tracker.resolve_due_promises's is_paid callback. None (the
    default) uses tracker's own fallback: paid if the case is already
    RECOVERED.
    """

    cases: dict[str, dict]
    live: bool = False
    on_action_executed: OnActionExecuted | None = None
    promise_is_paid: PromiseIsPaid | None = None

    next_action_at: dict[str, datetime] = field(default_factory=dict)
    decision_cache: dict[str, dict] = field(default_factory=dict)
    gate_block_counts: Counter = field(default_factory=Counter)
    opt_out_gate_confirmed: set = field(default_factory=set)

    # -----------------------------------------------------------------
    # public entry point -- the ONE method both SimulatedClock (batch) and
    # APScheduler (live) call.
    # -----------------------------------------------------------------

    def tick(self, now: datetime) -> dict:
        """
        Process everything due at `now`:
          1. resolve promises whose promised_date has arrived
          2. run the pipeline step for each non-terminal, due case

        Returns a small summary ({"promises_resolved", "cases_processed",
        "active", "recovered"}) -- the same numbers batch_scanner's day loop
        used to compute inline for its progress line, now shared with the
        live CLI's own output.
        """
        promises_resolved = self._resolve_due_promises(now)

        processed = 0
        for case_id, case in list(self.cases.items()):
            if case["state"] in TERMINAL_STATES:
                continue
            if case.get("opted_out") and case_id in self.opt_out_gate_confirmed:
                continue
            due = self.next_action_at.get(case_id, now)
            if due.date() > now.date():
                continue
            was_opted_out = bool(case.get("opted_out"))
            self._process_case(case, now)
            processed += 1
            if was_opted_out:
                # opted_out was already true when _process_case ran, so
                # whatever gate_check it hit saw it and G2 fired -- this
                # case's confirming pass is done, skip it from now on.
                self.opt_out_gate_confirmed.add(case_id)

        active = sum(1 for c in self.cases.values() if c["state"] not in TERMINAL_STATES and not c.get("opted_out"))
        recovered = sum(1 for c in self.cases.values() if c["state"] == "RECOVERED")
        return {
            "promises_resolved": promises_resolved,
            "cases_processed": processed,
            "active": active,
            "recovered": recovered,
        }

    def load_new_cases(self, rows: list[dict]) -> int:
        """
        Live mode only: merge freshly-fetched non-terminal case rows into
        self.cases, skipping any case_id already tracked (its in-memory
        state -- attempts_made, decision_cache, etc. -- must not be clobbered
        by a snapshot re-fetched mid-run). New cases default to due
        immediately (next_action_at falls back to `now` in tick() when a
        case_id has no entry yet), same as a fresh batch's initial seeding.
        Returns how many were actually new.
        """
        added = 0
        for row in rows:
            case_id = row.get("id")
            if case_id and case_id not in self.cases:
                self.cases[case_id] = row
                added += 1
        return added

    def sweep_unresolved(self, now: datetime) -> int:
        """
        End-of-horizon sweep -- BATCH MODE ONLY, called once by
        app.detection.batch_scanner after its day loop finishes, never from
        tick(). A live scheduler has no fixed horizon: cases just keep
        ticking until a governance gate closes them. No case may finish a
        batch run in a non-terminal state; anything still active when the
        observation window runs out is closed as CLOSED_LOST, an artifact of
        the batch's finite horizon, not a governance verdict.
        """
        swept = 0
        for case_id, case in self.cases.items():
            if case["state"] in TERMINAL_STATES:
                continue
            prior_state = case["state"]
            repository.update_case(case_id, state="CLOSED_LOST")
            case["state"] = "CLOSED_LOST"
            audit_log.record(
                case_id, _ACTOR, audit_log.CLOSED_LOST,
                reasoning=(
                    f"Case was still {prior_state} when the batch's observation window ended "
                    f"on {now.date().isoformat()}, with no resolution in sight. Closing as lost "
                    "rather than reporting it as neither recovered nor actively pursued."
                ),
            )
            swept += 1
        return swept

    # -----------------------------------------------------------------
    # promise resolution
    # -----------------------------------------------------------------

    def _resolve_due_promises(self, now: datetime) -> int:
        results = tracker.resolve_due_promises(
            now.date().isoformat(), config.DEFAULT_POLICY, is_paid=self.promise_is_paid, now=now,
        )
        for result in results:
            case = self.cases.get(result["case_id"])
            if case is None:
                continue
            case["state"] = result["case_state"]
            if result["status"] == "kept":
                amount = float(case.get("amount") or 0)
                case["recovered_amount"] = amount
                case["recovered_at"] = now.isoformat()
        return len(results)

    # -----------------------------------------------------------------
    # per-case pipeline step
    # -----------------------------------------------------------------

    def _process_case(self, case: dict, now: datetime) -> None:
        """
        decision_cache holds the ONE decision a case is currently pursuing
        while it waits on a mandate-debit notice/schedule. Re-calling
        decide() on every waiting tick would be wrong: `now` advancing can
        shift a freshly-computed salary-day guess, turning "wait until day
        20" into a moving target that's never actually reached. The cache is
        cleared the moment the case's planned action is actually attempted,
        so the next wait (if any) starts from a fresh decision.
        """
        case_id = case["id"]
        policy = config.DEFAULT_POLICY

        if case["state"] == "DETECTED":
            category, _how = classifier.classify(case)
            case["reason_category"] = category
            case["state"] = "DIAGNOSED"
            repository.update_case(case_id, state="DIAGNOSED", reason_category=category)

        decision = self.decision_cache.get(case_id) or engine.decide(case, [], policy, now=now)
        scheduled = _parse_ts(decision.get("scheduled_for"))

        if decision.get("is_mandate_debit"):
            context = repository.gate_context(case_id)
            notice_at = _parse_ts(context.get("pre_debit_notice_at"))

            if notice_at is None:
                # Nothing sent yet: send it now, however far out the
                # scheduled charge is. It only needs to be aged by the
                # scheduled date, and sending it early never hurts.
                result = actions.execute(decision, case, policy, live=self.live, now=now)
                self._tally_gate(result)

                # The notice-send is itself a governed action: a G5 block
                # (case aged past the grace period) translates straight to
                # CLOSED_LOST instead of the generic "keep waiting" path.
                result_intervention = result.get("intervention")
                if result_intervention == "close_lost":
                    case["state"] = "CLOSED_LOST"
                    self.decision_cache.pop(case_id, None)
                    return

                retry_at = _parse_ts(result.get("retry_at"))
                self.next_action_at[case_id] = retry_at or (now + timedelta(days=1))
                case["state"] = "SCHEDULED"
                repository.update_case(case_id, state="SCHEDULED")
                self.decision_cache[case_id] = decision  # keep pursuing this same plan
                return

            notice_hours = int(policy.get("rbi_pre_debit_notice_hours", 24))
            notice_aged = (now - notice_at).total_seconds() / 3600 >= notice_hours
            schedule_due = scheduled is None or scheduled.date() <= now.date()

            if not (notice_aged and schedule_due):
                candidates = []
                if not notice_aged:
                    candidates.append(notice_at + timedelta(hours=notice_hours))
                if not schedule_due:
                    candidates.append(scheduled)
                self.next_action_at[case_id] = max(candidates)
                case["state"] = "SCHEDULED"
                repository.update_case(case_id, state="SCHEDULED")
                self.decision_cache[case_id] = decision  # keep pursuing this same plan
                return
            # both conditions satisfied -- fall through to the real charge attempt

        self.decision_cache.pop(case_id, None)  # this decision is being acted on now

        result = actions.execute(decision, case, policy, live=self.live, now=now)
        self._tally_gate(result)

        # What actually happened, not what the decision originally proposed:
        # a G3/G6-escalate or G5 block translates the intervention
        # actions.py returns, which can differ from `intervention` above
        # (e.g. a retry_after_date that got G3-blocked comes back as
        # "escalate").
        result_intervention = result.get("intervention")

        if result.get("escalated") or result_intervention == "escalate":
            case["state"] = "ESCALATED"
            return
        if result_intervention == "close_lost":
            case["state"] = "CLOSED_LOST"
            return

        if not result.get("executed") or result.get("reused"):
            self.next_action_at[case_id] = now + timedelta(days=1)
            return

        case["attempts_made"] = int(case.get("attempts_made") or 0) + 1

        recovered = False
        if self.on_action_executed is not None:
            recovered = bool(self.on_action_executed(case, decision, now))

        if recovered:
            amount = float(case.get("amount") or 0)
            case["state"] = "RECOVERED"
            case["recovered_amount"] = amount
            case["recovered_at"] = now.isoformat()
            repository.mark_recovered(case_id, amount)
            audit_log.record(case_id, _ACTOR, audit_log.RECOVERED, reasoning=f"Recovered via {result_intervention}.")
            return

        if case["state"] in ("PROMISE_MADE", "ESCALATED"):
            return  # on_action_executed's own reply routing already moved it there

        case["state"] = actions._NEXT_STATE.get(result_intervention, case["state"])
        self.next_action_at[case_id] = now + timedelta(days=1)

    def _tally_gate(self, result: dict) -> None:
        gate = result.get("gate")
        if gate:
            self.gate_block_counts[gate] += 1


# ---------------------------------------------------------------------------
# live mode
# ---------------------------------------------------------------------------

def _load_active_cases(batch_id: str | None = None) -> dict[str, dict]:
    """Every non-terminal case from the real repository -- the live
    scheduler's starting point. Cases created by a Razorpay webhook (a
    separate sensor, not this module's concern) default to batch_id='live'
    (see app/db/memory_repository.py's _CASE_DEFAULTS); pass batch_id to
    scope a run to one merchant/dataset if it's ever needed."""
    rows = repository.list_cases(batch_id=batch_id, limit=10_000)
    return {r["id"]: r for r in rows if r.get("state") not in TERMINAL_STATES}


def make_live_scheduler(batch_id: str | None = None) -> Scheduler:
    """A Scheduler wired for real: real Razorpay (live=True), no outcome
    simulation (a real charge's result arrives later via webhook, not
    synchronously), promise verdicts from the case's real current state."""
    cases = _load_active_cases(batch_id)
    return Scheduler(cases, live=True, on_action_executed=None, promise_is_paid=None)


def run_once(batch_id: str | None = None) -> dict:
    """python -m app.scheduler.jobs --once: one live tick against real
    Supabase, then return. Used for manual testing and by the --once CLI."""
    repository.verify_schema()
    scheduler = make_live_scheduler(batch_id)
    now = datetime.now(timezone.utc)
    summary = scheduler.tick(now)
    summary["cases_loaded"] = len(scheduler.cases)
    summary["gate_block_counts"] = dict(scheduler.gate_block_counts)
    return summary


def run_daemon(interval_minutes: int | None = None, batch_id: str | None = None) -> None:
    """python -m app.scheduler.jobs --daemon: runs forever, one tick every
    `interval_minutes` (default config.SCHEDULER_INTERVAL_MINUTES) against
    real time. Blocks until interrupted (Ctrl+C)."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    repository.verify_schema()
    interval_minutes = interval_minutes or config.SCHEDULER_INTERVAL_MINUTES
    scheduler = make_live_scheduler(batch_id)

    def _tick_job() -> None:
        now = datetime.now(timezone.utc)
        added = scheduler.load_new_cases(list(_load_active_cases(batch_id).values()))
        summary = scheduler.tick(now)
        print(
            f"[{now.isoformat()}] tick: +{added} new case(s), "
            f"{summary['cases_processed']} processed, {summary['promises_resolved']} promise(s) resolved, "
            f"active={summary['active']} recovered={summary['recovered']} "
            f"gate_blocks={dict(scheduler.gate_block_counts)}",
            file=sys.stderr,
        )

    aps = BlockingScheduler(timezone=timezone.utc)
    aps.add_job(_tick_job, "interval", minutes=interval_minutes, next_run_time=datetime.now(timezone.utc))
    print(
        f"[scheduler.jobs] starting daemon: one tick every {interval_minutes} minute(s). "
        "Ctrl+C to stop.",
        file=sys.stderr,
    )
    try:
        aps.start()
    except (KeyboardInterrupt, SystemExit):
        print("[scheduler.jobs] stopped.", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Wapsi live scheduler -- fires due retries, promises, follow-ups.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="run a single live tick against real Supabase, then exit")
    mode.add_argument("--daemon", action="store_true", help="run continuously, one tick every --interval-minutes")
    ap.add_argument(
        "--interval-minutes", type=int, default=None,
        help=f"daemon tick interval (default: config.SCHEDULER_INTERVAL_MINUTES = {config.SCHEDULER_INTERVAL_MINUTES})",
    )
    ap.add_argument("--batch-id", default=None, help="only process cases with this batch_id (default: all)")
    args = ap.parse_args()

    if args.once:
        started = time.perf_counter()
        summary = run_once(batch_id=args.batch_id)
        elapsed = time.perf_counter() - started
        print(f"\n=== Wapsi scheduler: single tick ({elapsed:.2f}s) ===")
        print(f"  cases loaded       : {summary['cases_loaded']}")
        print(f"  cases processed    : {summary['cases_processed']}")
        print(f"  promises resolved  : {summary['promises_resolved']}")
        print(f"  active             : {summary['active']}")
        print(f"  recovered          : {summary['recovered']}")
        print(f"  gate blocks        : {summary['gate_block_counts']}")
        return 0

    run_daemon(interval_minutes=args.interval_minutes, batch_id=args.batch_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
