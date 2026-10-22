"""One-step Zoho connection wizard (replaces hand-editing .env + zoho_get_token.py).

    .venv/bin/python scripts/zoho_connect.py

Asks for 4 things (all input hidden): Client ID, Client Secret, Organization ID, grant code.
Exchanges the grant code for a refresh token, writes the ZOHO_* lines into .env (other lines
untouched), then runs the read-only check. Never prints a secret.
"""
import getpass
import subprocess
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
ACCOUNTS = "https://accounts.zoho.in"
API_BASE = "https://www.zohoapis.in/inventory/v1"
SCOPES = ("ZohoInventory.items.READ,ZohoInventory.items.CREATE,ZohoInventory.contacts.READ,"
          "ZohoInventory.contacts.CREATE,ZohoInventory.purchaseorders.READ,ZohoInventory.purchaseorders.CREATE")


def ask(label):
    while True:
        v = getpass.getpass(f"{label} (paste, then Enter; hidden): ").strip().strip('"').strip("'")
        if v:
            return v
        print("  Empty, try again.")


def write_env(values):
    lines = ENV.read_text().splitlines() if ENV.exists() else []
    lines = [l for l in lines if not l.startswith("ZOHO_") and l.strip() != "# Zoho (see docs/zoho_setup.md)"]
    while lines and not lines[-1].strip():
        lines.pop()
    lines += ["", "# Zoho (see docs/zoho_setup.md)"] + [f"{k}={v}" for k, v in values.items()]
    ENV.write_text("\n".join(lines) + "\n")


def current():
    from dotenv import dotenv_values
    v = dotenv_values(ENV) if ENV.exists() else {}
    return {k: (v.get(k) or "").strip() for k in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_ORG_ID")}


def usable(v):
    return bool(v) and not v.startswith("PASTE")


print("\n== Zoho connection (India data centre) ==")
have = current()
if "--reset" in sys.argv:
    have = {k: "" for k in have}
cid = have["ZOHO_CLIENT_ID"] if usable(have["ZOHO_CLIENT_ID"]) else ""
secret = have["ZOHO_CLIENT_SECRET"] if usable(have["ZOHO_CLIENT_SECRET"]) else ""
org = have["ZOHO_ORG_ID"] if usable(have["ZOHO_ORG_ID"]) else ""
if not (cid and secret):
    print("Open https://api-console.zoho.in -> your Self Client -> 'Client Secret' tab.\n")
    cid = ask("Client ID")
    secret = ask("Client Secret")
if not org:
    org = ask("Organization ID (Zoho Inventory: click org name top-right, the number under it)")

# Diagnostics without revealing secrets (a Client ID is a public identifier).
print(f"\n  Client ID     : {cid[:9]}...{cid[-4:]} ({len(cid)} chars; expected 1000. + 30 = 35)")
print(f"  Client Secret : {len(secret)} chars (expected 42)")
print(f"  Org ID        : {len(org)} digits, numeric={org.isdigit()}")
problems = []
if not (cid.startswith("1000.") and len(cid) == 35):
    problems.append("Client ID looks wrong")
if len(secret) != 42:
    problems.append("Client Secret length looks wrong")
if not org.isdigit():
    problems.append("Organization ID should be only digits")
# Save what we have now, so a retry only needs the grant code.
write_env({"ZOHO_ENABLED": "false", "ZOHO_CLIENT_ID": cid, "ZOHO_CLIENT_SECRET": secret,
           "ZOHO_ORG_ID": org, "ZOHO_ACCOUNTS_URL": ACCOUNTS, "ZOHO_API_BASE": API_BASE})
if problems:
    sys.exit("\nSTOP: " + "; ".join(problems) + ". Re-run with --reset and paste again "
             "(use the copy icons; check the paste is not an old clipboard value).")

print("\nNow in the API console open 'Generate Code', paste this as Scope, duration 10 minutes, Create:\n")
print(f"  {SCOPES}\n")
code = ask("Grant code")

result = None
for accounts, api in ((ACCOUNTS, API_BASE), ("https://accounts.zoho.com", "https://www.zohoapis.com/inventory/v1")):
    r = requests.post(f"{accounts}/oauth/v2/token", timeout=30, params={
        "grant_type": "authorization_code", "client_id": cid, "client_secret": secret, "code": code})
    body = r.json()
    print(f"  {accounts}: {'OK' if 'refresh_token' in body else body.get('error', r.status_code)}")
    if "refresh_token" in body:
        result = (accounts, api, body["refresh_token"])
        break
    if body.get("error") != "invalid_client":
        break                                   # right server, other problem: don't try others
if not result:
    sys.exit("\nFAILED. invalid_code = code expired/used: re-run (only the grant code is asked). "
             "invalid_client on both = Client ID/Secret wrong: re-run with --reset.")

accounts, api, refresh = result
write_env({"ZOHO_ENABLED": "false", "ZOHO_CLIENT_ID": cid, "ZOHO_CLIENT_SECRET": secret,
           "ZOHO_REFRESH_TOKEN": refresh, "ZOHO_ORG_ID": org,
           "ZOHO_ACCOUNTS_URL": accounts, "ZOHO_API_BASE": api})
print("\nSaved to .env (Zoho still switched OFF in the app). Running the read-only check...\n")
sys.exit(subprocess.call([sys.executable, str(ROOT / "scripts" / "zoho_check.py"), "--record"]))
