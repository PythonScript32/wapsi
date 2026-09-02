# Setup

Requires Node 18+, Python 3.11+, and free accounts on
[Razorpay](https://razorpay.com) (test mode), [Supabase](https://supabase.com),
and [Google AI Studio](https://aistudio.google.com) for a Gemini key (or
[Groq](https://console.groq.com) — faster free tier, used instead of Gemini
when its key is set).

```bash
git clone https://github.com/PythonScript32/wapsi.git && cd wapsi

python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cd dashboard && npm install && cd ..

cp .env.example .env                      # Razorpay + Supabase + Gemini/Groq keys
cp dashboard/.env.example dashboard/.env   # Supabase URL + anon key ONLY, never service_role
```

**Database** — in the Supabase project's SQL Editor, run both migrations, in order:
`supabase/migrations/001_initial_schema.sql`, then `002_promise_source.sql`.

**Generate data and run a batch:**

```bash
python -m app.detection.synthetic_data           # writes cases_dev.json (100) + cases_holdout.json (300)
python -m app.detection.batch_scanner --set dev   # or --set holdout; ingests + prints the recovery summary
```

**Run the dashboard** (two terminals):

```bash
uvicorn app.main:app --reload --port 8000   # backend
cd dashboard && npm run dev                 # frontend -> http://localhost:5173
```

**Run the tests:**

```bash
pytest tests/
```

**Webhooks** — `/webhooks/razorpay` needs a public URL to receive real Razorpay
events. Expose the backend with a tunnel (e.g. `ngrok http 8000`) and register
the resulting `https://.../webhooks/razorpay` URL in the Razorpay dashboard's
Webhooks settings.
