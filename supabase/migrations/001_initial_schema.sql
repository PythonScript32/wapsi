-- वापसी (Wapsi) — initial schema
-- Run this in Supabase Studio → SQL Editor → New query → Run.
-- Idempotent: safe to re-run.

-- ---------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------
do $$ begin
  create type case_source as enum ('subscription', 'checkout');
exception when duplicate_object then null; end $$;

do $$ begin
  create type reason_category as enum (
    'insufficient_funds', 'expired_card', 'mandate_revoked',
    'bank_downtime', 'technical_other', 'checkout_dropoff'
  );
exception when duplicate_object then null; end $$;

do $$ begin
  create type case_state as enum (
    'DETECTED', 'DIAGNOSED', 'SCHEDULED', 'OUTREACH_SENT', 'AWAITING_RESPONSE',
    'PROMISE_MADE', 'RETRYING', 'RECOVERED', 'ESCALATED', 'CLOSED_LOST'
  );
exception when duplicate_object then null; end $$;

do $$ begin
  create type promise_status as enum ('pending', 'kept', 'broken');
exception when duplicate_object then null; end $$;

-- ---------------------------------------------------------------------------
-- cases — one row per at-risk piece of revenue
-- ---------------------------------------------------------------------------
create table if not exists cases (
    id                text primary key,
    batch_id          text not null default 'live',
    source            case_source not null,
    customer_ref      text not null,
    customer_phone    text,
    amount            numeric(12,2) not null,
    currency          text not null default 'INR',
    reason_raw        text,                      -- raw gateway reason string
    reason_category   reason_category,           -- set by diagnosis
    state             case_state not null default 'DETECTED',
    attempts_made     int not null default 0,
    opted_out         boolean not null default false,
    recovered_amount  numeric(12,2) not null default 0,
    recovered_at      timestamptz,
    latent            jsonb,                     -- synthetic ground truth ONLY
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);
create index if not exists idx_cases_state    on cases(state);
create index if not exists idx_cases_batch    on cases(batch_id);
create index if not exists idx_cases_reason   on cases(reason_category);

-- ---------------------------------------------------------------------------
-- payment_attempts — every retry/charge, idempotency enforced by unique key
-- ---------------------------------------------------------------------------
create table if not exists payment_attempts (
    id              uuid primary key default gen_random_uuid(),
    case_id         text not null references cases(id) on delete cascade,
    attempt_no      int not null,
    strategy        text,                        -- e.g. 'after_salary_day'
    scheduled_for   timestamptz,
    executed_at     timestamptz,
    razorpay_ref    text,
    result          text,                        -- 'success' | 'failed' | 'pending'
    failure_reason  text,
    idempotency_key text not null unique,        -- NEVER double-charge
    created_at      timestamptz not null default now()
);
create index if not exists idx_attempts_case on payment_attempts(case_id);

-- ---------------------------------------------------------------------------
-- outreach — every message sent and every reply received
-- ---------------------------------------------------------------------------
create table if not exists outreach (
    id               uuid primary key default gen_random_uuid(),
    case_id          text not null references cases(id) on delete cascade,
    channel          text not null,              -- whatsapp | sms | email | voice
    direction        text not null default 'outbound',
    message          text not null,
    language         text default 'hinglish',
    sent_at          timestamptz not null default now(),
    response_text    text,
    response_intent  text,
    responded_at     timestamptz
);
create index if not exists idx_outreach_case on outreach(case_id);

-- ---------------------------------------------------------------------------
-- promises — promise-to-pay lifecycle
-- ---------------------------------------------------------------------------
create table if not exists promises (
    id              uuid primary key default gen_random_uuid(),
    case_id         text not null references cases(id) on delete cascade,
    promised_amount numeric(12,2) not null,
    promised_date   date not null,
    status          promise_status not null default 'pending',
    resolved_at     timestamptz,
    created_at      timestamptz not null default now()
);
create index if not exists idx_promises_case on promises(case_id);
create index if not exists idx_promises_date on promises(promised_date);

-- ---------------------------------------------------------------------------
-- audit_log — append-only. The explainability backbone.
-- ---------------------------------------------------------------------------
create table if not exists audit_log (
    id          bigserial primary key,
    case_id     text,
    ts          timestamptz not null default now(),
    actor       text not null,        -- which component/model acted
    event_type  text not null,        -- DETECTED | DIAGNOSED | DECIDED | GATE_ALLOW | GATE_BLOCK | ACTED | ...
    input       jsonb,
    decision    text,
    reasoning   text,                 -- WHY, in plain language
    action      text,
    result      jsonb
);
create index if not exists idx_audit_case on audit_log(case_id);
create index if not exists idx_audit_ts   on audit_log(ts desc);

-- Enforce append-only: block UPDATE and DELETE on the audit log.
create or replace function audit_log_is_append_only()
returns trigger language plpgsql as $$
begin
  raise exception 'audit_log is append-only; % is not permitted', tg_op;
end $$;

drop trigger if exists trg_audit_no_update on audit_log;
create trigger trg_audit_no_update
  before update or delete on audit_log
  for each row execute function audit_log_is_append_only();

-- ---------------------------------------------------------------------------
-- updated_at maintenance on cases
-- ---------------------------------------------------------------------------
create or replace function touch_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

drop trigger if exists trg_cases_touch on cases;
create trigger trg_cases_touch
  before update on cases
  for each row execute function touch_updated_at();

-- ---------------------------------------------------------------------------
-- Realtime — lets the React dashboard stream live pipeline updates
-- ---------------------------------------------------------------------------
do $$ begin
  alter publication supabase_realtime add table cases;
exception when duplicate_object then null; end $$;
do $$ begin
  alter publication supabase_realtime add table audit_log;
exception when duplicate_object then null; end $$;

-- ---------------------------------------------------------------------------
-- RLS: this is a demo with no end-user auth. The backend uses the service-role
-- key (bypasses RLS). The dashboard uses the anon key and needs read access.
-- Read-only anon policies:
-- ---------------------------------------------------------------------------
alter table cases            enable row level security;
alter table payment_attempts enable row level security;
alter table outreach         enable row level security;
alter table promises         enable row level security;
alter table audit_log        enable row level security;

do $$ begin
  create policy anon_read_cases    on cases            for select using (true);
  create policy anon_read_attempts on payment_attempts for select using (true);
  create policy anon_read_outreach on outreach         for select using (true);
  create policy anon_read_promises on promises         for select using (true);
  create policy anon_read_audit    on audit_log        for select using (true);
exception when duplicate_object then null; end $$;
