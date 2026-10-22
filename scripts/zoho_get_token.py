"""Phase 1: exchange a Self Client grant code (valid ~10 minutes) for a Zoho REFRESH token.

    python scripts/zoho_get_token.py            # paste the grant code when asked

Reads ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET / ZOHO_ACCOUNTS_URL from .env.
Prints only the one line you must paste into .env / secrets. Nothing else is printed.
"""
import getpass
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from copilot.zoho_config import _get  # noqa: E402

cid, secret, accounts = _get("ZOHO_CLIENT_ID"), _get("ZOHO_CLIENT_SECRET"), _get("ZOHO_ACCOUNTS_URL")
if not (cid and secret):
    sys.exit("Put ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET in .env first (see docs/zoho_setup.md).")
code = getpass.getpass("Paste the grant code (input hidden): ").strip()
r = requests.post(f"{accounts.rstrip('/')}/oauth/v2/token", timeout=30, params={
    "grant_type": "authorization_code", "client_id": cid, "client_secret": secret, "code": code})
body = r.json()
if "refresh_token" not in body:
    err = body.get("error", f"HTTP {r.status_code}")
    hint = ""
    if err in ("invalid_client", "invalid_code"):
        hint = (" -> Check the domain first: India accounts need ZOHO_ACCOUNTS_URL=https://accounts.zoho.in."
                " invalid_code also means the grant code expired or was already used: generate a new one.")
    sys.exit(f"Token exchange failed: {err}.{hint}")
print("\nAdd this line to .env (and later to GitHub / Streamlit secrets):\n")
print(f"ZOHO_REFRESH_TOKEN={body['refresh_token']}")
