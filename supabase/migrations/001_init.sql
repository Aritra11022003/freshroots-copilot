-- Freshroots Replenishment Copilot: initial schema.
-- Paste into the Supabase SQL editor (or `supabase db push`). Safe to re-run.

-- [C2] 5 seed tables. CSV column names in snake_case. No foreign keys on purpose:
-- a bad export still loads and the C3 validation gate reports it as a data gap.
create table if not exists skus (
  sku_id               text primary key,
  sku_name             text,
  category             text,
  perishability        text,
  standard_reorder_qty integer,
  unit_of_measure      text,
  distributor_id       text
);

create table if not exists distributors (
  distributor_id   text primary key,
  distributor_name text,
  region           text,
  status           text
);

create table if not exists calendar (
  year_month        text primary key,          -- 'YYYY-MM'
  year              integer,
  month             integer,
  is_festival_month boolean
);

create table if not exists inventory (
  sku_id        text,
  location      text,
  units_on_hand integer,
  as_of_date    date,
  primary key (sku_id, location)
);

create table if not exists weekly_sales (
  sku_id     text,
  week       text,                              -- ISO week 'YYYY-Www'
  units_sold integer,
  primary key (sku_id, week)
);

-- [C6] Results
create table if not exists runs (
  id                 bigserial primary key,
  run_date           date not null,
  started_at         timestamptz,
  finished_at        timestamptz,
  status             text not null check (status in ('ok', 'blocked', 'error')),
  triggered_by       text not null check (triggered_by in ('cron', 'manual')),
  assumptions_signed boolean not null default false,
  notes              text
);

create table if not exists data_gaps (
  id         bigserial primary key,
  run_id     bigint not null references runs(id),
  sku        text,
  issue      text not null,
  fix_needed text
);

create table if not exists recommendations (
  id                  bigserial primary key,
  run_id              bigint not null references runs(id),
  sku                 text not null,
  route               text not null check (route in ('APPROVE_QUEUE', 'EXCEPTION_QUEUE')),
  action              text not null,
  recommended_qty     integer not null default 0,
  on_hand             integer,
  on_order            integer,
  avg_4w              numeric,
  avg_12w             numeric,
  demand_change_pct   numeric,
  festival_uplift_pct numeric,
  expected_weekly     numeric,
  weeks_of_cover      numeric,
  cover_window        text,
  flags               text,
  exceptions          text,
  reason              text,
  unique (run_id, sku)
);

create table if not exists decisions (
  id            bigserial primary key,
  run_id        bigint not null references runs(id),
  sku           text not null,
  engine_action text,
  final_action  text,
  final_qty     integer,
  decision      text not null check (decision in ('approve', 'override', 'reject')),
  reason_code   text,
  note          text,
  decided_by    text not null,
  decided_at    timestamptz not null default now()
);

create table if not exists assumptions (
  key           text primary key,
  value         text,
  signed_off_by text,
  signed_off_at timestamptz
);

create table if not exists backtest_results (
  id          bigserial primary key,
  ran_at      timestamptz not null default now(),
  metrics     jsonb,
  gate_status text
);

-- Indexes on run_id and sku
create index if not exists idx_data_gaps_run       on data_gaps(run_id);
create index if not exists idx_data_gaps_sku       on data_gaps(sku);
create index if not exists idx_recs_run            on recommendations(run_id);
create index if not exists idx_recs_sku            on recommendations(sku);
create index if not exists idx_decisions_run       on decisions(run_id);
create index if not exists idx_decisions_sku       on decisions(sku);
create index if not exists idx_weekly_sales_sku    on weekly_sales(sku_id);
create index if not exists idx_inventory_sku       on inventory(sku_id);

-- Append-only: runs and decisions can never be edited or deleted.
create or replace function forbid_update_delete() returns trigger
language plpgsql as $$
begin
  raise exception '% is append-only', tg_table_name;
end $$;

drop trigger if exists runs_append_only on runs;
create trigger runs_append_only before update or delete on runs
  for each row execute function forbid_update_delete();

drop trigger if exists decisions_append_only on decisions;
create trigger decisions_append_only before update or delete on decisions
  for each row execute function forbid_update_delete();

-- One transaction per run: the run row, its recommendations and its gaps land
-- together or not at all. A partial output is worse than none.
create or replace function record_run(p_run jsonb, p_recs jsonb, p_gaps jsonb)
returns bigint
language plpgsql as $$
declare v_id bigint;
begin
  insert into runs (run_date, started_at, finished_at, status, triggered_by, assumptions_signed, notes)
  select run_date, started_at, finished_at, status, triggered_by, assumptions_signed, notes
  from jsonb_populate_record(null::runs, p_run)
  returning id into v_id;

  insert into recommendations (run_id, sku, route, action, recommended_qty, on_hand, on_order,
                               avg_4w, avg_12w, demand_change_pct, festival_uplift_pct,
                               expected_weekly, weeks_of_cover, cover_window, flags, exceptions, reason)
  select v_id, sku, route, action, recommended_qty, on_hand, on_order,
         avg_4w, avg_12w, demand_change_pct, festival_uplift_pct,
         expected_weekly, weeks_of_cover, cover_window, flags, exceptions, reason
  from jsonb_populate_recordset(null::recommendations, coalesce(p_recs, '[]'::jsonb));

  insert into data_gaps (run_id, sku, issue, fix_needed)
  select v_id, sku, issue, fix_needed
  from jsonb_populate_recordset(null::data_gaps, coalesce(p_gaps, '[]'::jsonb));

  return v_id;
end $$;

revoke all on function record_run(jsonb, jsonb, jsonb) from public, anon, authenticated;
grant execute on function record_run(jsonb, jsonb, jsonb) to service_role;

-- Security: RLS on every table, no policies. anon/authenticated get nothing;
-- only the service role key (server-side: GitHub Actions, Streamlit) can read or write.
alter table skus             enable row level security;
alter table distributors     enable row level security;
alter table calendar         enable row level security;
alter table inventory        enable row level security;
alter table weekly_sales     enable row level security;
alter table runs             enable row level security;
alter table data_gaps        enable row level security;
alter table recommendations  enable row level security;
alter table decisions        enable row level security;
alter table assumptions      enable row level security;
alter table backtest_results enable row level security;
