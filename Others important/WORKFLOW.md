# WORKFLOW.md — how to run this build

Two things live here: **how to start and stop a work session**, and **what to
build next**. Open this file first every time you sit down.

---

# PART 1 — Cold start (after a break, a restart, anything)

### Step 1 — Open the project

Open VS Code → `File → Open Folder` → `C:\Projects\wapsi`
(or from PowerShell: `cd C:\Projects\wapsi ; code .`)

### Step 2 — Sync before you touch anything

Terminal 1 (`` Ctrl+` ``):

```powershell
git status          # should say "working tree clean"
git pull            # only matters if you ever work from another machine
```

> If `git status` shows uncommitted changes you don't recognise, run `git diff`
> before doing anything else. Never start new work on top of a mess you haven't
> read.

### Step 3 — Terminal 1 → `api`

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8000
```

Check http://localhost:8000/health → `{"status":"ok","service":"wapsi"}`
Leave it running. `--reload` restarts it on every file save.

### Step 4 — Terminal 2 → `work` (where you live)

New terminal (`+` icon):

```powershell
.\.venv\Scripts\Activate.ps1
pytest -q               # confirm you're starting from green
```

This is where you run scripts, tests, batches, and git.

### Step 5 — Terminal 3 → `ai`

```powershell
claude
```

Runs in the project folder, auto-reads `CLAUDE.md` → `AGENTS.md`.

### Step 6 — Terminal 4 → `web` (only for frontend days)

```powershell
cd dashboard
npm run dev             # → http://localhost:5173
```

### Step 7 — Port forward (only when testing live webhooks)

PORTS tab → Forward a Port → `8000` → right-click → Public.
**The URL changes every time.** If you forward again, update it in
Razorpay → Settings → Webhooks.

> Rename terminals: right-click the tab → Rename → `api`, `work`, `ai`, `web`.

### 30-second sanity check

- [ ] `(.venv)` in the prompt of terminals 1 and 2
- [ ] `/health` returns ok
- [ ] `pytest -q` is green
- [ ] `git status` clean

If all four pass, start building.

---

# PART 2 — During work

### The loop

```
pick ONE module  →  read its spec in PRD.md / AGENTS.md
                 →  git commit (clean checkpoint BEFORE the AI touches anything)
                 →  build it (Claude Code, or yourself)
                 →  git diff        ← READ THIS. Every time.
                 →  pytest
                 →  git commit + push
                 →  next module
```

**`git diff` before every commit is the habit that matters.** It's what turns a
generated repo into a repo you can defend in an interview. If you can't explain
a line, ask Claude Code why it's there before you accept it.

### When to commit

| Moment | Why |
| --- | --- |
| Before letting an AI make a big change | so `git restore .` is safe |
| After a module works and tests pass | a real checkpoint |
| Before switching tasks | context boundary |
| Before you close the laptop | never lose a day |

```powershell
git add . ; git commit -m "feat(diagnosis): rule-based reason classifier" ; git push
```

Prefixes: `feat:` `fix:` `chore:` `docs:` `test:` `refactor:`

### When to test

```powershell
pytest -q                        # after every module
pytest tests\test_x.py -v        # while building one thing
```

Never commit red tests. If something's broken and you must stop, commit with
`wip:` and a note in the message.

### Working with Claude Code well

Scoped requests, not open-ended ones. This preserves quota **and** produces
better code:

- ✅ "Implement `app/diagnosis/classifier.py` per AGENTS.md. Rules first, LLM
  only on unmatched. Return `(category, how)`. Write tests using RAW_REASONS
  from synthetic_data.py."
- ❌ "Build the diagnosis system."

Point it at files. Reference `AGENTS.md` and `PRD.md` by section. Ask it to
explain anything you don't follow — that's what the panel will ask you.

---

# PART 3 — Session end

```powershell
pytest -q
git add . ; git commit -m "..." ; git push
```

Then `Ctrl+C` in the api terminal, close VS Code. Nothing else to clean up.

Once every few days: open the Supabase dashboard so the free tier doesn't pause
the project after ~7 days idle.

---

# PART 4 — Status board

### ✅ Done
- Project, git, public repo, venv, dependencies
- Supabase schema live (5 tables, RLS, realtime, append-only trigger)
- 400 synthetic cases (100 dev / 300 holdout)
- FastAPI running; **real Razorpay webhook delivered end to end**
- Webhook simulator + Gemini key checker
- `repository.py` — full data-access layer
- `audit/log.py` — append-only audit
- `governance/policy_gate.py` — gates G0–G10, **43 tests passing**
- Dashboard shell rendering
- Docs: PRD, AGENTS, README, SETUP ×2, COMMANDS, LEARNING

### 🔨 Remaining

| Module | What it does |
| --- | --- |
| `diagnosis/classifier.py` | raw reason string → category |
| `decision/engine.py` | category + history + policy → intervention |
| `execution/razorpay_client.py` | Razorpay test API wrapper |
| `execution/actions.py` | execute allowed decisions + graceful failure |
| `detection/batch_scanner.py` | batch runner + outcome simulator |
| `metrics/compute.py` | metrics + naive baseline + exception list |
| `voice/inbound.py` | Hinglish audio → transcript → intent |
| `promises/tracker.py` | promise-to-pay lifecycle |
| `scheduler/jobs.py` | simulated clock + due work |
| `dashboard/` | 3 screens |
| Feature B, outbound voice | **droppable** |

---

# PART 5 — Day by day (28 Aug → 5 Sep)

**8 days.** The plan front-loads a working end-to-end run, because a crude
complete pipeline on day 3 is worth far more than three perfect modules on day 6.

### 🚶 The walking-skeleton rule
Get **one case** to travel detect → diagnose → decide → gate → act → recover,
even if every step is dumb, **by 30 Aug**. Then improve each step. Never build
modules in isolation hoping they connect at the end.

| Day | Build | Done when |
| --- | --- | --- |
| **28 Aug** (today) | `classifier.py` + `decision/engine.py` | A case gets a category and an intervention with timing |
| **29 Aug** | `razorpay_client.py` + `actions.py` | An allowed decision creates a real test-mode payment link |
| **30 Aug** | `batch_scanner.py` + outcome simulator | **Walking skeleton: full dev batch runs end to end** |
| **31 Aug** | `metrics/compute.py` + naive baseline | You can print recovery rate and lift for the dev set |
| **1 Sep** | `voice/inbound.py` + `promises/tracker.py` | A Hinglish voice note becomes a logged promise |
| **2 Sep** | `scheduler/jobs.py` + graceful-failure showpiece | Promised dates resolve; API timeout escalates cleanly |
| **3 Sep** | Dashboard screens 1 + 2 | Pipeline board live; audit trail readable |
| **4 Sep** | Screen 3 + **holdout run (once)** + README numbers | Real metrics in the README |
| **5 Sep** | Demo video, pitch rehearsal, submit | Submitted |

**Buffer rule:** if you're behind on 2 Sep, drop Feature B and outbound voice.
Core (A + C + D) fully satisfies the track. Depth over breadth.

---

# PART 6 — Claude Code prompts, ready to paste

### Today, module 1 — diagnosis
```
Implement app/diagnosis/classifier.py following AGENTS.md.

- Rule-based mapping FIRST: substring/code match on the raw Razorpay reason
  string → one of: insufficient_funds, expired_card, mandate_revoked,
  bank_downtime, technical_other, checkout_dropoff.
- LLM (Groq/Gemini) only when rules find no match; constrain the output to that
  exact enum. It must never override an unambiguous rule match.
- Return (category, how) where how is "rule" or "llm".
- Log DIAGNOSED to the audit trail with the raw reason in, category out, and
  which path decided.
- Write tests/test_diagnosis.py using the RAW_REASONS dict in
  app/detection/synthetic_data.py as ground truth.
```

### Today, module 2 — decision engine
```
Implement app/decision/engine.py per PRD.md §5 (FR-A3) and AGENTS.md.

decide(case, history, policy) -> Decision dict with intervention,
scheduled_for, channel, message (Hinglish), discount_pct, reasoning.

Timing intelligence is the point:
- insufficient_funds -> next salary day (1st or month-end cluster), NOT today
- bank_downtime -> exponential backoff in hours
- mandate_revoked -> request_re_mandate, never a silent retry
- expired_card -> request_card_update, payment link fallback
- checkout_dropoff -> nudge first, bounded offer only on touch 2

Clamp every field to policy before returning. Log DECIDED with plain-language
reasoning. Add tests asserting insufficient_funds is never scheduled before the
salary day.
```

### Tomorrow — execution
```
Implement app/execution/razorpay_client.py and app/execution/actions.py.

actions.execute() must:
1. call governance.policy_gate.check() and audit the verdict via audit.log.gate()
2. build idempotency key {case_id}:{intervention}:{attempt_no}
3. execute via razorpay_client (test mode) or persist simulated outreach
4. record the payment_attempt / outreach row via repository
5. audit ACTED with the result
6. advance case state

Graceful failure: on timeout/5xx use exponential backoff (1s, 4s, 10s); if it
still fails, mark the attempt pending, audit ERROR + ESCALATED with full
context, and never retry blindly. Never double-charge.
```

---

## The one-line version

**Today:** commit, then build `classifier.py`, then `decision/engine.py`.
**By 30 Aug:** one case travelling the whole pipeline.
**By 4 Sep:** holdout numbers in the README.
