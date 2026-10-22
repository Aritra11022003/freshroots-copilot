"""[C12] Push approved orders to Zoho as DRAFT purchase orders.

- Only approved and overridden rows (qty > 0) of the run: never undecided or rejected rows.
- One draft PO per vendor: vendor_id, line_items (item_id, quantity),
  reference_number = FRC-<run_id>-<vendor code>, notes with each line's reason.
- Idempotent: before creating, Zoho itself is asked whether a PO with that reference_number
  exists (the list endpoint documents no reference filter, so recent POs are listed and
  matched). If it exists the push is skipped and the existing PO number is shown.
- Each vendor is independent: one failing does not stop the others; "Retry failed" re-sends
  only the failed vendors (the reference check covers retries too).
- Every attempt is logged to zoho_po_pushes (append-only).

POs are created with POST /purchaseorders only, which leaves them in Draft. Nothing here
issues, emails, receives, bills, cancels or deletes a PO.
"""
from __future__ import annotations

import re

import pandas as pd

from .db import now_iso
from .zoho_client import ZohoClient, ZohoError
from .zoho_sync import VENDOR_NAME, load_mapping

REF_PREFIX = "FRC"
NOTES_MAX = 4900                     # keep under Zoho's notes length limit


def vendor_code(vendor_name: str, vendor_id: str) -> str:
    if vendor_name == VENDOR_NAME:
        return "FRS"
    letters = re.sub(r"[^A-Z0-9]", "", str(vendor_name).upper())[:3] or "VEN"
    return f"{letters}{str(vendor_id)[-4:]}"


def reference_for(run_id: int, code: str, prefix: str = REF_PREFIX) -> str:
    return f"{prefix}-{run_id}-{code}"


def approved_rows(db, run_id: int) -> pd.DataFrame:
    """Approved/overridden rows with qty > 0 from this run (latest decision per SKU wins),
    joined to the engine's reason. Same selection as the CSV export."""
    from .service import latest_decisions
    dec = latest_decisions(db, run_id)
    if dec.empty:
        return pd.DataFrame(columns=["sku", "final_action", "final_qty", "decision", "reason"])
    keep = dec[dec["decision"].isin(["approve", "override"]) & (dec["final_qty"].fillna(0) > 0)].copy()
    recs = db.fetch_all("recommendations", {"run_id": run_id})
    if not recs.empty:
        keep = keep.merge(recs[["sku", "reason"]], on="sku", how="left")
    return keep.sort_values("sku").reset_index(drop=True)


def _line_note(r: dict) -> str:
    s = f"{r['sku']}: {r['final_action']} {int(r['final_qty'])} ({r['decision']}"
    if r.get("decision") == "override":
        s += f", {r.get('reason_code')}; engine said {r.get('engine_action')}"
    s += ")"
    if isinstance(r.get("note"), str) and r["note"]:
        s += f" note: {r['note']}"
    if isinstance(r.get("reason"), str) and r["reason"]:
        s += f" | {r['reason']}"
    return s


def find_existing(z: ZohoClient, reference: str) -> dict | None:
    for po in z.list_purchase_orders():
        if str(po.get("reference_number", "")).strip() == reference:
            return po
    return None


def push_drafts(db, z: ZohoClient, run_id: int, rows: pd.DataFrame | None = None, *,
                pushed_by: str = "", only_vendors: set[str] | None = None,
                prefix: str = REF_PREFIX) -> list[dict]:
    """Returns one result per vendor: {vendor_id, vendor_name, reference_number, status,
    zoho_po_id, zoho_po_number, error, line_count, total_qty}."""
    rows = approved_rows(db, run_id) if rows is None else rows
    if rows.empty:
        return []
    m = load_mapping(db)
    mapping = m.set_index("sku_id") if not m.empty else None
    rows = rows.copy()
    if mapping is not None:
        rows["zoho_item_id"] = rows["sku"].map(mapping["zoho_item_id"])
        rows["zoho_vendor_id"] = rows["sku"].map(mapping["zoho_vendor_id"])
    else:
        rows["zoho_item_id"] = None
        rows["zoho_vendor_id"] = None

    results = []
    unmapped = rows[rows["zoho_item_id"].isna() | rows["zoho_vendor_id"].isna()]
    if not unmapped.empty:
        results.append(_log(db, run_id, {
            "vendor_id": "UNMAPPED", "vendor_name": None, "reference_number": reference_for(run_id, "UNMAPPED", prefix),
            "status": "failed", "error": "No Zoho item/vendor for SKU(s) " + ", ".join(unmapped["sku"])
                                         + ". Run scripts/zoho_setup.py.",
            "line_count": len(unmapped), "total_qty": int(unmapped["final_qty"].sum())}, pushed_by))
    rows = rows.drop(unmapped.index)

    for vendor_id, g in rows.groupby("zoho_vendor_id"):
        vendor_id = str(vendor_id)
        if only_vendors is not None and vendor_id not in only_vendors:
            continue
        name = VENDOR_NAME
        ref = reference_for(run_id, vendor_code(name, vendor_id), prefix)
        base = {"vendor_id": vendor_id, "vendor_name": name, "reference_number": ref,
                "line_count": len(g), "total_qty": int(g["final_qty"].sum())}
        try:
            existing = find_existing(z, ref)
            if existing:
                results.append(_log(db, run_id, {**base, "status": "skipped",
                                                 "zoho_po_id": str(existing.get("purchaseorder_id")),
                                                 "zoho_po_number": existing.get("purchaseorder_number"),
                                                 "error": "Already in Zoho; nothing created"}, pushed_by))
                continue
            notes = "\n".join(_line_note(r) for r in g.to_dict("records"))
            body = z.post("purchaseorders", {
                "vendor_id": vendor_id,
                "reference_number": ref,
                "line_items": [{"item_id": str(r["zoho_item_id"]), "quantity": int(r["final_qty"])}
                               for r in g.to_dict("records")],
                "notes": (f"Freshroots Replenishment Copilot run {run_id}. DRAFT for procurement review.\n"
                          + notes)[:NOTES_MAX],
            })
            po = body.get("purchaseorder", {})
            status_note = None if str(po.get("status", "draft")).lower() == "draft" else \
                f"WARNING: Zoho returned status '{po.get('status')}', expected draft"
            results.append(_log(db, run_id, {**base, "status": "created",
                                             "zoho_po_id": str(po.get("purchaseorder_id")),
                                             "zoho_po_number": po.get("purchaseorder_number"),
                                             "error": status_note}, pushed_by))
        except ZohoError as e:
            results.append(_log(db, run_id, {**base, "status": "failed", "error": str(e)}, pushed_by))
    return results


def failed_vendors(db, run_id: int) -> set[str]:
    """Vendors whose LATEST attempt for this run failed."""
    log = db.fetch_all("zoho_po_pushes", {"run_id": run_id})
    if log.empty:
        return set()
    latest = log.sort_values("id").groupby("vendor_id").tail(1)
    return set(latest.loc[latest["status"] == "failed", "vendor_id"].astype(str)) - {"UNMAPPED"}


def retry_failed(db, z: ZohoClient, run_id: int, pushed_by: str = "", prefix: str = REF_PREFIX) -> list[dict]:
    vendors = failed_vendors(db, run_id)
    if not vendors:
        return []
    return push_drafts(db, z, run_id, pushed_by=pushed_by, only_vendors=vendors, prefix=prefix)


def push_log(db, run_id: int) -> pd.DataFrame:
    log = db.fetch_all("zoho_po_pushes", {"run_id": run_id})
    return log.sort_values("id", ascending=False) if not log.empty else log


def _log(db, run_id: int, r: dict, pushed_by: str) -> dict:
    row = {"run_id": int(run_id), "vendor_id": r["vendor_id"], "vendor_name": r.get("vendor_name"),
           "reference_number": r["reference_number"], "zoho_po_id": r.get("zoho_po_id"),
           "zoho_po_number": r.get("zoho_po_number"), "status": r["status"], "error": r.get("error"),
           "line_count": r.get("line_count"), "total_qty": r.get("total_qty"),
           "pushed_by": pushed_by or None, "pushed_at": now_iso()}
    db.insert("zoho_po_pushes", [row])
    return row
