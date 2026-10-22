# Zoho Inventory transition plan (Phase 0 audit)

Branch: `feature/zoho`, cut from `main` at `80b3562`. Audit date: 2026-10-22. No code was changed in Phase 0.

## 1. Baseline

| Check | Result |
|---|---|
| `python -m pytest -q` | **54 passed, 1 skipped.** The skipped test is the Supabase integration test, which needs env vars. |
| Same, with `.env` loaded | **55 passed** (the integration test passes against the live Supabase project) |
| App, local demo (`COPILOT_DEMO=1 streamlit run app.py`) | Starts; `/_stcore/health` returns 200; all 6 pages render (`tests/test_app.py`) |
| App, live | https://freshroots-copilot.streamlit.app/: Queues, Data gaps, Sign-off, Backtest, Export and History all render against Supabase (checked by hand on 2026-10-22) |
| GitHub Actions | `Replenishment run` succeeded (run 2: ok, 15 recommendations) |

Every phase must keep these 54 (+1) tests passing.

## 2. Structure

- **Entry points:**
  - `app.py`: Streamlit. Pages are chosen with a sidebar radio: Queues, Data gaps, Sign-off, Backtest, Export, History. "Run now" is in the sidebar.
  - `run.py`: the CLI the cron uses.
- **Modules (`copilot/`):**
  - `config.py`: `config.json` and the 6 assumptions.
  - `db.py`: `SupabaseDB` and `FakeDB`, with `fetch_all` paging.
  - `seed.py`: CSV → seed rows.
  - `weeks.py`: ISO week helpers.
  - `validate.py`: C3 validation gate.
  - `engine.py`: C5 rules.
  - `pipeline.py`: `run_pipeline()`.
  - `service.py`: sign-off, queues, decisions and export logic.
  - `backtest.py`: C10.
- **Scripts:** `scripts/seed.py`, `scripts/inspect_data.py`.
- **Tests:** in `tests/`, run with `python -m pytest -q`. Engine and validation tests use DataFrames only. Flow tests use `FakeDB`. `test_app.py` uses Streamlit `AppTest` on the demo DB.

## 3. Data source and where `on_order` is read today

- **Source:** Supabase, for both seed tables and results. CSVs in `data/raw/` are used only by the one-time seed.
- **`on_order` today:**
  - `copilot/engine.py` `run_rules()`, lines 141–146. If the `inventory` DataFrame has a `units_on_order` column, it is used. Otherwise `on_order = 0` and the run note says `on_order = 0 for every SKU (DEFAULT: no on-order feed)`.
  - The `inventory` table has no on-order column, so today on_order is always 0.
- **Single plug-in point:** `copilot/pipeline.py` `run_pipeline()`, just after `tables = {t: db.fetch_all(t) ...}` (line 53). When `ZOHO_ENABLED`, Zoho's per-SKU on-order quantity is merged into `tables["inventory"]` as `units_on_order`. **The engine is not touched;** it already accepts that column. With the flag off, nothing changes.

## 4. Persistence

- Runs, recommendations, decisions, data gaps, assumptions and backtests are all in **Supabase**. They're written via `record_run()` (one transaction) and `db.insert`.
- **Nothing is stored on the local filesystem.** The Streamlit Cloud restart risk therefore does not apply.
- Duplicate-PO protection will still come from Zoho itself (a reference-number lookup), as Phase 4 requires. Push results will also be logged to a new Supabase table.

## 5. Approve flow and export

- **Recording a decision:**
  - The function is `copilot/service.py` `record_decision()`, which writes one append-only `decisions` row.
  - In the UI it's called from `app.py` `decision_form()` (the "Save decision" button) and from the "Approve all N undecided rows shown" button.
- **Building the CSV export:**
  - `copilot/service.py` `build_export()` selects the latest successful run, then the latest decision per SKU, keeping `approve`/`override` rows with `final_qty > 0`.
  - It's disabled while any assumption is unsigned.
  - `app.py` "Export" page offers the download as `zoho_draft_po_<date>.csv`.
- **Push selection:** Phase 4 reuses exactly this selection, so undecided exceptions and rejected rows can never be pushed.

## 6. SKU identity and vendor

- **SKU code:** `skus.sku_id` (SKU001–SKU015). It will be written to the Zoho item's `sku` field.
- **No supplier/vendor column exists.** `skus.distributor_id` → `distributors` (DIST001–DIST004, with regions Maharashtra, Karnataka and Gujarat). In the case, distributors are Freshroots' **customers** (the 1,200 distributors it sells through), not suppliers.
- **⚠️ Decision needed:**
  - **Option A (recommended, per the prompt's rule for "no vendor column"):** one vendor, "Freshroots Supplier (demo)", for every SKU. Each run then produces at most 1 draft PO.
  - **Option B:** treat `distributor_id` as the vendor key, which gives 4 vendors and up to 4 POs per run. This exercises the per-vendor grouping and partial-failure logic more, but it misreads what a distributor is.

## 7. Secrets and deployment

- **Supabase:**
  - Scripts and Actions: `os.getenv` after `load_dotenv()`.
  - App on Streamlit Cloud: `st.secrets` fallback, in `copilot/db.py` `get_db()`.
- **`.gitignore`** already contains `.env` and `.streamlit/secrets.toml` ✔.
- **Deployment:**
  - App: Streamlit Community Cloud, from `main`.
  - Cron: GitHub Actions (`.github/workflows/run.yml`) on Mon/Thu, with the Supabase secrets in Actions secrets.
- **⚠️ The cron also runs the pipeline,** so the Zoho flag and secrets must be set in **both** places: GitHub Actions secrets and Streamlit secrets. Otherwise a cron run (flag off, on_order = 0) and a "Run now" in the app (flag on) would use different on-order numbers. Plan: `ZOHO_ENABLED` and the Zoho secrets go in both. Until Phase 6, both stay off.

## 8. File-by-file plan

| Phase | File | New / edit | Why |
|---|---|---|---|
| 1 | `docs/zoho_setup.md` | new | Your exact steps: demo org (India DC), Self Client, scopes, grant code |
| 1 | `copilot/zoho_client.py` | new | `ZohoClient` (requests): refresh-token auth, auto-refresh on expiry/401, `organization_id` on every call, pagination, backoff on 429/5xx, 30 s timeouts, `ZohoError(message, code, endpoint)` |
| 1 | `copilot/zoho_config.py` | new | Reads the 7 `ZOHO_*` settings from `st.secrets` or env; `zoho_enabled()`; reports missing keys |
| 1 | `scripts/zoho_get_token.py` | new | Grant code → refresh token; prints only what to paste |
| 1 | `scripts/zoho_check.py` | new | Read-only: counts items, contacts and POs, and prints one redacted PO JSON |
| 1 | `tests/test_zoho_client.py`, `tests/fixtures/zoho/*.json` | new | Mocked HTTP: token refresh on 401, pagination, 429 retry |
| 1 | `requirements.txt` | edit | Add `requests` |
| 2 | `supabase/migrations/002_zoho.sql` | new | `zoho_items` (sku, zoho_item_id, zoho_vendor_id), `zoho_on_order` (sync snapshots), `zoho_po_pushes` (push log). RLS on, no policies |
| 2 | `copilot/db.py` | edit | Add the new tables to `ORDER`/`PRIMARY_KEYS` only (needed for paging and upsert); no behaviour change |
| 2 | `scripts/zoho_setup.py`, `copilot/zoho_sync.py` (`ensure_items_and_vendor()`) | new | One item per SKU and the vendor(s); looks up before creating; a re-run creates 0 |
| 3 | `copilot/zoho_sync.py` (`sync_on_order()`) | new code | Open (not fully received) POs → per SKU, ordered − received; writes a `zoho_on_order` snapshot |
| 3 | `copilot/pipeline.py` | edit (~10 lines) | If the flag is on, call `sync_on_order()` and merge `units_on_order` into the inventory frame. On failure: **blocked** run plus a `data_gaps` row "Zoho on-order sync failed: …". Note the source and sync time in run notes |
| 3 | `copilot/config.py` | edit (help text only) | The `include_on_order_in_cover` help text mentions the Zoho source. The assumption **value** is unchanged, so its sign-off is not cleared |
| 3 | `app.py` | edit | Queues caption shows the on-order source (Zoho/default), sync time and number of SKUs with open POs |
| 4 | `copilot/zoho_push.py` (`push_drafts(run_id, approved_rows)`) | new | Groups by vendor; one draft per vendor with `reference_number = FRC-<run_id>-<vendor code>` and per-line reasons in notes; checks Zoho for that reference first; logs each result to `zoho_po_pushes`; "Retry failed" |
| 4 | `copilot/service.py` | edit (additive) | `approved_rows(db, run_id)`, reusing `build_export`'s selection; `build_export()` unchanged |
| 5 | `app.py` | edit | New "Zoho" page: connection status (button-triggered), last sync, "Send approved to Zoho" with a `session_state` lock, per-vendor results, "Retry failed", CSV fallback. Disabled until sign-off is complete. Cached client via `@st.cache_resource` |
| 6 | `tests/test_zoho_sync.py`, `tests/test_zoho_push.py`, `tests/test_flow.py` (additive) | new / edit | On-order maths, sync failure blocks the run, one PO per vendor, undecided rows excluded, existing reference skipped, partial failure then retry, flag off = baseline |
| 6 | `README.md`, `.github/workflows/run.yml` | edit | Zoho setup section, TOML secrets example, free-plan limit note; Actions passes the `ZOHO_*` secrets through |

**Not touched:**
- `engine.py`
- `validate.py`
- `backtest.py`
- `config.json` thresholds
- the existing pages, beyond the on-order indicator and the new Zoho page

## 9. Contradictions and risks to confirm

1. **Vendor mapping:** Option A or B (§6).
2. **The earlier v1 prompt** put "Zoho API calls" out of scope, and the README says so. This task explicitly brings Zoho into scope, so the README's out-of-scope line will be updated in Phase 6.
3. **The cron needs Zoho secrets too** (§7).
4. **Zoho field names** (PO status values, `quantity`, received quantity, the `reference_number` filter on the list endpoint) will be confirmed from Zoho's official Inventory API docs and one read-only call in Phase 1, before any code depends on them.
5. **Local `git push`:** your global git credential helper points to a deleted `gh` binary. Pushing the branch will need `gh auth setup-git`, or the one-off workaround used last time.

---

# Final report (2026-10-22)

## 1. Files changed or added
| File | Change | Why |
|---|---|---|
| `copilot/zoho_client.py` | new | Zoho API client: refresh-token auth, refresh on expiry/401, `organization_id` on every call, pagination, backoff on 429/5xx, 30 s timeouts, `ZohoError`. GET and POST only |
| `copilot/zoho_config.py` | new | `ZOHO_*` settings from env/.env or `st.secrets`; `zoho_enabled()` flag (default false); missing-key report |
| `copilot/zoho_sync.py` | new | Phase 2 vendor/item mapping (idempotent) and Phase 3 `sync_on_order()` (C11) |
| `copilot/zoho_push.py` | new | Phase 4 `push_drafts()` (C12): one draft per vendor, Zoho-side reference check, retry failed, push log |
| `copilot/pipeline.py` | edit | Optional `on_order_provider`; when the flag is on, Zoho on-order is merged into the inventory frame. A sync failure blocks the run |
| `copilot/db.py` | edit | `zoho_items` added to the paging order and primary keys; `zoho_po_pushes` append-only in `FakeDB` |
| `app.py` | edit | New **Zoho** page; on-order source line on Queues; demo mode reuses the Zoho mapping |
| `supabase/migrations/002_zoho.sql` | new | `zoho_items`, `zoho_on_order`, `zoho_po_pushes` (append-only), RLS |
| `scripts/zoho_connect.py` | new | One-step wizard: 4 hidden prompts, refresh token, `.env`, read-only check |
| `scripts/zoho_get_token.py` | new | Manual grant code → refresh token |
| `scripts/zoho_check.py` | new | Read-only counts and one redacted PO; `--record` saves fixtures |
| `scripts/zoho_setup.py` | new | Creates the vendor and items once; a re-run creates 0 |
| `scripts/zoho_live_test.py` | new | Live push and duplicate check against the demo org, without touching real queues |
| `tests/test_zoho_*.py`, `tests/zoho_fakes.py`, `tests/zoho_fake_api.py`, `tests/fixtures/zoho/**` | new | Mocked HTTP and fake-Zoho tests, plus recorded redacted real PO |
| `tests/conftest.py`, `tests/test_app.py` | edit | Flag forced off in tests; Zoho page smoke tests |
| `.github/workflows/run.yml` | edit | Passes `ZOHO_*` secrets to the cron run |
| `requirements.txt` | edit | `requests` |
| `README.md`, `docs/zoho_setup.md` | edit / new | Zoho setup, TOML secrets example, free-plan limits |

`engine.py`, `validate.py`, `backtest.py` and the `config.json` thresholds are **untouched**.

## 2. Tests
- **Baseline:** 54 passed, 1 skipped (55 with Supabase env).
- **Final:** **88 passed, 2 skipped.** The two skipped tests are the Supabase and Zoho live tests. All 54 baseline tests still pass, and the Zoho live test passes with `ZOHO_LIVE_TESTS=1`.

## 3. `zoho_check.py`
- **First run (empty org):** 0 items, 0 contacts, 0 POs.
- **After the setup and the test push:** 15 items, 1 vendor, 2 POs, all `draft`, with `received_status` `to_be_received`.
- **Line-item fields confirmed live:** `quantity`, `quantity_received`, `quantity_marked_as_received`, `quantity_cancelled`, `quantity_intransit`, `quantity_billed`, `item_id`, `sku`.

## 4. Items and vendors created
- The vendor "Freshroots Supplier (demo)" was created.
- 15 items were created, SKU001–SKU015, each with our SKU code in the Zoho `sku` field.
- A re-run reported "already existed" and **0 created**.

## 5. On-order sync
- Live run 3 on Supabase with Zoho on: ok, "on_order source: Zoho … 0 SKUs with open POs".
- 15 snapshot rows were written.
- Recommendations are **identical** to run 2 (Zoho off), because the only POs in the org are test/demo drafts, which are excluded.

## 6. Test pushes
| Push | Reference | Result |
|---|---|---|
| Script, 1st | `FRC-TEST-16-FRS` | **created PO-00001** (draft): SKU001×750, SKU006×40 (override). The rejected and undecided rows were excluded |
| Script, 2nd | `FRC-TEST-16-FRS` | **skipped**, "Already in Zoho"; nothing created |
| App (demo mode), 1st click | `FRC-DEMO-16-FRS` | **created PO-00002** (draft): SKU001×750, SKU003×300, SKU004×150, SKU011×500, SKU013×120 |
| App, 2nd click | `FRC-DEMO-16-FRS` | **skipped**, showed PO-00002 |

## 7. Assumptions to confirm with the instructor
1. **One demo vendor** for all SKUs. `distributor_id` is customer-side, and there's no supplier column. Approved in Phase 0.
2. **Drafts count as on-order.** A pushed but not-yet-issued draft must stop the next run re-ordering the same SKU. If procurement deletes a draft, it stops counting. `FRC-TEST-*` and `FRC-DEMO-*` drafts never count.
3. **Received quantity** = max(`quantity_received`, `quantity_marked_as_received`). Zoho has both receive paths; both were confirmed on a live PO.
4. **Open statuses:** draft, open, issued, partially_received, pending_approval, approved. Excluded: received (`received_status` = received), cancelled, closed. Only `draft` has been seen live.
5. **Duplicate check:** the PO list endpoint documents no `reference_number` filter, so the copilot lists POs and matches the reference. That's fine at demo volume; at 1,000s of POs, add a date filter.
6. **PO numbers** come from Zoho's auto-numbering (PO-00001…). We don't set `purchaseorder_number`.
7. **Items** were created as `item_type=inventory`, `product_type=goods`, with rate and purchase_rate set to 0. Prices are left for procurement to fill in Zoho.
8. **Deploy:** the live Streamlit app deploys from `main`. Instead of a separate branch deployment, `feature/zoho` was verified locally and live against the demo org, then merged with the flag **off**. With the flag off the app behaves identically (tested). Turning it on is just adding the secrets.
