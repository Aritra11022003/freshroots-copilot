"""End-to-end flows on a fake DB layer: seed -> run_pipeline -> decisions -> export."""
import pytest

from copilot.config import Config
from copilot.db import FakeDB
from copilot.pipeline import run_pipeline
from copilot.seed import seed
from copilot.service import (BANNER, banner, build_export, latest_run, queue_frame,
                             record_decision, sign_assumption, unsigned_keys)
from tests.conftest import RUN_DATE


@pytest.fixture
def db():
    d = FakeDB()
    seed(d)
    return d


def sign_all(db):
    for k in unsigned_keys(db):
        sign_assumption(db, k, "raghav@freshroots.in")


def decide_all(db, run_id):
    recs = db.fetch_all("recommendations", {"run_id": run_id}).to_dict("records")
    for r in recs:
        record_decision(db, run_id, r, "approve", "raghav@freshroots.in")
    return recs


# 1. Clean seed data ---------------------------------------------------------------------
def test_clean_seed_run_is_ok_and_export_has_only_approved_rows(db):
    sign_all(db)
    res = run_pipeline(db, RUN_DATE, "cron")
    assert res["status"] == "ok"
    recs = db.fetch_all("recommendations", {"run_id": res["run_id"]})
    assert len(recs) == 15 and recs["sku"].is_unique                 # every SKU exactly once
    assert set(recs["route"]) <= {"APPROVE_QUEUE", "EXCEPTION_QUEUE"}
    assert db.fetch_all("data_gaps").empty
    run = latest_run(db)
    assert run["assumptions_signed"] and run["triggered_by"] == "cron"

    reorders = recs[recs.recommended_qty > 0]
    first, second = reorders.iloc[0].to_dict(), reorders.iloc[1].to_dict()
    record_decision(db, res["run_id"], first, "approve", "raghav")
    record_decision(db, res["run_id"], second, "reject", "raghav", note="supplier closed for Diwali")
    pause = recs[recs.action == "PAUSE_NEXT_ORDER"].iloc[0].to_dict()
    record_decision(db, res["run_id"], pause, "override", "raghav", final_action="REORDER_NOW",
                    final_qty=40, reason_code="DISTRIBUTOR_SCHEME")
    no_act = recs[recs.action == "NO_ACTION"].iloc[0].to_dict()
    record_decision(db, res["run_id"], no_act, "approve", "raghav")    # qty 0 -> not a PO line

    out, msg = build_export(db)
    assert list(out.columns) == ["sku", "qty", "notes"]
    assert set(out["sku"]) == {first["sku"], pause["sku"]}              # only approved / overridden
    assert dict(zip(out.sku, out.qty)) == {first["sku"]: first["recommended_qty"], pause["sku"]: 40}
    assert second["sku"] not in set(out["sku"])


def test_export_uses_latest_run_and_latest_decision(db):
    sign_all(db)
    r1 = run_pipeline(db, RUN_DATE)
    decide_all(db, r1["run_id"])
    r2 = run_pipeline(db, RUN_DATE)
    out, _ = build_export(db)
    assert out.empty                                                     # run 2 has no decisions yet
    rec = db.fetch_all("recommendations", {"run_id": r2["run_id"]})
    rec = rec[rec.sku == "SKU001"].iloc[0].to_dict()
    record_decision(db, r2["run_id"], rec, "approve", "raghav")
    record_decision(db, r2["run_id"], rec, "reject", "raghav", note="changed my mind")
    out, _ = build_export(db)
    assert out.empty                                                     # newest decision wins


# 2. One SKU's standard quantity removed -------------------------------------------------
def test_missing_standard_qty_blocks_run(db):
    sign_all(db)
    db.update("skus", {"standard_reorder_qty": None}, {"sku_id": "SKU004"})
    res = run_pipeline(db, RUN_DATE)
    assert res["status"] == "blocked"
    gaps = db.fetch_all("data_gaps", {"run_id": res["run_id"]})
    assert list(gaps["sku"]) == ["SKU004"] and "Standard reorder quantity" in gaps.iloc[0]["issue"]
    assert db.fetch_all("recommendations").empty                        # no partial output
    assert latest_run(db)["status"] == "blocked"


def test_allow_defaults_needs_signed_rule_and_routes_to_exceptions(db):
    db.update("skus", {"standard_reorder_qty": None}, {"sku_id": "SKU002"})
    assert run_pipeline(db, RUN_DATE, allow_defaults=True)["status"] == "blocked"   # rule unsigned
    sign_assumption(db, "standard_qty_default_rule", "raghav")
    res = run_pipeline(db, RUN_DATE, allow_defaults=True)
    assert res["status"] == "ok"
    r = db.fetch_all("recommendations", {"run_id": res["run_id"]}).set_index("sku").loc["SKU002"]
    assert r["route"] == "EXCEPTION_QUEUE" and "DEFAULTED to 340" in r["exceptions"]


# 3. Unsigned assumptions -----------------------------------------------------------------
def test_unsigned_assumptions_show_banner_and_block_export(db):
    res = run_pipeline(db, RUN_DATE)
    assert res["status"] == "ok"                                         # runs still work
    assert banner(db) == BANNER
    assert latest_run(db)["assumptions_signed"] is False
    assert BANNER in latest_run(db)["notes"]
    decide_all(db, res["run_id"])
    out, msg = build_export(db)
    assert out is None and "Export disabled" in msg
    sign_all(db)
    assert banner(db) is None and build_export(db)[0] is not None


def test_config_change_clears_that_sign_off(db):
    sign_all(db)
    run_pipeline(db, RUN_DATE, cfg=Config.load())
    cfg = Config.load()
    cfg.festival_lookahead_weeks = 2
    res = run_pipeline(db, RUN_DATE, cfg=cfg)
    assert unsigned_keys(db) == ["festival_lookahead_weeks"]
    assert "sign-off cleared for: festival_lookahead_weeks" in res["notes"]


# Decisions rules ---------------------------------------------------------------------------
def test_decision_rules(db):
    res = run_pipeline(db, RUN_DATE)
    rec = db.fetch_all("recommendations", {"run_id": res["run_id"]}).iloc[0].to_dict()
    with pytest.raises(ValueError, match="reason code"):
        record_decision(db, res["run_id"], rec, "override", "raghav", final_qty=999)
    with pytest.raises(ValueError, match="new quantity or action"):
        record_decision(db, res["run_id"], rec, "override", "raghav", reason_code="OTHER")
    with pytest.raises(ValueError, match="note"):
        record_decision(db, res["run_id"], rec, "reject", "raghav")
    with pytest.raises(ValueError, match="decided_by"):
        record_decision(db, res["run_id"], rec, "approve", "")
    assert db.fetch_all("decisions").empty
    record_decision(db, res["run_id"], rec, "approve", "raghav")
    assert len(db.fetch_all("decisions")) == 1                           # one click, one row
    with pytest.raises(PermissionError):
        db.update("decisions", {"note": "x"}, {"sku": rec["sku"]})       # append-only


def test_queue_frame_joins_region_and_perishability(db):
    res = run_pipeline(db, RUN_DATE)
    q = queue_frame(db, res["run_id"])
    assert q.set_index("sku").loc["SKU005", "region"] == "Karnataka"
    assert q.set_index("sku").loc["SKU004", "perishability"] == "High"


def test_engine_crash_records_error_run(db, monkeypatch):
    import copilot.pipeline as p
    monkeypatch.setattr(p, "run_rules", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    res = run_pipeline(db, RUN_DATE)
    assert res["status"] == "error" and latest_run(db)["status"] == "error"
    assert db.fetch_all("recommendations").empty
