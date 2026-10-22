-- Zoho Inventory integration tables. Additive only; safe to re-run.

-- SKU -> Zoho item + vendor (written by scripts/zoho_setup.py; changes only when SKUs change)
create table if not exists zoho_items (
  sku_id         text primary key,
  zoho_item_id   text not null,
  zoho_vendor_id text not null,
  updated_at     timestamptz not null default now()
);

-- [C11] On-order snapshots read from Zoho open purchase orders, one row per SKU per sync
create table if not exists zoho_on_order (
  id             bigserial primary key,
  synced_at      timestamptz not null,
  sku_id         text not null,
  units_on_order integer not null,
  open_po_count  integer not null default 0
);
create index if not exists idx_zoho_on_order_synced on zoho_on_order(synced_at);
create index if not exists idx_zoho_on_order_sku    on zoho_on_order(sku_id);

-- [C12] One row per vendor push attempt (created | skipped | failed). Append-only.
create table if not exists zoho_po_pushes (
  id               bigserial primary key,
  run_id           bigint not null references runs(id),
  vendor_id        text not null,
  vendor_name      text,
  reference_number text not null,
  zoho_po_id       text,
  zoho_po_number   text,
  status           text not null check (status in ('created', 'skipped', 'failed')),
  error            text,
  line_count       integer,
  total_qty        integer,
  pushed_by        text,
  pushed_at        timestamptz not null default now()
);
create index if not exists idx_zoho_po_pushes_run on zoho_po_pushes(run_id);

drop trigger if exists zoho_po_pushes_append_only on zoho_po_pushes;
create trigger zoho_po_pushes_append_only before update or delete on zoho_po_pushes
  for each row execute function forbid_update_delete();

alter table zoho_items     enable row level security;
alter table zoho_on_order  enable row level security;
alter table zoho_po_pushes enable row level security;
