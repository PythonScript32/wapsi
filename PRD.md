# PRD — वापसी (Wapsi)

**Product:** वापसी (Wapsi) — Revenue Recovery Agent
**Track:** Razorpay AI Buildathon · Track 3 (AI Revenue Recovery)
**Author:** Ekansh Chaurasiya
**Version:** 2.0 · 22 Aug 2026 · Submit 5 Sep 2026
**One-liner:** An agent that plugs into a merchant's Razorpay and wins back
slipping revenue — every money action explainable, bounded, gated, and logged.

---

## 1. Problem

Revenue doesn't leave a business in one clean event. It leaks:

| Leak | What happens | Why it's recoverable |
| --- | --- | --- |
| Failed recurring debit | UPI AutoPay / card mandate declines on renewal | Customer never chose to leave (*involuntary churn*) |
| Abandoned checkout | Cart filled, payment screen abandoned | Intent was already demonstrated |
| Unfollowed promise | "I'll pay next week" → nobody follows up | The customer said yes |

Grounding facts for India:
- UPI AutoPay debit failure runs ~8–15% normally, and spiked to 55–90% across
  banks in Aug 2025 — mostly insufficient balance and mandate issues.
- Email-only dunning recovers **under 10%**; WhatsApp + a UPI link recovers
  roughly **3×** that.
- Insufficient-funds failures cluster around salary cycles, so *when* you retry
  changes whether you get paid at all.

**The gap:** no cheap, autonomous, compliant system closes the loop from
detecting slipping revenue to actually recovering it — with the timing
intelligence, the local language, and the audit discipline that money demands.

---

## 2. Goals / non-goals

**Goals**
- G1 Detect at-risk revenue from ≥2 sources (failed debits, abandoned checkouts).
- G2 Diagnose each failure into a canonical category grounded in gateway codes.
- G3 Choose a *reason-appropriate*, bounded intervention — never a fixed retry.
- G4 Execute against Razorpay test APIs, idempotently.
- G5 Understand Hinglish replies (voice + text) and act on intent.
- G6 Reach out in Hinglish voice (ElevenLabs) where it helps.
- G7 Capture and follow through on promises to pay.
- G8 Gate every money action; log every decision immutably.
- G9 Recover measurable money across a 300-case held-out batch, with honest metrics.
- G10 Handle ≥1 failure gracefully, demonstrably.

**Non-goals**
- Real money movement (test mode only) · multi-tenant SaaS · auth/billing ·
  real WhatsApp delivery (simulated + logged) · fraud/chargebacks (Track 2) ·
  training ML models (this is an agentic policy system).

---

## 3. Personas

- **Priya — founder/ops, D2C subscription brand (primary).** Loses ~10% of
  renewals to failed debits. Sends generic reminder emails. Wants recovery on
  autopilot, with numbers she can trust and no compliance exposure.
- **Rahul — finance ops, growing SaaS.** Cares about MRR leakage and needs an
  audit trail he can hand to finance.
- **The end customer (indirect).** Wants clear Hinglish communication, no
  spam, and an easy way to pay or say "next week".

---

## 4. Metrics — full definitions

Report **dev** and **holdout** separately. Headline numbers come from the
holdout, which is never used for tuning.

### 4.1 Primary
| ID | Metric | Formula | Target |
| --- | --- | --- | --- |
| M1 | Recovery rate (count) | recovered_cases / total_cases | ≥ 35% |
| M2 | Recovery rate (value) | ₹recovered / ₹at_risk | ≥ 35% |
| M3 | **Recovery lift** | (ours − naive) / naive | **≥ +60%** |
| M4 | Ceiling capture | ₹recovered / ₹recoverable_ceiling | ≥ 65% |

*Naive baseline:* one immediate retry, no timing intelligence, no voice, no
promises, no follow-up. This is what most merchants actually do.
*Ceiling:* the theoretical max from latent ground truth — proves we report
against what was actually winnable, not against an impossible 100%.

### 4.2 Secondary
| ID | Metric | Formula |
| --- | --- | --- |
| M5 | Kept-promise rate | kept / (kept + broken) |
| M6 | False-escalation rate | escalated_that_self_resolved / escalated |
| M7 | Avg time-to-recovery | mean(recovered_at − created_at) in days |
| M8 | Interventions per recovery | total_actions / recovered_cases |
| M9 | Cost per recovered ₹ | Σ channel_costs / ₹recovered |
| M10 | Recovery by reason | per-category recovery rate (a table) |
| M11 | Contact efficiency | recovered / total_messages_sent |

### 4.3 Governance & safety (proves the bar)
| ID | Metric | Why it matters |
| --- | --- | --- |
| M12 | Gate-block counts by gate | Shows bounds actually bind |
| M13 | Double-charge incidents | Must be **0** |
| M14 | Post-opt-out contacts | Must be **0** |
| M15 | Actions without audit rows | Must be **0** |
| M16 | Over-cap discounts issued | Must be **0** |

### 4.4 Honesty artifacts
- **Exception list** — every unrecovered case, grouped by reason, with the why.
- **Failure analysis** — the 3 categories we're worst at, and the hypothesis.

---

## 5. Feature A — Recurring-debit recovery (core)

> As Priya, when a renewal debit fails, I want the system to work out why and
> recover it the right way, so I don't lose the customer or the revenue.

**FR-A1** Ingest a failed debit (webhook or batch) → create a case.
**FR-A2** Diagnose into `insufficient_funds | expired_card | mandate_revoked |
bank_downtime | technical_other`.
**FR-A3** Apply the reason-appropriate strategy:

| Reason | Strategy | Rationale |
| --- | --- | --- |
| insufficient_funds | Wait for salary day, then retry | Retrying into an empty account just fails again |
| bank_downtime | Exponential backoff (hours) | Transient; the money is there |
| mandate_revoked | Request re-mandate | A revoked mandate *cannot* be charged |
| expired_card | Request card update + link fallback | Card will never work again |
| technical_other | Backoff, capped attempts | Unknown; fail safe |

**FR-A4** Execute idempotently; record a `payment_attempt`.
**FR-A5** Advance state to RECOVERED / ESCALATED / CLOSED_LOST.
**FR-A6** Send an RBI-compliant pre-debit notice ≥24h before any mandate debit.

**Acceptance**
- An insufficient_funds retry scheduled *before* the salary day is a bug.
- A mandate_revoked case never triggers a silent charge retry.
- No case is ever charged twice, even with duplicate webhooks.

---

## 6. Feature B — Checkout drop-off recovery

> As a merchant, when a customer abandons checkout, I want a timely nudge — and
> a small bounded offer only if the nudge fails.

**FR-B1** Detect an order created but unpaid within a configurable window (default 60 min).
**FR-B2** Touch 1 = nudge + fresh payment link. No discount.
**FR-B3** Touch 2 (only if touch 1 fails) = bounded offer ≤ `max_discount_pct`.
**FR-B4** Never offer to a customer who already paid.
**FR-B5** Max 2 touches, then CLOSED_LOST.

**Acceptance**
- An offer above the cap is blocked by governance and logged as GATE_BLOCK.
- A recovered checkout is attributed to the specific touch that recovered it.

---

## 7. Feature C — Hinglish voice concierge

> As an end customer, I want to reply in a Hinglish voice note and be understood.

**Inbound (FR-C1…C5)**
- **FR-C1** Accept audio or text replies.
- **FR-C2** Gemini Flash does transcription + intent in one multimodal call.
- **FR-C3** Intents: `promise_to_pay | already_paid | opt_out | pay_now |
  dispute | unclear`.
- **FR-C4** Resolve relative dates ("agle hafte", "salary ke baad", "3 din
  mein") against today and the known salary day.
- **FR-C5** Route: promise → Feature D · already_paid → verify then close ·
  opt_out → halt permanently · pay_now → send link · dispute → escalate to human.

**Outbound (FR-C6…C8)**
- **FR-C6** ElevenLabs renders the Hinglish message as a voice reminder.
- **FR-C7** Cache audio by `hash(text, voice_id)` — the free tier is
  character-limited.
- **FR-C8** If the voice API fails or quota is exhausted, fall back to text and
  log the fallback. Voice must **never** block a recovery.

**Acceptance**
- "abhi paise nahi, 28 Aug tak kar dunga" → `promise_to_pay`, date 28 Aug.
- "band kar do" → all outreach halts immediately and permanently.
- Low confidence → `unclear` → conservative follow-up, never an invented promise.

---

## 8. Feature D — Promise-to-pay tracker

**FR-D1** On a promise intent, create a promise (amount + date), move case to
PROMISE_MADE.
**FR-D2** Schedule the retry for the promised date + a gentle morning reminder.
**FR-D3** **A live promise pauses all other outreach** until its date.
**FR-D4** On the date: paid → `kept` + RECOVERED; not paid → `broken` + escalate.
**FR-D5** Cap promised dates at `max_promise_horizon_days`; flag anything beyond.
**FR-D6** Expose kept-promise rate to metrics.

**Acceptance**
- Chasing a customer who has an active unbroken promise is a bug.
- A broken promise escalates exactly once, then closes.

---

## 9. Architecture

**Two sensors → one brain → two layers → one ledger.**

```
Razorpay webhooks ─┐
                   ├─→ Case store (state machine)
Batch scanner ─────┘         │
                             ▼
                        Diagnosis  ──→ Decision engine (LLM + policy)
                                             │
                                             ▼
                                   ⛔ Governance gate  ──→ audit
                                             │ allowed
                                             ▼
                                     Action executor (idempotent)
                                     ├─→ Razorpay (retry / payment link)
                                     └─→ Outreach (WhatsApp/SMS/voice)
                                             │
                              Hinglish voice ↔ Promise tracker
                                             │
                                        Scheduler ──┘ (loops back)
                                             │
                                    Audit log + Metrics
```

**Case state machine**
`DETECTED → DIAGNOSED → SCHEDULED → OUTREACH_SENT → AWAITING_RESPONSE →
PROMISE_MADE → RETRYING → RECOVERED | ESCALATED | CLOSED_LOST`

Rules: states never regress (RECOVERED is terminal); every transition writes an
audit row; ESCALATED requires attached context.

---

## 10. Data model

Postgres via Supabase — see `supabase/migrations/001_initial_schema.sql`.

| Table | Purpose | Key constraints |
| --- | --- | --- |
| `cases` | One row per at-risk amount | enums for source/reason/state |
| `payment_attempts` | Every charge/retry | `idempotency_key` **UNIQUE** |
| `outreach` | Messages out + replies in | direction, channel, intent |
| `promises` | PTP lifecycle | status enum, promised_date |
| `audit_log` | Every decision | **append-only** (trigger blocks UPDATE/DELETE) |

Realtime is enabled on `cases` and `audit_log` so the dashboard streams live.
RLS: anon = read-only; the backend uses the service key.

`cases.latent` holds synthetic ground truth. **Only `batch_scanner.py` may read
it.** The pipeline decides blind.

---

## 11. Governance gates (the bar)

Every money/outreach action calls `policy_gate.check()` first. **Fail closed** —
if a check can't be evaluated, block.

| Gate | Condition | Action |
| --- | --- | --- |
| G1 | Case already RECOVERED | Block — never charge a payer |
| G2 | `opted_out = true` | Block — permanently |
| G3 | attempts ≥ per-reason cap | Block → escalate |
| G4 | Inside `min_contact_gap_hours` (24) | Block — anti-harassment |
| G5 | Past `grace_period_days` (14) | Block → CLOSED_LOST |
| G6 | discount > `max_discount_pct` (10%) | Block |
| G7 | amount > `max_exposure_inr` (₹5,000) | Block → human approval |
| G8 | Missing idempotency key | Block — double-charge risk |
| G9 | Mandate debit without 24h pre-debit notice | Block — RBI |
| G10 | Active unbroken promise | Block other outreach |

Every call writes `GATE_ALLOW` or `GATE_BLOCK` with a plain-language reason.

---

## 12. Design spec — dashboard (React)

Use this instead of Figma wireframes. Give it verbatim to your AI tool.

**Tokens** (already in `tailwind.config.js`): `ink #0B0F14` (bg), `panel
#141A22` (cards), `line #222C38` (borders), `muted #8A97A8` (secondary text),
`recovered #22C55E`, `atrisk #F59E0B`, `lost #EF4444`, `promise #6366F1`,
`accent #14B8A6`. Font Inter; mono JetBrains Mono for IDs/amounts. Spacing on a
4px scale. Rounded-lg cards, 1px borders, no drop shadows. Dark theme only.

**Screen 1 — Pipeline** (default)
Top: 4 stat cards — ₹ at risk · ₹ recovered · recovery rate · active cases.
Below: a kanban with one column per state. Each card shows customer name, ₹
amount (mono), a coloured reason chip, and time-in-state. New rows animate in
via Supabase Realtime. Clicking a card opens Screen 2.

**Screen 2 — Case detail**
Header: customer, amount, reason, current state. Body: a vertical audit timeline
— each entry shows timestamp, actor, decision, **reasoning in plain language**,
action, result. GATE_BLOCK rows in red with the reason; GATE_ALLOW in green.
Right rail: attempts, messages (with the Hinglish text and a play button for
voice), and promises. *This screen is what proves "explainable".*

**Screen 3 — Metrics**
Headline: recovery rate, **lift vs naive** (the hero number), ceiling capture,
kept-promise rate. A grouped bar chart of recovery-by-reason (ours vs naive) via
recharts. A gate-block table. Then the **exception list** — unrecovered cases
grouped by reason with the why.

Build order: Screen 1 → 2 → 3. Screen 2 matters most for judging.

---

## 13. Edge-case register

**Payments & idempotency**
1. Duplicate webhook for the same event → dedupe by event id.
2. Out-of-order webhooks (success arrives after failure) → never regress state.
3. Retry succeeds server-side but the response times out → idempotency key
   prevents the second charge.
4. Customer pays via another channel mid-sequence → G1 blocks; close RECOVERED.
5. Partial payment → record; keep the remainder at risk.
6. Amount above exposure cap → human approval.
7. Refund after recovery → reopen or close-lost, never silently ignore.
8. Currency other than INR → out of scope; flag and skip.

**Mandates & compliance**
9. Mandate revoked mid-sequence → stop charging immediately; request re-mandate.
10. Pre-debit notice not sent → G9 blocks the debit.
11. Mandate expires during grace → CLOSED_LOST with reason.
12. Bank downtime persists past the retry window → escalate, don't burn attempts.

**Customer interaction**
13. `opt_out` → permanent halt, all channels (G2).
14. "Already paid" but no payment found → verify, then one clarifying message.
15. Ambiguous Hinglish → `unclear`, never an invented promise.
16. Reply in pure Hindi/Devanagari or another language → handle or route unclear.
17. Voice note is silent/noise/too short → treat as no reply.
18. Multiple replies with conflicting intents → latest wins, log both.
19. Reply arrives after the case closed → log; reopen only if it's a payment intent.
20. Dispute intent → immediate human escalation, stop all automation.

**Promises**
21. Promise date beyond the horizon cap → cap it and flag.
22. Promise date in the past → treat as pay_now.
23. Second promise after a broken one → allow once, then escalate.
24. Customer pays before the promised date → mark kept early, cancel the retry.

**System & data**
25. Supabase unreachable → queue locally, retry; never lose an audit row.
26. Gemini rate-limited → fall back to rule-based classification, log degraded mode.
27. ElevenLabs quota exhausted → text fallback (FR-C8).
28. Razorpay 5xx / timeout → backoff → escalate with context (**the showpiece**).
29. Scheduler double-fires → idempotency keys absorb it.
30. Clock/timezone drift → store UTC, render IST.
31. Malformed webhook payload → reject, log, alert; never crash the handler.
32. Bad signature → drop silently, log the attempt.

---

## 14. Build phases & development sequence

**The sequencing rule:** data model → backend logic → agent → integrations →
frontend. The dashboard visualises data that must already exist; building UI
first means building on air.

| Phase | Days | Deliverable |
| --- | --- | --- |
| **0 — Foundation** | 22 Aug | SETUP.md complete; Razorpay webhook proven; schema live; data generated |
| **1 — Core loop** | 23–27 Aug | Repository, audit log, **governance gate**, diagnosis, decision engine, batch runner, naive baseline. *Runs the dev set end to end.* |
| **2 — Differentiators** | 28–30 Aug | Hinglish inbound (Gemini), promise tracker, scheduler with simulated clock |
| **3 — Breadth** | 31 Aug–1 Sep | Checkout recovery (Feature B) + ElevenLabs outbound voice. **Droppable if behind.** |
| **4 — Surface** | 2–3 Sep | Metrics engine, React dashboard (Screens 1→2→3), graceful-failure showpiece |
| **5 — Ship** | 4–5 Sep | Holdout run (once), README numbers, architecture diagram, demo video, pitch rehearsal, submit |

**Within Phase 1, this order:** `repository` → `audit` → `policy_gate` (+tests)
→ `classifier` → `decision engine` → `actions` → `batch_scanner` → `metrics`.
Governance and audit come *before* the agent logic, so every later module is
born already gated and logged.

**Fallback:** if behind, drop Feature B and outbound voice. Core (A + C + D)
fully satisfies the track.

---

## 15. Risks

| Risk | Mitigation |
| --- | --- |
| Scope creep across 4 features | Strict phase order; B and outbound voice explicitly droppable |
| Razorpay API friction | Day-0 checkpoint before any logic |
| Claude usage limits | OpenCode + cheap model for bulk; git keeps work portable |
| Supabase pause/outage | Repository seam + JSON snapshot export; touch the project weekly |
| Hinglish misclassification | Conservative `unclear` routing; log every classification |
| Metrics look implausibly clean | Report ceiling capture + exception list + failure analysis |
| Demo-day network failure | Pre-recorded demo video as backup |

---

## 16. Demo script (5 min)

1. **0:00 The leak (30s)** — Indian merchants lose recurring revenue to failed
   AutoPay; email dunning recovers under 10%.
2. **0:30 The product (45s)** — one agent, two sensors, one brain, Hinglish
   voice + promises, every action bounded/gated/logged.
3. **1:15 Live demo (2m30s)** — a failed debit flows through detect → diagnose
   (insufficient funds) → *decide to wait for salary day* → Hinglish voice
   reminder → customer replies "agle hafte kar dunga" → promise logged, retry
   scheduled → clock advances → **recovered**. Open the audit trail. Then
   trigger the API timeout → backoff → escalation with context.
4. **3:45 The numbers (45s)** — 300 held-out cases: ₹ recovered, recovery rate,
   **lift vs naive**, ceiling capture, kept-promise rate, exception list.
5. **4:30 Close (30s)** — what it recovers, why the audit trail matters, and
   that every layer is understood end to end.

---

## 17. Open decisions
- [ ] Outbound voice: ElevenLabs live, or pre-rendered clips for the demo?
- [ ] Dashboard hosting: local-only vs Vercel deploy.
- [ ] Include a small "merchant settings" screen to show policy bounds are
      configurable? (Nice signal, ~2h.)
