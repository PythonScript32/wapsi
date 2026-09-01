// Fetches the batch snapshot from the FastAPI backend (app/main.py's
// GET /batch/results), which serves data/results_{set}.json verbatim. The
// Metrics screen renders exactly what this returns -- it never recomputes
// naive/ceiling/lift itself (those need case["latent"], which only
// app/detection/batch_scanner.py may read).
const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000'

export async function fetchBatchResults(setName = 'dev') {
  let res
  try {
    res = await fetch(`${API_BASE}/batch/results?set=${encodeURIComponent(setName)}`)
  } catch (err) {
    throw new Error(`Couldn't reach the backend at ${API_BASE} (${err.message}). Is uvicorn running?`)
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null)
    throw new Error(body?.detail || `Backend returned ${res.status}`)
  }
  return res.json()
}
