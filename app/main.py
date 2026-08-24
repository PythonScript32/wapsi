"""
वापसी (Wapsi) API surface.

  GET  /health                 liveness
  POST /webhooks/razorpay      detection sensor (verify -> dedupe -> case)
  POST /replies                inbound customer reply (text or audio) -> voice.inbound
  POST /batch/run              run a batch, return metrics
  GET  /cases                  list cases (dashboard fallback if not using Supabase directly)
  GET  /cases/{id}/audit       full audit trail for one case

Run: uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="वापसी (Wapsi) — Revenue Recovery Agent")

# React dev server runs on :5173
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok", "service": "wapsi"}


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
