"""Phase 2 one-time setup: one Zoho vendor + one Zoho item per SKU, mapping saved to Supabase
`zoho_items`. Looks everything up first; re-running creates nothing ("0 created").

    .venv/bin/python scripts/zoho_setup.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from copilot.db import get_db  # noqa: E402
from copilot.zoho_client import ZohoClient, ZohoError  # noqa: E402
from copilot.zoho_config import settings  # noqa: E402
from copilot.zoho_sync import VENDOR_NAME, setup_mapping  # noqa: E402

try:
    r = setup_mapping(get_db(), ZohoClient.from_settings(settings()))
except ZohoError as e:
    sys.exit(f"FAILED: {e}")
print(f"vendor '{VENDOR_NAME}': {'created' if r['vendor_created'] else 'already existed'}")
print(f"items: {len(r['items_created'])} created, {r['items_mapped']} mapped in total")
if r["items_created"]:
    print("created:", ", ".join(r["items_created"]))
