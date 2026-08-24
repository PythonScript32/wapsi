# AGENTS.md — project brief for AI coding tools

You are working on **वापसी (Wapsi)**, a revenue-recovery agent for the Razorpay
AI Buildathon (Track 3). Read this fully before writing code. Both Claude Code
and OpenCode read this file — it is the single source of truth that keeps output
consistent across tools, models, and sessions.

Deeper detail lives in `PRD.md`. Setup lives in `SETUP.md`.

---

## What we're building

An agent that plugs into a merchant's Razorpay (test mode) and automatically
wins back money that's slipping away.

- **Feature A — Recurring-debit recovery (core).** Failed subscription /
  UPI-AutoPay debits: detect → diagnose → smart retry → recover.
- **Feature B — Checkout drop-off recovery.** Abandoned checkouts: nudge, then a
  bounded offer.
- **Feature C — Hinglish voice concierge.** Inbound voice/text understanding
  (Gemini Flash) + outbound voice reminders (ElevenLabs).
- **Feature D — Promise-to-pay tracker.** Capture "agle hafte kar dunga",
  schedule the retry, follow through, measure kept-promise rate.

**Mental model:** *two sensors → one brain → two layers → one ledger.*
A and B are detection sources feeding ONE shared pipeline. C and D are
cross-cutting layers over that pipeline.

---

## Golden rules (never violate)

1. **Every money action is explainable, bounded, gated, and logged.** Nothing
   touches money without passing `app/governance/policy_gate.py` and writing to
   `app/audit/log.py`.
2. **Fail closed.** If a governance check can't be evaluated, BLOCK.
3. **Reason codes are ground truth.** Rules classify first; the LLM is consulted
   only for unmatched cases and is constrained to the valid enum. It may never
   override an unambiguous rule match or invent a reason.
4. **Idempotency everywhere money moves.** Key format
   `{case_id}:{intervention}:{attempt_no}`, UNIQUE in the DB. Never double-charge.
5. **Smart retries, never dumb ones.** Timing follows the reason:
   insufficient_funds → after salary day · bank_downtime → backoff ·
   mandate_revoked → re-mandate, never a silent retry · expired_card → card update.
6. **Opt-out and stopping rules are sacred.** Respect max attempts, grace period,
   24h contact gap, and any opt-out — immediately and permanently.
7. **Never read `case["latent"]` in pipeline code.** It is hidden synthetic
   ground truth. ONLY `app/detection/batch_scanner.py` may read it. The pipeline
   decides blind, exactly as it would in production.
8. **Never tune against the holdout set.** Build on `data/cases_dev.json`.
   `data/cases_holdout.json` runs once, at the end.
9. **Secrets never leave `.env`.** The Supabase **service key is backend-only**;
   the frontend gets the anon key. Never commit either.
10. **All DB access goes through `app/db/repository.py`.** Never call
    `get_client()` from pipeline code — the repository is the seam that keeps us
    portable if Supabase is unavailable.

---

## Module map

| Path | Responsibility |
| --- | --- |
| `app/config.py` | Secrets + `DEFAULT_POLICY` (all bounds live here, never inline) |
| `app/db/client.py` | Supabase client factory |
| `app/db/repository.py` | **All** data access |
| `app/detection/webhooks.py` | Sensor 1 — Razorpay events (verify → dedupe → case) |
| `app/detection/batch_scanner.py` | Sensor 2 — batch runner + outcome simulator |
| `app/detection/synthetic_data.py` | Dataset generator (done) |
| `app/diagnosis/classifier.py` | Raw reason → category |
| `app/decision/engine.py` | Category + history + policy → intervention |
| `app/governance/policy_gate.py` | **Gates G1–G10.** Most important module |
| `app/execution/razorpay_client.py` | Razorpay test API wrapper |
| `app/execution/actions.py` | Execute allowed decisions + graceful failure |
| `app/voice/inbound.py` | Hinglish transcription + intent (Gemini) |
| `app/voice/outbound.py` | Hinglish voice reminders (ElevenLabs, stretch) |
| `app/promises/tracker.py` | Promise-to-pay lifecycle |
| `app/scheduler/jobs.py` | Live scheduler + simulated clock for batches |
| `app/audit/log.py` | Append-only audit log |
| `app/metrics/compute.py` | Batch metrics + naive baseline + exception list |
| `dashboard/` | React + Vite + Tailwind + Supabase Realtime |

---

## Case state machine

`DETECTED → DIAGNOSED → SCHEDULED → OUTREACH_SENT → AWAITING_RESPONSE →
PROMISE_MADE → RETRYING → RECOVERED | ESCALATED | CLOSED_LOST`

States never regress. RECOVERED is terminal. Every transition writes audit.

---

## Conventions

- Python 3.11+, type hints, small pure functions. Prefer testable logic over
  clever abstraction.
- Read every limit from `DEFAULT_POLICY`. A hardcoded number in pipeline logic
  is a bug.
- Runtime LLM = **Gemini Flash (free tier)** only. Never call a paid API from
  app code.
- Webhook handlers stay fast: verify → dedupe → create/advance case → return 200.
  Decisions happen in the pipeline.
- Audit `reasoning` must be plain language, specific enough that a human reading
  one case's trail understands every rupee that moved and why.
- Frontend: Tailwind tokens from `tailwind.config.js` (`ink`, `panel`, `line`,
  `muted`, `recovered`, `atrisk`, `lost`, `promise`, `accent`). Never raw hex.
- Commits: `feat:` `fix:` `chore:` `docs:` `test:` `refactor:`. Commit often.

---

## Which tool does what

- **Claude Code** — architecture, the governance gate, the decision engine's
  timing logic, tricky debugging, code review. Use sparingly; quota is shared
  with other work.
- **OpenCode (cheap/free model)** — bulk implementation from these specs,
  boilerplate, React components, tests, refactors.

Both edit the same folder. **Commit before switching tools** so there's a clean
checkpoint. Git is the continuity layer, not any tool's memory.

---

## Build order

1. `repository` → `audit` → `policy_gate` (+ tests) — governance and logging
   exist *before* agent logic, so everything after is born gated and logged.
2. `classifier` → `decision engine` → `actions` → `batch_scanner` → `metrics`.
3. Voice inbound → promises → scheduler.
4. Checkout recovery → outbound voice.
5. Dashboard: Pipeline → Case detail → Metrics.

If time runs short, drop Feature B and outbound voice. Core (A + C + D) fully
satisfies the track. **Depth over breadth.**

---

## Do NOT

- Do not add features not in `PRD.md` without being asked.
- Do not install extra dependencies without a clear reason.
- Do not touch `latent` outside `batch_scanner.py`.
- Do not weaken a governance gate to make a test pass.
- Do not write to `audit_log` via UPDATE/DELETE (the DB trigger will reject it).
- Do not create new top-level folders.
- Do not put the Supabase service key anywhere under `dashboard/`.

---

## How to run

```bash
source .venv/bin/activate
python -m app.detection.synthetic_data     # regenerate datasets
uvicorn app.main:app --reload --port 8000  # backend
./scripts/run_batch.sh dev                 # batch + metrics
cd dashboard && npm run dev                # dashboard :5173
pytest                                     # tests
```
