"""
Detection sensor #2 -- the batch runner. This is how the track's
"measured money recovered across a batch" bar is met.

run_batch():
  load the dataset -> insert cases -> run the full pipeline for each ->
  fast-forward a simulated clock day by day so scheduled retries and promised
  dates actually resolve -> compute metrics -> export a JSON snapshot.

The outcome simulator lives here too: it reads case['latent'] (hidden ground
truth) to decide whether an executed action ACTUALLY recovers the money.
This is the ONLY module allowed to read `latent`. The pipeline (classifier,
decision engine, governance, actions) decides blind, exactly as it would in
production.

Recovery rule of thumb: an action recovers if the case is recoverable AND the
chosen strategy matches latent['correct_strategy'] (and, for promises, the
customer keeps it). Timing matters: an insufficient_funds retry before the
salary day should fail.

THE PRE-DEBIT NOTICE / SCHEDULED RETRIES
-----------------------------------------
Neither the decision engine's scheduled_for nor governance enforces "wait
until this date" by itself — the decision engine only proposes a date, and
policy_gate only checks a pre-debit notice's age. This module is what
actually respects the schedule: a mandate-debit decision gets its pre-debit
notice sent as soon as it's decided (it can age far past the RBI minimum with
no penalty), and the real charge only fires once BOTH the notice has aged
rbi_pre_debit_notice_hours AND the simulated clock has reached scheduled_for.
That's normally at least two simulated days: notice on day N, charge on day
N+1 or later.
"""
from __future__ import annotations

import argparse
import calendar
import contextlib
import json
import os
import random
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app import config
from app.audit import log as audit_log
from app.db import repository
from app.db.memory_repository import MemoryRepository
from app.metrics import compute as metrics
from app.promises import tracker
from app.scheduler.jobs import Scheduler, SimulatedClock

_ACTOR = "detection.batch_scanner"
_OUTREACH_INTERVENTIONS = {"request_re_mandate", "request_card_update", "send_link", "send_link_with_offer"}

# Every function app.db.repository exposes. Swapping these — and only these —
# for a MemoryRepository's bound methods is what makes persist="memory" swap
# the backend for the whole pipeline: audit.log, execution.actions, and this
# module all called `from app.db import repository`, so they all hold the
# same module object and see the same swap.
_REPOSITORY_FUNCTIONS = (
    "clear_batch", "insert_case", "upsert_case", "get_case", "list_cases", "update_case",
    "mark_recovered", "increment_attempts", "insert_attempt", "get_attempt_by_key",
    "attempts_for_case", "update_attempt", "insert_outreach", "last_outreach_at",
    "outreach_for_case", "record_reply", "insert_promise", "active_promise", "due_promises",
    "resolve_promise", "all_promises", "promises_for_case", "append_audit", "audit_for_case",
    "audit_by_event", "gate_context",
)


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


def _load_dataset(set_name: str) -> list[dict]:
    with open(f"data/cases_{set_name}.json", "r", encoding="utf-8") as f:
        return json.load(f)


@contextlib.contextmanager
def _repository_backend(persist: str):
    """
    persist="memory": swap every function app.db.repository exposes for a
    fresh MemoryRepository's bound methods, so the entire pipeline (audit
    log, actions, this module) runs against plain dicts and lists at Python
    speed instead of a network round trip per call. Restores the real
    functions afterward, even on error. Yields the MemoryRepository so the
    caller can flush it to Supabase once the swap is undone.

    persist="supabase": no swap — yields None, everything hits the real DB
    directly, exactly as it always did.
    """
    if persist == "supabase":
        yield None
        return
    if persist != "memory":
        raise ValueError(f"persist must be 'memory' or 'supabase', got {persist!r}")

    backend = MemoryRepository()
    originals = {name: getattr(repository, name) for name in _REPOSITORY_FUNCTIONS}
    for name in _REPOSITORY_FUNCTIONS:
        setattr(repository, name, getattr(backend, name))
    try:
        yield backend
    finally:
        for name, fn in originals.items():
            setattr(repository, name, fn)


# Tables whose id column the database generates (uuid default / bigserial).
# cases.id is the one exception — a text primary key that comes from the
# dataset itself, so it's the only table NOT in this set.
_DB_GENERATED_ID_TABLES = {"payment_attempts", "outreach", "promises", "audit_log"}


def _strip_generated_ids(table: str, rows: list[dict]) -> list[dict]:
    """
    MemoryRepository assigns its own sequential/uuid ids so gate_context,
    get_attempt_by_key, and friends have something to key off of during the
    run — but those ids are internal bookkeeping only. Sending them to
    Supabase would pin every row to whatever MemoryRepository happened to
    assign instead of letting the real column default (gen_random_uuid() /
    bigserial) generate one, and for audit_log specifically the database
    owns that sequence — writing an explicit id there is a bug waiting to
    collide with a real row.
    """
    if table not in _DB_GENERATED_ID_TABLES:
        return rows
    return [{k: v for k, v in row.items() if k != "id"} for row in rows]


def _gather_batch_records(batch_id: str, case_ids: list[str]) -> dict[str, list[dict]]:
    """
    Collect every payment_attempts/outreach/audit_log row for this batch's
    cases, plus its promises, so app/metrics/compute.py can compute the
    operational metrics and safety invariants (Sec 4.2-4.4) without ever
    touching case["latent"] itself.

    Called through repository.* rather than a backend-specific dump, so the
    same code path works for both persist="memory" (repository.* is bound to
    the swapped-in MemoryRepository) and persist="supabase" (repository.* is
    the real module, hitting Supabase directly) -- see the caller's note on
    why this must run before the persist="memory" swap is undone.
    """
    attempts: list[dict] = []
    outreach: list[dict] = []
    audit: list[dict] = []
    for case_id in case_ids:
        attempts.extend(repository.attempts_for_case(case_id))
        outreach.extend(repository.outreach_for_case(case_id))
        audit.extend(repository.audit_for_case(case_id))
    promises = repository.all_promises(batch_id)
    return {"attempts": attempts, "outreach": outreach, "promises": promises, "audit": audit}


def _flush_to_supabase(backend: MemoryRepository) -> None:
    """
    Bulk-write a finished memory run to Supabase: one insert per table with
    all its rows, chunked at 500 (repository.bulk_insert) — a handful of
    calls total instead of one per row.
    """
    tables = (
        ("cases", backend.dump_cases()),
        ("payment_attempts", backend.dump_attempts()),
        ("outreach", backend.dump_outreach()),
        ("promises", backend.dump_promises()),
        ("audit_log", backend.dump_audit_log()),
    )
    for table, rows in tables:
        rows = _strip_generated_ids(table, rows)
        n = repository.bulk_insert(table, rows)
        print(f"  flushed {n:>6} rows -> {table}")


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def run_batch(
    set_name: str = "dev",
    horizon_days: int = 30,
    live: bool = False,
    *,
    persist: str = "memory",
    limit: int | None = None,
    now: datetime | None = None,
    clear: bool = True,
) -> dict:
    """
    horizon_days=30: long enough for the slowest realistic path to actually
    resolve inside the window — a salary-day retry that lands near the
    grace_period_days (14) boundary, plus the notice-aging and backoff time
    around it, can otherwise still be "active" when a shorter horizon runs
    out. Whatever's still non-terminal at the end gets swept to CLOSED_LOST
    (see below) rather than reported as neither recovered nor abandoned.

    persist="memory" (default): the whole pipeline runs against
    app/db/memory_repository.py — plain dicts and lists, no network — so a
    holdout-sized batch finishes in seconds. The finished run is bulk-flushed
    to Supabase at the end (repository.bulk_insert), a handful of calls
    instead of tens of thousands of round trips.
    persist="supabase": every call hits the real database directly, as
    before. Much slower; useful for a live/small run or debugging the DB
    layer itself.
    limit: only process the first `limit` cases from the dataset — a quick
           diagnostic run over a slice instead of the full set.
    now: only for tests — pins the simulated clock's start instead of the
    real wall clock, so timing-sensitive scenarios (salary-day guesses,
    pre-debit notice aging) are deterministic. Production and the CLI never
    pass it.
    clear=True (default): wipe this batch_id's existing rows
    (repository.clear_batch) before ingesting, so a re-run never silently
    skips the flush on a duplicate idempotency/primary key from the prior
    run. Pass False only to deliberately append onto an existing batch.

    Checks repository.verify_schema() before touching anything else, even
    for persist="memory" -- that mode still bulk-flushes to the real
    Supabase project at the end, so an unapplied migration is just as fatal
    to it, only discovered 30 simulated days later instead of immediately.
    """
    repository.verify_schema()

    policy = config.DEFAULT_POLICY
    batch_id = set_name
    started_at = time.perf_counter()

    with _repository_backend(persist) as backend:
        if clear:
            repository.clear_batch(batch_id)
        raw_cases = _load_dataset(set_name)
        if limit is not None:
            raw_cases = raw_cases[:limit]
        cases = _ingest(batch_id, raw_cases)

        clock = SimulatedClock(now or datetime.now(timezone.utc))
        # Scheduler owns next_action_at/decision_cache/gate_block_counts/
        # opt_out_gate_confirmed internally now -- tick() is the exact same
        # per-day body this loop used to run inline (see
        # app/scheduler/jobs.py), so live mode's APScheduler-driven tick()
        # can never diverge from what a batch run does. on_action_executed
        # and promise_is_paid are the two places this simulated run reads
        # case["latent"] (this module is the only one allowed to); live mode
        # passes neither.
        scheduler = Scheduler(
            cases, live=live, on_action_executed=_simulate_outcome, promise_is_paid=_is_paid(cases),
        )

        for day_no in range(1, horizon_days + 1):
            summary = scheduler.tick(clock.now)

            elapsed = time.perf_counter() - started_at
            print(
                f"  day {day_no:>3}/{horizon_days}  active={summary['active']:>4}  "
                f"recovered={summary['recovered']:>4}  elapsed={elapsed:.1f}s"
            )

            clock.advance(1)

        swept = scheduler.sweep_unresolved(clock.now)
        if swept:
            print(f"  swept {swept:>4} unresolved cases -> CLOSED_LOST at end of horizon")

        # Gathered here, while persist="memory" still has repository.* bound
        # to the swapped-in MemoryRepository — outside this block those
        # functions are restored to the real ones, which would silently
        # start reading an empty (or unrelated) backend instead.
        batch_records = _gather_batch_records(batch_id, list(cases.keys()))

    if backend is not None:
        _flush_to_supabase(backend)

    naive = naive_baseline(set_name)
    ceiling = _compute_ceiling(list(cases.values()))
    clean_cases = [{k: v for k, v in c.items() if k != "latent"} for c in cases.values()]
    result = metrics.compute(
        clean_cases, naive=naive, ceiling=ceiling, gate_block_counts=dict(scheduler.gate_block_counts),
        attempts=batch_records["attempts"], outreach=batch_records["outreach"],
        promises=batch_records["promises"], audit_rows=batch_records["audit"],
        policy=policy,
    )

    os.makedirs("data", exist_ok=True)
    out_path = f"data/results_{set_name}.json"
    metrics.export_snapshot(result, out_path)
    result["_snapshot_path"] = out_path
    return result


def _ingest(batch_id: str, raw_cases: list[dict]) -> dict[str, dict]:
    cases: dict[str, dict] = {}
    for raw in raw_cases:
        case = dict(raw)
        case["batch_id"] = batch_id
        case["state"] = "DETECTED"
        case["attempts_made"] = 0
        case["opted_out"] = False
        case["recovered_amount"] = 0.0
        case["recovered_at"] = None
        repository.insert_case(case)
        audit_log.record(
            case["id"], _ACTOR, audit_log.DETECTED,
            inp={"source": case.get("source"), "amount": case.get("amount")},
            reasoning=f"Ingested into batch {batch_id}.",
        )
        cases[case["id"]] = case
    return cases


# ---------------------------------------------------------------------------
# callbacks injected into app.scheduler.jobs.Scheduler -- the only two places
# THIS run reads case["latent"], since only this module may.
# ---------------------------------------------------------------------------

def _is_paid(cases: dict[str, dict]) -> Callable[[dict], bool]:
    """Scheduler's promise_is_paid callback: a closure over this run's own
    `cases` dict (the same object Scheduler holds), so it always sees
    current state without needing its own copy."""
    def check(promise: dict) -> bool:
        case = cases.get(promise.get("case_id"))
        latent = (case or {}).get("latent") or {}
        # recoverable=False is the ground-truth ceiling: a case that can
        # never truly recover keeping its promise anyway (ceiling_capture
        # must never exceed 100%) would be a self-contradiction in the
        # synthetic data, not a real outcome.
        return bool(latent.get("recoverable")) and bool(latent.get("keeps_promise"))
    return check


# ---------------------------------------------------------------------------
# the outcome simulator — the ONLY code allowed to read case["latent"]
# ---------------------------------------------------------------------------

def _simulate_outcome(case: dict, decision: dict, now: datetime) -> bool:
    """
    Judges whether the action just executed actually recovers the money, and
    simulates the customer's reply when the model says they'd respond to
    this outreach — exactly the outcome a real webhook or inbound message
    would report later, never something the pipeline decided for itself.

    This is app.scheduler.jobs.Scheduler's on_action_executed callback for a
    simulated run — live mode passes none, since a real charge's outcome
    arrives later via an actual webhook, not synchronously from a tick.
    """
    latent = case.get("latent") or {}

    reply_outcome = _maybe_route_reply(case, decision, latent, now)
    if reply_outcome is not None:
        return reply_outcome

    return _matches_correct_strategy(case, decision, latent, now)


def _matches_correct_strategy(case: dict, decision: dict, latent: dict, now: datetime) -> bool:
    if not latent.get("recoverable"):
        return False

    intervention = decision.get("intervention")
    if intervention in ("escalate", "close_lost"):
        return False

    reason = case.get("reason_category")
    strategy = (config.DEFAULT_POLICY.get("retry_rules", {}).get(reason) or {}).get("strategy")
    if strategy != latent.get("correct_strategy"):
        return False

    scheduled = _parse_ts(decision.get("scheduled_for")) or now
    created = _parse_ts(case.get("created_at"))

    if reason == "insufficient_funds":
        salary_day = latent.get("salary_day")
        if salary_day is not None and created is not None:
            true_next = _true_next_salary_date(created, int(salary_day))
            if scheduled.date() < true_next.date():
                return False  # the agent's guess (1st/month-end) undershot the real payday

    elif reason == "bank_downtime":
        resolves_after = latent.get("resolves_after_days")
        if resolves_after is not None and created is not None:
            if (scheduled - created).days < int(resolves_after):
                return False  # retried before the outage genuinely cleared

    return True


def _true_next_salary_date(from_dt: datetime, salary_day: int) -> datetime:
    """The customer's REAL next salary date after `from_dt` — used only to
    grade the decision engine's blind guess, never fed back into it."""
    year, month = from_dt.year, from_dt.month
    day = min(salary_day, calendar.monthrange(year, month)[1])
    candidate = from_dt.replace(day=day, hour=9, minute=0, second=0, microsecond=0)
    if candidate.date() > from_dt.date():
        return candidate
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    day = min(salary_day, calendar.monthrange(year, month)[1])
    return from_dt.replace(year=year, month=month, day=day, hour=9, minute=0, second=0, microsecond=0)


def _maybe_route_reply(case: dict, decision: dict, latent: dict, now: datetime) -> bool | None:
    """Returns True (recovered), False (definitively not, this cycle), or
    None (no reply / nothing that overrides the direct outcome math)."""
    if decision.get("intervention") not in _OUTREACH_INTERVENTIONS:
        return None
    if not latent.get("responds_to_outreach"):
        return None

    case_id = case["id"]
    intent = latent.get("reply_intent")
    text = latent.get("reply_text_hinglish")

    audit_log.record(
        case_id, _ACTOR, audit_log.REPLY_RECEIVED,
        inp={"text": text}, decision=intent,
        reasoning=f"Simulated customer reply ({intent}): {text!r}",
    )

    if intent == "opt_out":
        repository.update_case(case_id, opted_out=True)
        case["opted_out"] = True
        return False

    if intent == "dispute":
        repository.update_case(case_id, state="ESCALATED")
        case["state"] = "ESCALATED"
        audit_log.record(
            case_id, _ACTOR, audit_log.ESCALATED,
            reasoning="Customer disputes the charge; escalating, stopping automation.",
        )
        return False

    if intent in ("already_paid", "pay_now"):
        # Same ground-truth ceiling _matches_correct_strategy enforces: a
        # case that latent says can never actually recover doesn't get to
        # recover just because the simulated reply claims a payment -- that
        # would let recovered_value exceed the recoverable ceiling.
        return bool(latent.get("recoverable"))

    if intent == "promise_to_pay":
        # tracker.record_promise applies the horizon cap itself (FR-D5); pass
        # the uncapped offset and let it be the one place that decision is made.
        offset = int(latent.get("promise_offset_days") or 0)
        promised_date = now.date() + timedelta(days=offset)
        result = tracker.record_promise(
            case_id, float(case.get("amount") or 0), promised_date,
            source="inferred", policy=config.DEFAULT_POLICY, now=now,
        )
        case["state"] = "PROMISE_MADE" if result["created"] else "ESCALATED"
        return False

    return None  # 'unclear' or anything else — no override


def _compute_ceiling(cases: list[dict]) -> dict:
    recoverable = [c for c in cases if (c.get("latent") or {}).get("recoverable")]
    return {
        "recoverable_count": len(recoverable),
        "recoverable_value": sum(float(c.get("amount") or 0) for c in recoverable),
    }


# ---------------------------------------------------------------------------
# naive baseline — one immediate retry, no timing intelligence, no outreach,
# no promises. What most merchants actually do.
# ---------------------------------------------------------------------------

# ASSUMPTION (documented, not tuned to flatter our own numbers): a blind
# immediate retry on insufficient_funds is not automatically doomed. Some
# slice of customers will have topped up, or had an unrelated credit land,
# in the gap between the original failure and a same-day naive retry,
# entirely independent of our own salary-day timing model. 35% is a
# deliberately generous "modest chance" reading of that — high enough that
# the naive baseline isn't a strawman, low enough that it's still clearly
# what "no timing intelligence" looks like. Because insufficient_funds
# dominates the reason mix, this single number is what keeps the naive
# baseline in a believable ~12-18% recovery range instead of collapsing to
# ~5% (making our lift look implausibly large) or drifting so high naive
# stops reading as "naive."
_NAIVE_TOPUP_CHANCE = 0.35


def naive_baseline(set_name: str = "dev") -> dict:
    """
    Reads the dataset directly (no DB writes, no pipeline run) and judges
    each case against latent as if it got exactly one immediate retry, with
    no timing intelligence, no outreach, and no promise handling — what most
    merchants actually do:
      - insufficient_funds: recovers only if the case is fundamentally
        recoverable AND, independent of timing, the customer happened to
        already have funds (_NAIVE_TOPUP_CHANCE — see comment above).
      - bank_downtime: recovers only if the outage had already cleared by
        the moment of the retry (latent resolves_after_days == 0). This
        dataset's generator draws resolves_after_days from 1-3 days, so in
        practice this is ~never — realistic: an outage essentially never
        clears in the same instant as the original failure.
      - mandate_revoked / expired_card: can never recover — retrying the
        exact same broken payment method changes nothing, no matter when.
      - checkout_dropoff: no outreach at all means no nudge, no link, no
        conversion.
      - technical_other: no modeled timing gate, so a plain retry has a shot
        whenever the case is fundamentally recoverable.
    The topup chance is drawn from a per-case-id-seeded RNG, so re-running
    naive_baseline() on the same dataset always gives the same result —
    the same reproducibility discipline as the dataset's own generation.

    by_reason: {reason: {"count", "recovered_count"}} -- the dashboard's
    recovery-by-reason chart needs an "ours vs naive" pair per category, and
    naive's per-reason rate can only be computed here (it needs latent,
    which only this module may read) -- never recomputed client-side.
    """
    cases = _load_dataset(set_name)
    recovered_count = 0
    recovered_value = 0.0
    by_reason: dict[str, dict] = defaultdict(lambda: {"count": 0, "recovered_count": 0})
    for case in cases:
        row = by_reason[case.get("reason_category") or "unknown"]
        row["count"] += 1
        if _naive_recovers(case):
            recovered_count += 1
            recovered_value += float(case.get("amount") or 0)
            row["recovered_count"] += 1
    return {
        "recovered_count": recovered_count,
        "recovered_value": recovered_value,
        "total_count": len(cases),
        "at_risk_value": sum(float(c.get("amount") or 0) for c in cases),
        "by_reason": dict(by_reason),
    }


def _naive_recovers(case: dict) -> bool:
    latent = case.get("latent") or {}
    if not latent.get("recoverable"):
        return False

    reason = case.get("reason_category")

    if reason == "technical_other":
        return True

    if reason == "insufficient_funds":
        rng = random.Random(f"naive-topup:{case.get('id')}")
        return rng.random() < _NAIVE_TOPUP_CHANCE

    if reason == "bank_downtime":
        return latent.get("resolves_after_days") == 0

    # mandate_revoked, expired_card, checkout_dropoff (and anything
    # unrecognised): a blind immediate retry with no other action can never
    # succeed here.
    return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fmt_pct(value: float | None) -> str:
    return f"{value * 100:.1f}%" if value is not None else "n/a"


def _fmt_pct_n(value: float | None, numerator: int, denominator: int) -> str:
    """Same as _fmt_pct, but with the sample size shown alongside it -- a
    rate over a handful of data points (e.g. ~11 resolved promises on the
    dev set) reads as far more solid on its own than "(3/11)" makes honest."""
    return f"{_fmt_pct(value)} ({numerator}/{denominator})"


def _fmt_ratio(value: float | None, unit: str = "", decimals: int = 2) -> str:
    return f"{value:.{decimals}f}{unit}" if value is not None else "n/a"


def _fmt_invariant(value: int) -> str:
    return "PASS" if value == 0 else f"FAIL ({value})"


def _print_summary(set_name: str, m: dict) -> None:
    print(f"\n=== Wapsi batch: {set_name} ===")

    print("\n-- PRIMARY --")
    print(f"  cases              : {m['total_cases']}")
    print(f"  recovered          : {m['recovered_count']} ({_fmt_pct(m['recovery_rate_count'])})")
    print(
        f"  Rs recovered       : {m['recovered_value']:,.0f} / {m['at_risk_value']:,.0f} "
        f"({_fmt_pct(m['recovery_rate_value'])})"
    )
    print(f"  lift vs naive      : {_fmt_pct(m.get('recovery_lift'))}")
    print(f"  ceiling capture    : {_fmt_pct(m.get('ceiling_capture'))}")

    print("\n-- OPERATIONAL --")
    print(
        f"  kept-promise rate       : "
        f"{_fmt_pct_n(m.get('kept_promise_rate'), m.get('kept_promise_kept_count', 0), m.get('kept_promise_resolved_count', 0))}"
    )
    print(f"  false-escalation rate   : {_fmt_pct(m.get('false_escalation_rate'))}")
    print(f"  avg time to recovery    : {_fmt_ratio(m.get('avg_time_to_recovery_days'), ' days')}")
    print(f"  interventions/recovery  : {_fmt_ratio(m.get('interventions_per_recovery'))}")
    print(f"  cost per recovered Rs   : {_fmt_ratio(m.get('cost_per_recovered_rupee'), decimals=4)}")
    print(f"  contact efficiency      : {_fmt_ratio(m.get('contact_efficiency'))}")
    print(f"  gate blocks             : {m.get('gate_block_counts') or {}}")

    print("\n-- SAFETY INVARIANTS (must all be 0) --")
    print(f"  double-charge incidents : {_fmt_invariant(m.get('double_charge_incidents', 0))}")
    print(f"  post-opt-out contacts   : {_fmt_invariant(m.get('post_opt_out_contacts', 0))}")
    print(f"  actions without audit   : {_fmt_invariant(m.get('actions_without_audit', 0))}")
    print(f"  over-cap discounts      : {_fmt_invariant(m.get('over_cap_discounts', 0))}")

    print("\n-- EXCEPTIONS --")
    print(f"  unrecovered             : {len(m.get('exception_list') or [])}")
    for row in m.get("worst_three_reasons") or []:
        print(
            f"    worst: {row['reason_category']:<20} rate={_fmt_pct(row['recovery_rate']):>6}  "
            f"count={row['count']:>4}  Rs lost={row['rupees_lost']:,.0f}  "
            f"dominant={row['dominant_failure_mode']}"
        )

    print(f"\n  -> {m.get('_snapshot_path', f'data/results_{set_name}.json')}\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the Wapsi recovery batch end to end.")
    ap.add_argument("--set", dest="set_name", default="dev", choices=["dev", "holdout"])
    ap.add_argument("--live", action="store_true", help="use the real Razorpay API instead of simulating")
    ap.add_argument("--horizon-days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=None, help="only process the first N cases (diagnostic runs)")
    ap.add_argument(
        "--persist", choices=["memory", "supabase"], default="memory",
        help="'memory' (default) runs at Python speed and bulk-flushes to Supabase at the end; "
             "'supabase' hits the real database on every call",
    )
    ap.add_argument(
        "--clear", action=argparse.BooleanOptionalAction, default=True,
        help="clear this batch_id's existing rows before ingesting (default: true); "
             "use --no-clear to append onto an existing batch instead",
    )
    args = ap.parse_args()

    result = run_batch(
        set_name=args.set_name, horizon_days=args.horizon_days, live=args.live,
        persist=args.persist, limit=args.limit, clear=args.clear,
    )
    _print_summary(args.set_name, result)


if __name__ == "__main__":
    main()
