"""Phases 2-3: item/vendor mapping is idempotent; on-order maths; sync failure blocks the run;
flag off = baseline."""
import pandas as pd
import pytest

from copilot.db import FakeDB
from copilot.pipeline import run_pipeline
from copilot.seed import seed
from copilot.zoho_client import ZohoError
from copilot.zoho_sync import VENDOR_NAME, compute_on_order, setup_mapping, sync_on_order
from tests.conftest import RUN_DATE
from tests.zoho_fake_api import FakeZoho


@pytest.fixture
def db():
    d = FakeDB(); seed(d); return d


def test_setup_creates_one_vendor_and_one_item_per_sku_then_zero(db):
    z = FakeZoho()
    r1 = setup_mapping(db, z)
    assert r1["vendor_created"] and len(r1["items_created"]) == 15
    assert [c["contact_name"] for c in z.contacts] == [VENDOR_NAME]
    assert sorted(i["sku"] for i in z.items) == [f"SKU{i:03d}" for i in range(1, 16)]
    r2 = setup_mapping(db, z)
    assert not r2["vendor_created"] and r2["items_created"] == [] and r2["items_mapped"] == 15
    assert len(z.items) == 15 and len(z.contacts) == 1
    m = db.fetch_all("zoho_items")
    assert len(m) == 15 and m["zoho_vendor_id"].nunique() == 1


def test_on_order_maths_partially_received():
    pos = [{"purchaseorder_id": "1", "status": "partially_received", "received_status": "partially_received",
            "line_items": [{"item_id": "a", "quantity": 500, "quantity_received": 200},
                           {"item_id": "b", "quantity": 300, "quantity_received": 300}]},
           {"purchaseorder_id": "2", "status": "draft", "line_items": [{"item_id": "a", "quantity": 50}]},
           {"purchaseorder_id": "3", "status": "received", "received_status": "received",
            "line_items": [{"item_id": "a", "quantity": 999, "quantity_received": 999}]},
           {"purchaseorder_id": "4", "status": "cancelled", "line_items": [{"item_id": "a", "quantity": 999}]}]
    qty, count = compute_on_order(pos, {"a": "SKU001", "b": "SKU003"})
    assert qty == {"SKU001": 350}                     # 500-200 + 50 draft; fully received line = 0
    assert count == {"SKU001": 2}
    assert "SKU003" not in qty                        # no open quantity -> 0


def test_sync_writes_snapshot_and_sku_without_po_is_zero(db):
    z = FakeZoho(); setup_mapping(db, z)
    item = {i["sku"]: i["item_id"] for i in z.items}
    z.pos.append({"purchaseorder_id": "p1", "purchaseorder_number": "PO-1", "status": "open", "received_status": "",
                  "reference_number": "", "line_items": [{"item_id": item["SKU004"], "quantity": 150,
                                                          "quantity_received": 0}]})
    qty, meta = sync_on_order(db, z)
    assert qty == {"SKU004": 150} and meta["skus_with_open_pos"] == 1
    snap = db.fetch_all("zoho_on_order").set_index("sku_id")
    assert snap.loc["SKU004", "units_on_order"] == 150 and snap.loc["SKU001", "units_on_order"] == 0


def test_zoho_on_order_flows_into_the_run(db):
    res = run_pipeline(db, RUN_DATE, on_order_provider=lambda d: ({"SKU004": 150}, {
        "synced_at": "2026-10-22T01:30:00", "skus_with_open_pos": 1}))
    recs = db.fetch_all("recommendations", {"run_id": res["run_id"]}).set_index("sku")
    assert recs.loc["SKU004", "on_order"] == 150
    # SKU004: (180 + 150) / 99.75 = 3.3 weeks, inside 3-4 -> no reorder now
    assert recs.loc["SKU004", "action"] == "NO_ACTION"
    assert "on_order source: Zoho" in res["notes"] and "on_order = 0" not in res["notes"]


def test_skus_without_open_pos_unchanged_vs_baseline(db):
    base = run_pipeline(db, RUN_DATE)
    zoho = run_pipeline(db, RUN_DATE, on_order_provider=lambda d: ({"SKU004": 150}, {
        "synced_at": "x", "skus_with_open_pos": 1}))
    cols = ["sku", "action", "recommended_qty", "weeks_of_cover", "route"]
    a = db.fetch_all("recommendations", {"run_id": base["run_id"]})[cols].set_index("sku").drop("SKU004")
    b = db.fetch_all("recommendations", {"run_id": zoho["run_id"]})[cols].set_index("sku").drop("SKU004")
    pd.testing.assert_frame_equal(a.sort_index(), b.sort_index())


def test_sync_failure_blocks_the_run(db):
    def boom(d):
        raise ZohoError("Zoho did not answer within 30 seconds", "timeout", "GET /purchaseorders")
    res = run_pipeline(db, RUN_DATE, on_order_provider=boom)
    assert res["status"] == "blocked"
    gaps = db.fetch_all("data_gaps", {"run_id": res["run_id"]})
    assert "Zoho on-order sync failed" in gaps.iloc[0]["issue"]
    assert db.fetch_all("recommendations").empty      # no silent fallback to old numbers


def test_flag_off_outputs_equal_baseline(db, seed_tables, monkeypatch):
    from copilot.engine import run_rules
    monkeypatch.setenv("ZOHO_ENABLED", "false")
    res = run_pipeline(db, RUN_DATE)
    got = db.fetch_all("recommendations", {"run_id": res["run_id"]}).set_index("sku")
    t = seed_tables
    want, _ = run_rules(t["weekly_sales"], t["inventory"], t["skus"], t["calendar"], RUN_DATE)
    want = want.set_index("sku")
    for c in ["action", "recommended_qty", "on_order", "route"]:
        assert got[c].to_dict() == want[c].to_dict(), c
    assert "on_order = 0 for every SKU (DEFAULT" in res["notes"]
    assert db.fetch_all("zoho_on_order").empty
