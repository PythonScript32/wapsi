"""
वापसी (Wapsi) API surface.

  GET  /health                 liveness
  POST /webhooks/razorpay      detection sensor (verify -> dedupe -> case)
  POST /replies                inbound customer reply (text or audio) -> voice.inbound
  POST /batch/run              run a batch, return metrics
  GET  /batch/results          serve data/results_{set}.json (dashboard Metrics screen)
  GET  /cases                  list cases (dashboard fallback if not using Supabase directly)
  GET  /cases/{id}/audit       full audit trail for one case

Run: uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="वापसी (Wapsi) — Revenue Recovery Agent")

# React dev server runs on :5173, but Vite auto-increments to :5174, :5175...
# when that port's already taken (another dashboard instance, a leftover
# process) -- a regex over localhost:<any port> is what actually holds up in
# practice, not a single hardcoded origin.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://localhost:\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok", "service": "wapsi"}

@app.get("/")
def root():
    return {
        "service": "वापसी (Wapsi) — Revenue Recovery Agent",
        "health": "/health",
        "docs": "/docs",
    }


@app.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    # TODO: verify_signature -> dedupe by event id -> handle_event
    return {"received": True}


@app.post("/replies")
async def inbound_reply(request: Request):
    # TODO: voice.inbound.parse_reply -> route intent (promise / opt_out / paid / pay_now)
    return {"todo": "wire voice.inbound"}


@app.post("/batch/run")
def batch_run():
    # TODO: from app.detection.batch_scanner import run_batch
    return {"todo": "wire batch_scanner.run_batch()"}


@app.get("/batch/results")
def batch_results(set_: str = Query("dev", alias="set")):
    """
    Serves data/results_{set}.json verbatim -- the exact dict
    app.metrics.compute() produced and app.detection.batch_scanner.run_batch()
    exported. The dashboard's Metrics screen reads this directly rather than
    recomputing anything: naive/ceiling/lift need case["latent"], which only
    batch_scanner.py is allowed to read, so this file is the sole source of
    truth for those numbers -- never re-derived client-side.
    """
    path = Path("data") / f"results_{set_}.json"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No batch results for '{set_}' yet. Run: python -m app.detection.batch_scanner --set {set_}",
        )
    return json.loads(path.read_text(encoding="utf-8"))
