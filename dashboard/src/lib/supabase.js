import { createClient } from '@supabase/supabase-js'

// ANON key only. Read access is granted by RLS policies in the migration.
// The service key must never reach the browser.
export const supabase = createClient(
  import.meta.env.VITE_SUPABASE_URL,
  import.meta.env.VITE_SUPABASE_ANON_KEY
)

// Realtime helper: stream case changes so the pipeline animates live in the
// demo. Scoped to one batch_id -- without this filter, dev and holdout rows
// both stream into whichever board is open, contradicting the batch that's
// actually selected.
export function subscribeToCases(batchId, onChange) {
  return supabase
    .channel(`cases-stream-${batchId}`)
    .on(
      'postgres_changes',
      { event: '*', schema: 'public', table: 'cases', filter: `batch_id=eq.${batchId}` },
      onChange
    )
    .subscribe()
}
