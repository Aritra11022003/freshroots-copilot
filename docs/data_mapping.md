# Step 0: Data mapping (seed CSVs → engine inputs)

Profiled with `scripts/inspect_data.py` on 2026-10-22. Five files in `data/raw/`, all clean: no nulls, no negatives, no zero-sales weeks, no duplicate keys.

## 1. What each file holds

| File | Rows | Key (unique?) | Columns | Range |
|---|---|---|---|---|
| `sku_master.csv` | 15 | `SKU_ID` ✔ | SKU_ID, SKU_Name, Category, Perishability (`Low`/`High`), Standard_Reorder_Qty (int), Unit_of_Measure, Distributor_ID | SKU001–SKU015 |
| `weekly_sales.csv` | 240 | `SKU_ID, Week` ✔ | SKU_ID, Week (ISO `YYYY-Www`), Units_Sold (int) | 2026-W27 … 2026-W42: 16 weeks × 15 SKUs, no holes |
| `current_inventory.csv` | 15 | `SKU_ID, Location` ✔ (also `SKU_ID` alone) | SKU_ID, Location (all "Pune Central Warehouse"), Units_On_Hand, As_Of_Date | single snapshot 2026-10-19 |
| `distributor_master.csv` | 4 | `Distributor_ID` ✔ | Distributor_ID, Distributor_Name, Region, Status (all Active) | DIST001–DIST004 |
| `calendar.csv` | 24 | `Year_Month` ✔ | Year, Month, Year_Month, Is_Festival_Month (bool) | 2025-01 … 2026-12 |

## 2. Column → engine input

| Engine input | Source | Notes |
|---|---|---|
| Weekly units per SKU | `weekly_sales.Units_Sold` by `SKU_ID, Week` | Latest complete week is W42 (12–18 Oct). Run date 22 Oct is in W43, which is incomplete and ignored. |
| Weekly units per distributor | **not present** | Sales are SKU-level only. See gap G1. |
| On-hand stock | `current_inventory.Units_On_Hand`, summed per SKU across `Location` | One location today, so the sum is a no-op. |
| On-order stock | **not present** | See gap G2. |
| Perishability | `sku_master.Perishability`: `High` → high window (3–4 wk), `Low` → normal window (6–14 wk) | 3 High SKUs: SKU004, SKU007, SKU011. |
| Standard order qty | `sku_master.Standard_Reorder_Qty` | Present for all 15. No default needed today. |
| Launch date | **not present** | See gap G3. |
| Festival weeks | `calendar.Is_Festival_Month` by `Year_Month` | The calendar **has** them (Mar, Oct, Nov in both years), so no fallback is needed. They match Raghav's Oct/Nov/Mar. |
| Region (app filter) | `sku_master.Distributor_ID` → `distributor_master.Region` | Each SKU maps to exactly one distributor. |

## 3. How the files join

```
weekly_sales.SKU_ID ──┐
current_inventory.SKU_ID ──┼── sku_master.SKU_ID ── sku_master.Distributor_ID → distributor_master.Distributor_ID
                       │
weekly_sales.Week → month (ISO Thursday rule, see A2) → calendar.Year_Month
run_date (+ lookahead) → calendar.Year_Month → festival uplift for this run
```

SKU sets are identical across sales, inventory and the SKU table (15 each). All 4 distributor IDs used by SKUs exist in the distributor table.

## 4. Gaps, with a proposed labelled default for each

| # | Gap | Effect | Proposed default (label) | Needs Raghav? |
|---|---|---|---|---|
| G1 | **No distributor-level sales.** `Distributor_ID` on the SKU table is one fixed distributor per SKU, not a sales split. | Guard "distributor dropped ≥30%" cannot run. | **Skip the guard** and write `distributor_drop_guard: skipped, sales are SKU-level` into every run's `notes`. Assumption `distributor_drop_threshold` stays in the sign-off list (0.30 / 5% share / 8-week baseline) for when distributor data arrives. | Signs that it is skipped |
| G2 | **No stock on order** (no Zoho open-PO export). | `include_on_order_in_cover` has nothing to add. A twice-weekly run could re-recommend an order that is already in transit. | `on_order = 0` for every SKU, labelled `DEFAULT: no on-order feed`. Keep `include_on_order_in_cover = true` so it starts working the day an on-order column appears. **Not** routed to exceptions per SKU (it applies to all SKUs equally; it goes in the run notes and on the sign-off page instead). | Yes |
| G3 | **No launch date.** | Can't tell a new SKU from one with missing history. | Launch proxy = first week with `Units_Sold > 0`. History = weeks since that. Averages use only those weeks (never pre-launch zeros). Today every SKU has 16 weeks, so none is "new". | Yes |
| G4 | **No historical stock** (one snapshot only) and **no `raghav_past_decisions.csv`**. | Backtest cover checks and "stockouts flagged in advance" have no ground truth; 80% gate can't be computed. | Backtest reconstructs past on-hand by walking back from the 19 Oct snapshot: `stock[w-1] = stock[w] + sales[w]`, assuming **no receipts** in the window. Labelled `APPROX: no receipts data`. A "stockout week" is then undefined (stock only rises going backwards), so that metric is reported as **not evaluable** rather than faked. The 80% gate shows "not evaluable, needs Raghav's past decisions". | Gives the file |
| G5 | **Only 16 weeks of history.** | A 12-week average needs 12 weeks, so the backtest can only replay weeks W39–W42 (4 decision points). | Run anyway; label every backtest result **low-confidence** (only 4 replayable weeks). | No |
| G6 | **Regions outside the brief.** DIST003 is Karnataka and DIST004 is Gujarat; the case says Freshroots sells in Maharashtra and Goa. | Only affects the region filter. | Use as given. Flag it. | FYI |
| G7 | **Scale.** 15 SKUs and 4 distributors vs 400 SKUs and 1,200 distributors in the case. | None for logic; `fetch_all` paging still built for the 1,000-row cap. | n/a | No |
| G8 | **Perishability values** are `Low`/`High`, not `normal`/`high`. | Mapping only. | `High` → high-perishability; anything else → normal. Unknown or blank → validation gap. Assumption `perishability_source = sku_master.Perishability`. | Yes |

`standard_qty_default_rule` is still offered on the sign-off page as **4 × the 12-week average, rounded up to 10** (labelled `DEFAULT`). It only applies under `--allow-defaults` and only to SKUs Raghav approves; each such SKU goes to the exception queue. Nothing in today's data needs it.

## 5. Interpretation choices (flag if you disagree)

- **A1. Run date and windows.** Run date = today (IST). Sales windows end at the last complete ISO week before the run date. 4-week avg = W39–W42; 12-week avg = W31–W42 (the 12-week window includes the last 4, as in the existing engine).
- **A2. Week → month.** A week belongs to the month of its Thursday (ISO rule). W40 (28 Sep–4 Oct) counts as October. Used only in the backtest; a live run uses the run date's month plus `festival_lookahead_weeks` (default 0).
- **A3. Festival uplift.** Multiplies the 12-week average to give expected weekly demand, which drives cover. The high-demand check compares raw 4w vs 12w averages (`festival_uplift_applies_to_high_demand_check = false`).
- **A4. Cover** = (on_hand + on_order if signed) ÷ (12w avg × uplift).

## 6. Preview on seed data (run date 2026-10-22, October uplift ×1.25)

Computed by hand from the profile, not by the final engine. It is here so you can sanity-check the rules before code is written.

| SKU | Perish. | Cover (wk) | 4w vs 12w | Expected action | Qty |
|---|---|---|---|---|---|
| SKU001 | Low | 3.4 | +21% | REORDER_NOW at 1.5× (stockout + high demand) | 750 |
| SKU003 | Low | 4.4 | 0% | REORDER_NOW | 300 |
| SKU004 | High | 1.8 | +2% | REORDER_NOW | 150 |
| SKU011 | High | 1.8 | +2% | REORDER_NOW | 500 |
| SKU013 | Low | 3.2 | −1% | REORDER_NOW | 120 |
| SKU006 | Low | 22.2 | +1% | PAUSE_NEXT_ORDER | 0 |
| SKU007 | High | 4.1 | +10% | PAUSE_NEXT_ORDER | 0 |
| others (8) | | 6.8–11.5 | | NO_ACTION | 0 |

No guard fires on this data, so **the exception queue will likely be empty** on the seed run (the distributor guard is skipped, nothing is new, and no order overshoots max cover). This is a true result, not a bug. The flow tests will still exercise every guard with synthetic DataFrames.

## 7. Proposed Supabase schema (`supabase/migrations/001_init.sql`)

Seed tables keep CSV column names in snake_case:

```sql
skus        (sku_id text pk, sku_name text, category text, perishability text,
             standard_reorder_qty int, unit_of_measure text, distributor_id text)
distributors(distributor_id text pk, distributor_name text, region text, status text)
calendar    (year_month text pk, year int, month int, is_festival_month boolean)
inventory   (sku_id text, location text, units_on_hand int, as_of_date date,
             primary key (sku_id, location))
weekly_sales(sku_id text, week text, units_sold int, primary key (sku_id, week))
```

The seed tables have **no foreign keys**. That way a bad export still loads, and the C3 gate reports it as a gap instead of the insert failing silently.

App tables (as in §8 of the prompt):

```sql
runs        (id bigserial pk, run_date date, started_at timestamptz, finished_at timestamptz,
             status text check (status in ('ok','blocked','error')),
             triggered_by text check (triggered_by in ('cron','manual')),
             assumptions_signed boolean, notes text)
data_gaps   (id bigserial pk, run_id bigint → runs, sku text, issue text, fix_needed text)
recommendations (id bigserial pk, run_id bigint → runs, sku text, route text, action text,
             recommended_qty int, on_hand int, on_order int, avg_4w numeric, avg_12w numeric,
             demand_change_pct numeric, festival_uplift_pct numeric, expected_weekly numeric,
             weeks_of_cover numeric, cover_window text, flags text, exceptions text, reason text,
             unique (run_id, sku))
decisions   (id bigserial pk, run_id bigint → runs, sku text, engine_action text, final_action text,
             final_qty int, decision text check (decision in ('approve','override','reject')),
             reason_code text, note text, decided_by text, decided_at timestamptz default now())
assumptions (key text pk, value text, signed_off_by text, signed_off_at timestamptz)
backtest_results (id bigserial pk, ran_at timestamptz default now(), metrics jsonb, gate_status text)
```

- Append-only: `runs` and `decisions` get a trigger that rejects UPDATE/DELETE. (The pipeline inserts the run row once at the end, with its final status, so no update is needed.) The latest decision per (run, sku) wins in the app and export.
- RLS is enabled on all 11 tables, with no policies. anon and authenticated users get nothing; the service role bypasses RLS.
- Indexes on `run_id` and `sku` / `sku_id` everywhere they appear.
- The 6 assumption rows are seeded with defaults and `signed_off_by = null`.

There is no Supabase connector or CLI in this session. So after you OK this, I'll write the SQL file and you paste it into the Supabase SQL editor.

## 8. Build plan (prompt §10 order; pytest after each step)

| Step | Files | Component |
|---|---|---|
| 1 | `supabase/migrations/001_init.sql` | C2/C6 schema |
| 2 | `scripts/seed.py` (idempotent upsert from `data/raw/`) | C2 |
| 3 | `copilot/db.py`: `fetch_all()` paging `.range()`, plus an in-memory `FakeDB` with the same interface for tests | C6 |
| 4 | `copilot/validate.py` | C3 |
| 5 | `copilot/engine.py` + `config.json`, adapted from `freshroots-rules-engine/` (SKU-level sales, calendar-driven festivals, launch proxy, on_order default) | C5 |
| 6 | `copilot/pipeline.py` `run_pipeline()` + `run.py` CLI (`--allow-defaults`, `--triggered-by`) | C1/C3/C6 |
| 7 | `.github/workflows/run.yml` (cron `30 1 * * 1,4` + dispatch) | C1 |
| 8 | `app.py` (Queues, Data gaps, Sign-off, Backtest, Export, History, Run now, banner) | C4, C7, C8, C9 |
| 9 | `copilot/backtest.py` | C10 |
| 10 | `README.md` deploy guide, `requirements.txt`, `.env.example`, `.gitignore` | — |

The tests are in `tests/`. There is one test per rule, combination and guard (DataFrames only), plus validation tests, plus `test_flow.py` with the 3 flow cases on `FakeDB`, plus an integration test that is skipped unless `SUPABASE_URL` is set.
