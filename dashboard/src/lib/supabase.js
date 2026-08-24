import { createClient } from '@supabase/supabase-js'

// ANON key only. Read access is granted by RLS policies in the migration.
// The service key must never reach the browser.
export const supabase = createClient(
  import.meta.env.VITE_SUPABASE_URL,
  import.meta.env.VITE_SUPABASE_ANON_KEY
)

// Realtime helper: stream case changes so the pipeline animates live in the demo.
export function subscribeToCases(onChange) {
  return supabase
    .channel('cases-stream')
    .on('postgres_changes', { event: '*', schema: 'public', table: 'cases' }, onChange)
    .subscribe()
}
