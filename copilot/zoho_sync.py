"""Zoho item/vendor mapping (Phase 2) and on-order sync (Phase 3, component C11).

Vendor assumption (signed off in docs/zoho_transition_plan.md): the data has no supplier
column (skus.distributor_id is a customer-side distributor), so every SKU maps to ONE vendor,
"Freshroots Supplier (demo)".

Field names follow Zoho Inventory API v1 docs: contacts.contact_id / contact_name /
contact_type, items.item_id / sku, purchaseorders.status / received_status, and line items
item_id / quantity / quantity_received.
"""
from __future__ import annotations

import pandas as pd

from .db import now_iso
from .zoho_client import ZohoClient, ZohoError

VENDOR_NAME = "Freshroots Supplier (demo)"

# PO statuses whose unreceived quantity counts as stock on order. Drafts count too: a draft the
# copilot pushed but procurement has not issued yet must stop the next run from re-ordering.
OPEN_STATUSES = {"draft", "open", "issued", "partially_received", "pending_approval", "approved"}
# Drafts made by the live integration test / demo mode never count as real stock on order.
TEST_REF_PREFIXES = ("FRC-TEST-", "FRC-DEMO-")


# ---- Phase 2: one vendor + one item per SKU, never duplicated ----------------------------
def ensure_vendor(z: ZohoClient) -> tuple[str, bool]:
    for c in z.list_contacts(contact_name=VENDOR_NAME):
        if c.get("contact_name") == VENDOR_NAME and c.get("contact_type", "vendor") == "vendor":
            return str(c["contact_id"]), False
    body = z.post("contacts", {"contact_name": VENDOR_NAME, "contact_type": "vendor"})
    return str(body["contact"]["contact_id"]), True


def ensure_items(z: ZohoClient, skus: pd.DataFrame) -> tuple[dict[str, str], list[str]]:
    """{sku_id: item_id} for every SKU; creates only the missing ones. Returns (mapping, created)."""
    existing = {str(i.get("sku")): str(i["item_id"]) for i in z.list_items() if i.get("sku")}
    mapping, created = {}, []
    for r in skus.sort_values("sku_id").to_dict("records"):
        sku = str(r["sku_id"])
        if sku in existing:
            mapping[sku] = existing[sku]
            continue
        body = z.post("items", {
            "name": r.get("sku_name") or sku, "sku": sku, "unit": r.get("unit_of_measure") or "Units",
            "item_type": "inventory", "product_type": "goods", "rate": 0, "purchase_rate": 0})
        mapping[sku] = str(body["item"]["item_id"])
        created.append(sku)
    return mapping, created


def setup_mapping(db, z: ZohoClient) -> dict:
    """Idempotent: a second run creates nothing and reports 0 created."""
    skus = db.fetch_all("skus")
    vendor_id, vendor_created = ensure_vendor(z)
    mapping, created = ensure_items(z, skus)
    db.upsert("zoho_items", [{"sku_id": s, "zoho_item_id": i, "zoho_vendor_id": vendor_id,
                              "updated_at": now_iso()} for s, i in mapping.items()])
    return {"vendor_id": vendor_id, "vendor_created": vendor_created,
            "items_created": created, "items_mapped": len(mapping)}


def load_mapping(db) -> pd.DataFrame:
    m = db.fetch_all("zoho_items")
    return m if not m.empty else pd.DataFrame(columns=["sku_id", "zoho_item_id", "zoho_vendor_id"])


# ---- Phase 3: on-order = ordered - received, from POs not yet fully received -----------
def line_open_qty(li: dict) -> float:
    ordered = float(li.get("quantity") or 0)
    # Zoho has two receive paths (purchase receives and "mark as received"); confirmed live.
    received = max(float(li.get("quantity_received") or 0), float(li.get("quantity_marked_as_received") or 0))
    cancelled = float(li.get("quantity_cancelled") or 0)
    return max(ordered - received - cancelled, 0.0)


def is_open(po: dict) -> bool:
    status = str(po.get("status", "")).lower()
    return status in OPEN_STATUSES and str(po.get("received_status", "")).lower() != "received"


def compute_on_order(purchase_orders: list[dict], item_to_sku: dict[str, str]) -> tuple[dict, dict]:
    """purchase_orders: full PO objects (with line_items). Returns ({sku: qty}, {sku: po_count})."""
    qty: dict[str, float] = {}
    pos: dict[str, set] = {}
    for po in purchase_orders:
        if not is_open(po) or str(po.get("reference_number", "")).startswith(TEST_REF_PREFIXES):
            continue
        for li in po.get("line_items", []):
            sku = item_to_sku.get(str(li.get("item_id"))) or li.get("sku")
            if not sku:
                continue
            open_q = line_open_qty(li)
            if open_q > 0:
                qty[sku] = qty.get(sku, 0) + open_q
                pos.setdefault(sku, set()).add(po.get("purchaseorder_id"))
    return {k: int(round(v)) for k, v in qty.items()}, {k: len(v) for k, v in pos.items()}


def sync_on_order(db, z: ZohoClient) -> tuple[dict[str, int], dict]:
    """Read open POs from Zoho, compute on-order per SKU, store a snapshot. Raises ZohoError on
    any failure: the caller must BLOCK the run, never fall back to old numbers."""
    mapping = load_mapping(db)
    item_to_sku = dict(zip(mapping["zoho_item_id"].astype(str), mapping["sku_id"]))
    listed = [p for p in z.list_purchase_orders()
              if is_open(p) and not str(p.get("reference_number", "")).startswith(TEST_REF_PREFIXES)]
    full = [z.get_purchase_order(p["purchaseorder_id"]) for p in listed]
    qty, counts = compute_on_order(full, item_to_sku)
    synced_at = now_iso()
    skus = db.fetch_all("skus")["sku_id"].tolist()
    db.insert("zoho_on_order", [{"synced_at": synced_at, "sku_id": s, "units_on_order": qty.get(s, 0),
                                 "open_po_count": counts.get(s, 0)} for s in skus])
    meta = {"synced_at": synced_at, "open_pos": len(full), "skus_with_open_pos": len(qty)}
    return qty, meta


def zoho_on_order_provider(db):
    """Used by run_pipeline() when ZOHO_ENABLED. Builds the client from secrets."""
    from .zoho_config import settings
    return sync_on_order(db, ZohoClient.from_settings(settings()))


__all__ = ["ZohoError", "VENDOR_NAME", "setup_mapping", "sync_on_order", "compute_on_order"]
