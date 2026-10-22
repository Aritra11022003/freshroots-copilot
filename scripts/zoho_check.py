"""Phase 1: READ-ONLY check against the Zoho demo org. Creates, changes and deletes nothing.

    python scripts/zoho_check.py              # counts + one redacted purchase-order JSON
    python scripts/zoho_check.py --record     # also save redacted samples to tests/fixtures/zoho/recorded/
"""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from copilot.zoho_client import ZohoClient, ZohoError  # noqa: E402
from copilot.zoho_config import settings  # noqa: E402

REDACT = {"email", "phone", "mobile", "address", "billing_address", "shipping_address",
          "delivery_address", "contact_persons", "attention", "street", "street2", "zip", "fax",
          "gst_no", "pan_no", "vat_reg_no", "tax_reg_no", "website", "created_by_id",
          "last_modified_by_id", "submitted_by", "approvers_list", "documents"}


def redact(obj):
    if isinstance(obj, dict):
        return {k: ("<redacted>" if k in REDACT else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def main():
    try:
        z = ZohoClient.from_settings(settings())
        items, contacts, pos = z.list_items(), z.list_contacts(), z.list_purchase_orders()
    except ZohoError as e:
        sys.exit(f"FAILED: {e}")
    vendors = [c for c in contacts if c.get("contact_type") == "vendor"]
    print(f"items: {len(items)}   contacts: {len(contacts)} (vendors: {len(vendors)})   purchase orders: {len(pos)}")
    print("PO status values:", dict(Counter(p.get("status") for p in pos)))
    print("PO received_status values:", dict(Counter(p.get("received_status") for p in pos)))
    print("PO list fields:", sorted(pos[0].keys()) if pos else "-")
    sample = None
    if pos:
        # prefer a PO that has received something, to see the received-quantity field
        pick = next((p for p in pos if p.get("received_status") in ("partially_received", "received")), pos[0])
        sample = redact(z.get_purchase_order(pick["purchaseorder_id"]))
        print("\nSample purchase order (redacted):")
        print(json.dumps(sample, indent=2)[:6000])
        li = (sample.get("line_items") or [{}])[0]
        print("\nLine-item quantity fields:", {k: li.get(k) for k in li if "quantity" in k})
    if "--record" in sys.argv:
        out = ROOT / "tests" / "fixtures" / "zoho" / "recorded"
        out.mkdir(parents=True, exist_ok=True)
        (out / "purchaseorders_list.json").write_text(json.dumps(redact(pos[:5]), indent=2))
        if sample:
            (out / "purchaseorder_detail.json").write_text(json.dumps(sample, indent=2))
        print(f"\nRecorded redacted samples to {out.relative_to(ROOT)}")
    print("\nOK: read-only check passed.")


if __name__ == "__main__":
    main()
