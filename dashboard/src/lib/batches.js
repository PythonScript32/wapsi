// The two batches this dashboard can show. `id` doubles as both
// cases.batch_id (Postgres) and the `set` query param on GET /batch/results
// (data/results_{id}.json) -- app/detection/batch_scanner.py writes both
// under the same set_name, so one id cleanly addresses both sources.
export const BATCHES = [
  {
    id: 'holdout',
    label: 'Holdout · 300',
    size: 300,
    description: 'Held-out set · 300 cases · run once, never tuned against',
    // Rendered below the header, above the stat cards (PipelineBoard.jsx) --
    // the strongest credibility signal in the project, so it has to explain
    // itself to a viewer who has never heard the term "holdout set" before.
    explainer:
      '300 cases held back during the entire build and run exactly once. These are the reported numbers.',
  },
  {
    id: 'dev',
    label: 'Dev · 100',
    size: 100,
    description: 'Dev set · 100 cases · used for tuning during the build',
    explainer: '100 cases used while building and tuning the agent.',
  },
]

// README.md reports the holdout numbers -- default to matching it.
export const DEFAULT_BATCH_ID = 'holdout'

export function batchById(id) {
  return BATCHES.find((b) => b.id === id) || BATCHES.find((b) => b.id === DEFAULT_BATCH_ID)
}
