"""Live end-to-end check against the Zoho DEMO org, without touching the real queues/sign-offs.

Uses an in-memory copy of the seed data (plus the real zoho_items mapping), approves two rows,
pushes them as a draft PO with reference FRC-TEST-<run>-FRS, pushes again (must create nothing),
then reads the draft back to confirm Zoho's field names. FRC-TEST drafts never count as on-order.

    .venv/bin/python scripts/zoho_live_test.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from copilot.db import FakeDB, get_db  # noqa: E402
from copilot.pipeline import run_pipeline  # noqa: E402
from copilot.seed import seed  # noqa: E402
from copilot.service import record_decision, sign_assumption, unsigned_keys  # noqa: E402
from copilot.zoho_client import ZohoClient  # noqa: E402
from copilot.zoho_config import settings  # noqa: E402
from copilot.zoho_push import push_drafts  # noqa: E402
from copilot.zoho_sync import sync_on_order  # noqa: E402

z = ZohoClient.from_settings(settings())
db = FakeDB(); seed(db)
db.tables["zoho_items"] = get_db().fetch_all("zoho_items").to_dict("records")
for k in unsigned_keys(db):
    sign_assumption(db, k, "live-test")
run_id = run_pipeline(db, "2026-10-22", on_order_provider=lambda d: sync_on_order(d, z))["run_id"]
recs = db.fetch_all("recommendations", {"run_id": run_id}).set_index("sku")
rec = lambda s: {"sku": s, **recs.loc[s].to_dict()}
record_decision(db, run_id, rec("SKU001"), "approve", "live-test")
record_decision(db, run_id, rec("SKU006"), "override", "live-test", final_action="REORDER_NOW",
                final_qty=40, reason_code="DISTRIBUTOR_SCHEME")
record_decision(db, run_id, rec("SKU003"), "reject", "live-test", note="test reject")

first = push_drafts(db, z, run_id, pushed_by="live-test", prefix="FRC-TEST")
second = push_drafts(db, z, run_id, pushed_by="live-test", prefix="FRC-TEST")
for label, res in (("push 1", first), ("push 2", second)):
    for r in res:
        print(f"{label}: {r['status']:8s} {r['reference_number']} -> {r['zoho_po_number']} "
              f"lines={r['line_count']} qty={r['total_qty']} {r['error'] or ''}")
po_id = (first or second)[0]["zoho_po_id"]
po = z.get_purchase_order(po_id)
print("\nread back:", {k: po.get(k) for k in ("purchaseorder_number", "status", "reference_number", "vendor_name")})
print("line item fields:", sorted(po["line_items"][0].keys()))
print("line quantities:", [{k: li.get(k) for k in li if "quantity" in k or k in ("sku", "name")} for li in po["line_items"]])
qty, meta = sync_on_order(db, z)
print(f"\non-order sync after push: {meta} (FRC-TEST drafts excluded) -> {qty}")
