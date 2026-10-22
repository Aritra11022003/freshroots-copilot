# Freshroots Replenishment Copilot

[![Replenishment run](https://github.com/Aritra11022003/freshroots-copilot/actions/workflows/run.yml/badge.svg)](https://github.com/Aritra11022003/freshroots-copilot/actions/workflows/run.yml)

**Live app:** https://freshroots-copilot.streamlit.app/

Twice a week, a scheduled job reads sales and stock from Supabase, checks the data, applies Raghav's 5 written rules to every SKU, and saves one recommendation per SKU. Raghav approves, overrides or rejects each one in a web app. Procurement downloads only the approved rows as a CSV and imports it into Zoho Inventory. **The system never places an order.**

The component map is at `docs/component_map.png`, and the data mapping and every assumption are in `docs/data_mapping.md`.

| # | Component | Where |
|---|---|---|
| C1 | Scheduled run (Mon & Thu 07:00 IST + manual + "Run now") | `.github/workflows/run.yml`, `run.py`, `copilot/pipeline.py` |
| C2 | 5 seed tables | `supabase/migrations/001_init.sql`, `scripts/seed.py` |
| C3 | Validation gate | `copilot/validate.py` |
| C4 | Fix gaps, sign off | App pages **Data gaps** and **Sign-off** |
| C5 | 5 rules in order | `copilot/engine.py`, `config.json` |
| C6 | Store results | `runs`, `recommendations`, `decisions`, `data_gaps`, `backtest_results` (`copilot/db.py`) |
| C7 | Two queues + reasons | App page **Queues** |
| C8 | Approve / override / reject | Buttons on **Queues** (`copilot/service.py`) |
| C9 | Download draft POs | App page **Export** |
| C10 | Backtest (kill gate) | `copilot/backtest.py`, app page **Backtest** |
| C11 | On-order stock read from Zoho open POs | `copilot/zoho_sync.py` (`sync_on_order`), plugged into `run_pipeline()` |
| C12 | Approved rows become **draft** POs in Zoho | `copilot/zoho_push.py` (`push_drafts`), app page **Zoho** |

---

## Deploy guide (step by step, no coding needed)

You need free accounts on **GitHub**, **Supabase** and **Streamlit Community Cloud**. You also need Python 3.11 on your computer for one step (step 2).

### 1. Create the Supabase project and run the migration
1. Go to <https://supabase.com>, then **New project**. Name it `freshroots-copilot`, pick the Mumbai region (`ap-south-1`), and save the database password somewhere safe.
2. In the left menu, open **SQL Editor**, then **New query**.
3. Open `supabase/migrations/001_init.sql` from this repo, copy **all** of it, paste it in, and click **Run**. You should see "Success. No rows returned". The script is safe to run again.
4. Open **Table Editor**. You should see 11 tables, each marked "RLS enabled".

### 2. Add the keys to `.env`, then load the data
1. In Supabase, go to **Project Settings → API** (or **API Keys**). Copy:
   - the **Project URL**
   - the **service_role** key (in newer projects, the **secret** key; if that one is rejected, use the *legacy* service_role key)

   **Never share this key or put it in a browser.** It bypasses all security.
2. In the project folder, copy `.env.example` to `.env` and paste the two values in.
3. Open a terminal in the project folder and run:
   ```bash
   python3.11 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   python scripts/seed.py
   ```
   You should see `skus 15`, `weekly_sales 240` and so on. Re-running it is safe: rows are upserted, and sign-offs are kept.
4. Optional check: `python run.py --run-date 2026-10-22` should print `run 1: ok, 15 recommendations`.

### 3. Push to GitHub and add the two secrets
1. Create a **private** repo named `freshroots-copilot` on GitHub, then push this folder to it. `.env` is git-ignored and will not be uploaded.
2. In the repo, go to **Settings → Secrets and variables → Actions → New repository secret**. Add:
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_ROLE_KEY`

### 4. Enable the workflow and trigger it once by hand
1. Open the repo's **Actions** tab. If asked, click **I understand my workflows, go ahead and enable them**.
2. Click **Replenishment run → Run workflow → Run workflow**.
3. After 1–2 minutes the run should turn green. It turns **red** when the data has gaps (the run is *blocked*, by design) or when something errors. Open the app's **Data gaps** page to see why.
4. From then on it runs by itself every **Monday and Thursday at 07:00 IST**.

### 5. Deploy on Streamlit Community Cloud
1. Go to <https://share.streamlit.io>, then **Create app → Deploy a public app from GitHub**. Pick the `freshroots-copilot` repo, branch `main`, and main file `app.py`.
2. Under **Advanced settings → Secrets**, paste:
   ```toml
   SUPABASE_URL = "https://YOUR-PROJECT.supabase.co"
   SUPABASE_SERVICE_ROLE_KEY = "your-service-role-key"
   ```
   Set **Python version** to 3.11, then click **Deploy**.
3. After it starts, go to **Settings → Sharing**, set the app to **private** ("Only specific people can view this app"), and invite Raghav's and procurement's email addresses. The app has no login of its own; Streamlit's private sharing is the access control.

### 6. Raghav signs off the assumptions
1. Raghav opens the app and goes to **Sign-off**.
2. He reads each of the 6 assumptions and clicks **Sign**. If he disagrees with a value, it is changed in `config.json` (via a pull request). The changed assumption's sign-off is then cleared automatically, and he signs again.
3. Until all 6 are signed, every page shows **"UNSIGNED ASSUMPTIONS: not for procurement"** and **Export is disabled**. Runs still work.

### Check current free-tier limits
Free tiers change. Check them before relying on this setup:
- **Supabase:** <https://supabase.com/pricing>. Free projects pause after a period of inactivity; the twice-weekly run is meant to keep the project active. Also check database size and project count.
- **Streamlit Community Cloud:** <https://docs.streamlit.io/deploy/streamlit-community-cloud>. Check how many private apps you can have and the resource limits. Apps also sleep when unused and wake on the next visit.
- **GitHub Actions:** free minutes for private repos. Each run takes about 1–2 minutes.

---


## Zoho Inventory (draft POs + on-order stock)

Zoho is behind a feature flag, `ZOHO_ENABLED` (default `false`). With the flag off the app behaves exactly as v1 and the CSV export is the path to Zoho. With it on:

- **Every run** (cron or Run now) reads open purchase orders from Zoho. On-order per SKU is ordered minus received (and cancelled), and it counts toward weeks of cover. If the sync fails, the run is **blocked**; it never silently falls back to old numbers.
- **The Zoho page** sends the latest run's approved and overridden rows to Zoho as **one draft PO per vendor**, with reference `FRC-<run_id>-<vendor>`. Before creating a PO it asks Zoho whether that reference already exists, so a second click or a restart can't create duplicates. Each attempt is logged in `zoho_po_pushes`. "Retry failed" re-sends only failed vendors.
- **The copilot never issues, emails, receives, bills, cancels or deletes a PO.** Procurement reviews and issues the drafts inside Zoho.
- **Vendor:** the data has no supplier column, so every SKU maps to one vendor, "Freshroots Supplier (demo)".

**Setup:** follow [docs/zoho_setup.md](docs/zoho_setup.md), or run `python scripts/zoho_connect.py`, which asks for 4 values. Then:

1. Run `supabase/migrations/002_zoho.sql` in the Supabase SQL editor.
2. Run `python scripts/zoho_setup.py` to create the items and the vendor. It's safe to re-run.
3. Add the secrets to GitHub Actions (the cron sync needs them) and to Streamlit.

**Streamlit secrets (TOML), full example:**
```toml
SUPABASE_URL = "https://YOUR-PROJECT.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = "..."
ZOHO_ENABLED = "true"
ZOHO_CLIENT_ID = "..."
ZOHO_CLIENT_SECRET = "..."
ZOHO_REFRESH_TOKEN = "..."
ZOHO_ORG_ID = "..."
ZOHO_ACCOUNTS_URL = "https://accounts.zoho.in"
ZOHO_API_BASE = "https://www.zohoapis.in/inventory/v1"
```

**Free-plan limits (checked 2026-10-22, [Zoho pricing comparison](https://www.zoho.com/us/inventory/pricing-comparison/)):** 20 purchase orders a month, 1,000 API calls a day, 1 user. One draft per vendor per run at 2 runs a week is about 8 POs a month. Check the current limits before going live.

Drafts with references `FRC-TEST-*` (from `scripts/zoho_live_test.py`) and `FRC-DEMO-*` (from demo mode) never count as on-order stock.

## Using it

| Who | Where | What |
|---|---|---|
| Raghav | **Queues** | Approve tab: rule-covered SKUs. You can approve one at a time, or use "Approve all undecided". Exceptions tab: SKUs that need your judgment, with the reason each one is there. **Override** needs a new quantity or action plus a reason code. **Reject** needs a note. Every click writes one row to `decisions`; nothing is ever edited or deleted. |
| Raghav | **Data gaps** | Why the latest blocked run stopped, SKU by SKU, and what to fix. |
| Raghav | **Sign-off** | The 6 assumptions. |
| Anyone | **Backtest** | The kill gate. |
| Procurement | **Export** | `zoho_draft_po_<date>.csv` with columns (sku, qty, notes). It contains approved and overridden rows with qty > 0, from the latest successful run only. Import it into Zoho Inventory as draft POs. |
| Anyone | **History** | Every run and every decision. |

The **Run now** button in the sidebar runs the same `run_pipeline()` as the cron job. "Allow signed-off defaults" is the same as `--allow-defaults`: it applies only defaults Raghav has signed, and every defaulted SKU goes to the exception queue.

## `raghav_past_decisions.csv` (for the 80% gate)

Put this file at `data/raw/raghav_past_decisions.csv`, commit it, and re-run the backtest:

```csv
sku,week,action
SKU001,2026-W40,REORDER_NOW
SKU006,2026-W40,PAUSE_NEXT_ORDER
SKU002,2026-W41,NO_ACTION
```

- `week` is the ISO week the decision was made (`YYYY-Www`).
- `action` is one of `REORDER_NOW`, `REORDER_1.5X`, `PAUSE_NEXT_ORDER`, `NO_ACTION`, `NEEDS_REVIEW`.

The gate compares these against the engine's replayed action for the same SKU and week, counting rule-covered (approve-queue) SKUs only. It shows PASS at 80% or more, and FAIL below that. Without the file, the gate shows "not evaluable", never a made-up number.

## Developer notes

```bash
python -m pytest -q                      # 88 tests; Supabase / Zoho live tests run only when their env vars are set
COPILOT_DEMO=1 streamlit run app.py      # the app on an in-memory DB seeded from data/raw (nothing saved)
python -m copilot.backtest               # backtest against Supabase, saved to backtest_results
```

- Every Supabase read goes through `fetch_all()`, which pages with `.range()` until a page comes back empty (Supabase caps responses at 1,000 rows).
- The engine and app read only from Supabase. The CSVs in `data/raw/` are used only by the one-time seed (and `raghav_past_decisions.csv` by the backtest).
- `record_run()` is a Postgres function, so the run row, its recommendations and its data gaps are written in one transaction. A partial output is never saved.
- Rule thresholds live in `config.json`, so changing one is a code review.
- `freshroots-rules-engine/` is the earlier prototype; its logic was ported into `copilot/engine.py`.
- Out of scope: issuing or placing orders (Zoho drafts only), SAP APIs, ML forecasting, any LLM call, and custom login.
