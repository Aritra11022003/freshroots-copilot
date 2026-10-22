"""Phase 4: draft POs per vendor, idempotent, partial failure + retry, only approved rows."""
import pytest

from copilot.db import FakeDB
from copilot.pipeline import run_pipeline
from copilot.seed import seed
from copilot.service import record_decision
from copilot.zoho_push import approved_rows, failed_vendors, push_drafts, retry_failed
from copilot.zoho_sync import setup_mapping
from tests.conftest import RUN_DATE
from tests.zoho_fake_api import FakeZoho


@pytest.fixture
def env():
    db = FakeDB(); seed(db)
    z = FakeZoho(); setup_mapping(db, z)
    run_id = run_pipeline(db, RUN_DATE)["run_id"]
    recs = db.fetch_all("recommendations", {"run_id": run_id}).set_index("sku")
    rec = lambda s: {"sku": s, **recs.loc[s].to_dict()}
    record_decision(db, run_id, rec("SKU001"), "approve", "raghav")                       # 750
    record_decision(db, run_id, rec("SKU006"), "override", "raghav", final_action="REORDER_NOW",
                    final_qty=40, reason_code="DISTRIBUTOR_SCHEME")                       # 40
    record_decision(db, run_id, rec("SKU003"), "reject", "raghav", note="supplier holiday")
    record_decision(db, run_id, rec("SKU002"), "approve", "raghav")                       # qty 0
    return db, z, run_id


def test_one_draft_po_per_vendor_with_only_approved_rows(env):
    db, z, run_id = env
    res = push_drafts(db, z, run_id, pushed_by="raghav")
    assert [r["status"] for r in res] == ["created"]
    po = z.pos[0]
    assert po["status"] == "draft" and po["reference_number"] == f"FRC-{run_id}-FRS"
    item = {i["item_id"]: i["sku"] for i in z.items}
    assert {item[li["item_id"]]: li["quantity"] for li in po["line_items"]} == {"SKU001": 750, "SKU006": 40}
    assert "SKU001: REORDER_NOW 750" in po["notes"] and "DISTRIBUTOR_SCHEME" in po["notes"]
    # undecided exceptions / undecided rows (SKU004, SKU011, SKU013...), rejects and qty-0 are excluded
    assert set(approved_rows(db, run_id)["sku"]) == {"SKU001", "SKU006"}


def test_second_push_creates_nothing_and_shows_existing_number(env):
    db, z, run_id = env
    first = push_drafts(db, z, run_id)
    second = push_drafts(db, z, run_id)
    assert len(z.pos) == 1
    assert second[0]["status"] == "skipped" and second[0]["zoho_po_number"] == first[0]["zoho_po_number"]


def test_existing_reference_in_zoho_is_skipped_even_if_local_log_lost(env):
    db, z, run_id = env
    push_drafts(db, z, run_id)
    db.tables["zoho_po_pushes"] = []                  # local log wiped (restart / lost files)
    res = push_drafts(db, z, run_id)
    assert res[0]["status"] == "skipped" and len(z.pos) == 1


def test_partial_failure_then_retry_only_failed(env):
    db, z, run_id = env
    z.fail_po_create = 1
    res = push_drafts(db, z, run_id)
    assert res[0]["status"] == "failed" and "busy" in res[0]["error"] and not z.pos
    assert failed_vendors(db, run_id) == {res[0]["vendor_id"]}
    again = retry_failed(db, z, run_id)
    assert again[0]["status"] == "created" and len(z.pos) == 1
    assert failed_vendors(db, run_id) == set() and retry_failed(db, z, run_id) == []


def test_unmapped_sku_fails_clearly(env):
    db, z, run_id = env
    db.tables["zoho_items"] = [r for r in db.tables["zoho_items"] if r["sku_id"] != "SKU006"]
    res = push_drafts(db, z, run_id)
    assert {r["status"] for r in res} == {"failed", "created"}
    assert any("SKU006" in (r["error"] or "") and "zoho_setup" in r["error"] for r in res)


def test_nothing_approved_pushes_nothing():
    db = FakeDB(); seed(db); z = FakeZoho(); setup_mapping(db, z)
    run_id = run_pipeline(db, RUN_DATE)["run_id"]
    assert push_drafts(db, z, run_id) == [] and not z.pos


def test_push_log_is_append_only(env):
    db, z, run_id = env
    push_drafts(db, z, run_id)
    with pytest.raises(PermissionError):
        db.update("zoho_po_pushes", {"status": "x"}, {"run_id": run_id})
