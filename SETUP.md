# SETUP.md — zero to running

Follow this top to bottom, once. Tick each box. Nothing here costs money.

Where you run commands: **VS Code's integrated terminal**, opened inside the
project folder (`View → Terminal`, or `` Ctrl+` ``). Every command below assumes
you are in the project root (`wapsi/`) unless stated otherwise.

---

## Stage 0 — Accounts (do these first, ~20 min)

Create these in this order. All free, no card.

- [ ] **GitHub** — you already have one. Note your username.
- [ ] **Razorpay** → sign up, then **toggle to Test Mode** (top of dashboard).
      Go to `Settings → API Keys → Generate Test Key`. Copy **Key ID** and
      **Key Secret** into a scratch file — the secret is shown only once.
- [ ] **Supabase** → `supabase.com` → New project. Pick the region closest to
      you (Mumbai/Singapore). Save the DB password. Then
      `Project Settings → API` and copy: **Project URL**, **anon key**,
      **service_role key**.
- [ ] **Google AI Studio** → `aistudio.google.com` → Get API key (free tier).
      This is the agent's runtime brain.
- [ ] **OpenRouter** → `openrouter.ai` → Keys → create key. This lets OpenCode
      write most of your code without touching your Claude quota.
- [ ] **ElevenLabs** → `elevenlabs.io` → free tier → Profile → API key. Also
      pick a multilingual voice and copy its **Voice ID**.
      *(Optional stretch — you can add this in week 2.)*

---

## Stage 1 — Install on your laptop (~15 min)

Check what you already have:

```bash
node -v      # need v18+
python3 -V   # need 3.11+
git --version
```

Install anything missing:

- **Node.js 20 LTS** — nodejs.org (needed for React + Claude Code + OpenCode)
- **Python 3.11+** — python.org
- **Git** — git-scm.com
- **VS Code** — code.visualstudio.com
- **ngrok** — ngrok.com (free account, needed for webhooks in Stage 6)

VS Code extensions (Extensions panel, `Ctrl+Shift+X`): **Python**, **Pylance**,
**Tailwind CSS IntelliSense**, **ES7+ React snippets**.

Then the two AI coding tools:

```bash
npm install -g @anthropic-ai/claude-code     # your Pro plan covers this
curl -fsSL https://opencode.ai/install | bash
```

---

## Stage 2 — Project folder + unzip (~5 min)

Pick a permanent home. Do **not** use Downloads or Desktop.

```bash
# macOS / Linux
mkdir -p ~/Projects && cd ~/Projects

# Windows (PowerShell)
mkdir $HOME\Projects; cd $HOME\Projects
```

Move `wapsi-skeleton.zip` into `~/Projects`, then:

```bash
unzip wapsi-skeleton.zip     # creates ~/Projects/wapsi
cd wapsi
code .                       # opens this folder in VS Code
```

You should now see `app/`, `dashboard/`, `supabase/`, `AGENTS.md`, `PRD.md`.

> The **folder is the project**. Both AI tools, git, and VS Code all point at
> this one folder. That is what keeps everything in sync.

---

## Stage 3 — Git + GitHub (~10 min)

**One-time git identity** (skip if already set):

```bash
git config --global user.name  "Ekansh Chaurasiya"
git config --global user.email "your@email.com"
```

**Initialise and commit:**

```bash
cd ~/Projects/wapsi
git init
git branch -M main
git add .
git commit -m "chore: scaffold वापसी — architecture, docs, synthetic data"
```

**Create the GitHub repo:** github.com → New repository → name `wapsi` →
**Public** (the buildathon requires a public repo) → **do not** add a README or
.gitignore (you already have them) → Create.

**Connect and push** (HTTPS is simplest; when it asks for a password, paste a
**Personal Access Token** from GitHub → Settings → Developer settings → Tokens →
Generate new token (classic) → scope `repo`):

```bash
git remote add origin https://github.com/<your-username>/wapsi.git
git push -u origin main
```

**Your daily rhythm** — commit small and often. This is your safety net and your
continuity layer between AI tools:

```bash
git add .
git commit -m "feat(governance): implement gates G1–G4"
git push
```

Commit prefixes: `feat:` `fix:` `chore:` `docs:` `test:` `refactor:`.

> **Why this matters for AI tools:** the code lives in these files, not inside
> any tool's memory. Claude Code and OpenCode both read and write this same
> folder. Git is your undo button — if a model makes a mess, `git diff` shows
> exactly what changed and `git checkout -- <file>` reverts it.

---

## Stage 4 — Python env + secrets (~10 min)

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

You should see `(.venv)` in your prompt. **Re-run the activate line every time
you open a new terminal.**

Create your secrets file:

```bash
cp .env.example .env               # Windows: copy .env.example .env
```

Open `.env` in VS Code and paste in the keys from Stage 0. `.env` is gitignored
— **never commit it**.

Do the same for the frontend:

```bash
cp dashboard/.env.example dashboard/.env
```

Fill in `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY`.
⚠️ The frontend gets the **anon** key only. The **service_role** key stays in the
backend `.env` — it bypasses all security rules.

---

## Stage 5 — Database (~5 min)

1. Open your Supabase project → **SQL Editor** → **New query**.
2. Open `supabase/migrations/001_initial_schema.sql` in VS Code, copy the whole
   file, paste into the editor, click **Run**.
3. Go to **Table Editor** — you should see `cases`, `payment_attempts`,
   `outreach`, `promises`, `audit_log`.

Generate your data:

```bash
python -m app.detection.synthetic_data
```

This writes `data/cases_dev.json` (100 cases, for building) and
`data/cases_holdout.json` (300 cases, for your final reported numbers).

---

## Stage 6 — Razorpay day-0 checkpoint (~30 min) ⭐

**Do this before writing any logic.** It proves the payment plumbing works, and
it is the single biggest risk in the build.

1. **Start the API:**
   ```bash
   uvicorn app.main:app --reload --port 8000
   ```
   Visit `http://localhost:8000/health` → should return `{"status":"ok"}`.

2. **Expose it** (new terminal):
   ```bash
   ngrok http 8000
   ```
   Copy the `https://....ngrok-free.app` URL.

3. **Register the webhook:** Razorpay Dashboard (Test Mode) →
   `Settings → Webhooks → Add New Webhook`
   - URL: `https://....ngrok-free.app/webhooks/razorpay`
   - Secret: make one up, put the same value in `.env` as
     `RAZORPAY_WEBHOOK_SECRET`
   - Events: `payment.failed`, `payment.captured`, `order.paid`,
     `subscription.charged`, `subscription.halted`

4. **Prove it end to end:** create a test payment link from the Razorpay
   dashboard, open it, pay with a test card (`4111 1111 1111 1111`, any future
   expiry, any CVV), and watch the webhook hit your terminal.

✅ If you see the event arrive, the hard infrastructure is done.

---

## Stage 7 — The AI coding tools (~15 min)

### Claude Code (architecture + hard problems)

```bash
cd ~/Projects/wapsi
claude
```

First run opens a browser to log in — use the account with your Pro plan. Then
just talk to it in the terminal. It reads `CLAUDE.md` → `AGENTS.md`
automatically, so it starts with full project context.

**Reserve it for:** architecture decisions, the governance gate, tricky
debugging, code review. Not bulk typing.

### OpenCode (the bulk of the code — free/cheap)

```bash
cd ~/Projects/wapsi
opencode auth login          # choose OpenRouter, paste your key
opencode                     # start it in the project folder
```

Inside OpenCode use `/models` to pick one. Recommended order:

| Use | Model | Cost |
| --- | --- | --- |
| Everyday bulk coding | `deepseek/deepseek-chat` | ~pennies for the whole project |
| Zero-cost fallback | any model tagged `:free` on OpenRouter | ₹0 |
| Harder refactors | `moonshotai/kimi-k2` or `z-ai/glm-4.6` | cheap |

**How both tools share one project:** they edit the same files in
`~/Projects/wapsi`, and both read `AGENTS.md` for context. Switching is just
closing one and opening the other. **Commit before you switch** so there's a
clean checkpoint:

```bash
git add . && git commit -m "wip: before switching tools"
```

That's the whole trick. No syncing, no export/import — the folder *is* the
shared state.

---

## Stage 8 — Dashboard (~10 min)

```bash
cd dashboard
npm install
npm run dev        # → http://localhost:5173
```

Leave it running in its own terminal while you build.

---

## Your normal working setup

Four VS Code terminals, side by side:

| Terminal | Command | Purpose |
| --- | --- | --- |
| 1 | `uvicorn app.main:app --reload --port 8000` | backend |
| 2 | `cd dashboard && npm run dev` | frontend |
| 3 | `ngrok http 8000` | webhooks (only when testing them) |
| 4 | `claude` or `opencode` | your AI pair |

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `ModuleNotFoundError` | You forgot `source .venv/bin/activate` |
| `SUPABASE_URL missing` | `.env` not created, or you're running from the wrong folder |
| Webhook never arrives | ngrok URL changed (it does on every restart) — update it in the Razorpay dashboard |
| Supabase "project paused" | Free tier pauses after ~7 days idle — open the dashboard to resume. Touch it every few days before submission |
| RLS blocks the dashboard | The migration's anon read policies must have run; re-run the SQL |
| `git push` asks for a password | Use a Personal Access Token, not your GitHub password |
| Claude usage limit hit | Switch to OpenCode; commit first |
| Secret committed by accident | Rotate the key immediately in that service's dashboard |

---

## Definition of done for setup

- [ ] `http://localhost:8000/health` returns ok
- [ ] `http://localhost:5173` renders the dashboard shell
- [ ] Supabase Table Editor shows all 5 tables
- [ ] `data/cases_dev.json` and `data/cases_holdout.json` exist
- [ ] A Razorpay test webhook reached your terminal
- [ ] The repo is public on GitHub with your first commit
- [ ] `claude` and `opencode` both launch inside the project folder

Once all boxes are ticked, start **Phase 1** in `PRD.md §14`.
