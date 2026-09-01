-- वापसी (Wapsi) — track which channel a promise-to-pay came from.
-- Run this in Supabase Studio → SQL Editor → New query → Run.
-- Idempotent: safe to re-run.

do $$ begin
  create type promise_source as enum ('voice', 'text', 'inferred');
exception when duplicate_object then null; end $$;

-- Nullable: existing rows (and any promise created before this migration
-- ran) simply have no recorded source. New rows always set it --
-- app/promises/tracker.py's record_promise() requires source as a keyword
-- argument, so every promise created going forward is tagged.
alter table promises add column if not exists source promise_source;
