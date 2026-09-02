// Fetches the batch snapshot as a static asset the dashboard ships with:
// dashboard/public/data/results_{set}.json, kept in sync with the repo
// root's data/results_{set}.json by scripts/copy-data.js (run automatically
// before `npm run dev` and `npm run build` -- see package.json). No backend
// involved: the Metrics screen renders exactly what app/metrics/compute()
// wrote, never recomputed in the browser (that would need case["latent"],
// which only app/detection/batch_scanner.py may read).
export async function fetchBatchResults(setName = 'dev') {
  let res
  try {
    res = await fetch(`/data/results_${encodeURIComponent(setName)}.json`)
  } catch (err) {
    throw new Error(`Couldn't load results_${setName}.json (${err.message})`)
  }
  if (!res.ok) {
    throw new Error(
      `results_${setName}.json wasn't found (${res.status}). Run a batch first: ` +
        `python -m app.detection.batch_scanner --set ${setName}`
    )
  }
  try {
    return await res.json()
  } catch {
    throw new Error(`results_${setName}.json isn't valid JSON -- try re-running the batch.`)
  }
}
