# Zoho Inventory setup (India data centre, demo organisation)

This takes about 10 minutes. Steps 4–5 have a **10-minute clock**: the grant code expires quickly.

## 1. Create a Zoho Inventory account and a demo organisation
1. Go to <https://www.zoho.com/in/inventory/> and click **Sign up free**. Use the **India** site, so the account lives in the India data centre (`.in`).
2. When asked to set up an organisation, enter:
   - **Organization name:** `Freshroots Demo`
   - **Country:** India
   - **Currency:** INR

   Use this organisation only for this project. Never connect a real one.
3. Find the **Organization ID**: click the organisation name at the top right; it's the number shown under the name. (It's also under **Settings → Organization Profile**.)

## 2. Create a Self Client
1. Open <https://api-console.zoho.in> and make sure the URL ends in **`.in`**.
2. Click **Get Started** (or **Add Client**), choose **Self Client**, then **Create**, then **OK**.
3. Open the **Client Secret** tab. You'll see a **Client ID** and a **Client Secret**.

## Easiest path: one command
After steps 1–2, run `.venv/bin/python scripts/zoho_connect.py`. It asks for the Client ID, Client Secret,
Organization ID and a fresh grant code (all hidden), writes the `ZOHO_*` lines into `.env`, gets the refresh
token and runs the read-only check. Steps 3–6 below are the manual equivalent.

## 3. Put the first values in `.env`
Add these lines to the project's `.env` file, below the Supabase lines:

```
ZOHO_ENABLED=false
ZOHO_CLIENT_ID=<Client ID>
ZOHO_CLIENT_SECRET=<Client Secret>
ZOHO_ORG_ID=<Organization ID>
ZOHO_ACCOUNTS_URL=https://accounts.zoho.in
ZOHO_API_BASE=https://www.zohoapis.in/inventory/v1
```

Keep `ZOHO_ENABLED=false` for now. It stays off until Phase 6.

## 4. Generate a grant code (starts the 10-minute clock)
1. In the Self Client, open the **Generate Code** tab.
2. **Scope:** paste this exactly as one line:
   ```
   ZohoInventory.items.READ,ZohoInventory.items.CREATE,ZohoInventory.contacts.READ,ZohoInventory.contacts.CREATE,ZohoInventory.purchaseorders.READ,ZohoInventory.purchaseorders.CREATE
   ```
3. **Time duration:** 10 minutes.
4. **Scope description:** `Freshroots copilot`.
5. Click **Create**, choose the **Freshroots Demo** organisation if asked, then **Create** again.
6. Copy the code it shows.

## 5. Swap the grant code for a refresh token (within 10 minutes)
In a terminal in the project folder, run:
```bash
.venv/bin/python scripts/zoho_get_token.py
```
Paste the grant code when asked; it stays hidden as you type. The script prints one line, `ZOHO_REFRESH_TOKEN=...`. Add that line to `.env`.

If you see `invalid_client` or `invalid_code`:
- check that the domain is `.in`, in both `ZOHO_ACCOUNTS_URL` and api-console.zoho.in
- the code may have expired or already been used; generate a new one (step 4)

The refresh token does not expire. Treat it like a password.

## 6. Check the connection (read-only)
```bash
.venv/bin/python scripts/zoho_check.py
```
It prints counts of items, vendors and purchase orders, plus one purchase order with personal fields redacted. It creates, changes and deletes nothing.

## Later (Phase 6): secrets in GitHub and Streamlit
The same 7 `ZOHO_*` values go in:
- **GitHub Actions secrets**, because the cron run reads on-order stock from Zoho
- **Streamlit secrets**, in TOML format:

```toml
ZOHO_ENABLED = "true"
ZOHO_CLIENT_ID = "..."
ZOHO_CLIENT_SECRET = "..."
ZOHO_REFRESH_TOKEN = "..."
ZOHO_ORG_ID = "..."
ZOHO_ACCOUNTS_URL = "https://accounts.zoho.in"
ZOHO_API_BASE = "https://www.zohoapis.in/inventory/v1"
```

## What the copilot is allowed to do in Zoho
| Allowed | Never |
|---|---|
| Read items, contacts and purchase orders | Issue a PO (mark it as open or issued), email it, receive it, bill it, cancel it, approve it or delete it |
| Create items (one per SKU) and one vendor, "Freshroots Supplier (demo)" | Touch any organisation other than the demo one |
| Create **draft** purchase orders | |

Procurement reviews and issues the drafts inside Zoho.
