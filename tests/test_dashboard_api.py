"""Vercel dashboard API: aggregates are right and nothing personal or secret leaks."""
import importlib.util
import json
from pathlib import Path

from copilot.db import FakeDB
from copilot.pipeline import run_pipeline
from copilot.seed import seed
from copilot.service import record_decision, sign_assumption, unsigned_keys
from tests.conftest import RUN_DATE

spec = importlib.util.spec_from_file_location("metrics", Path(__file__).parent.parent / "dashboard/api/metrics.py")
metrics = importlib.util.module_from_spec(spec); spec.loader.exec_module(metrics)


def fake_fetch(db):
    def fetch(table, params=None):
        df = db.fetch_all(table)
        rows = df.to_dict("records") if not df.empty else []
        for k, v in (params or {}).items():
            if isinstance(v, str) and v.startswith("eq."):
                rows = [r for r in rows if str(r.get(k)) == v[3:]]
            if isinstance(v, str) and v.startswith("in.("):
                allowed = set(v[4:-1].split(","))
                rows = [r for r in rows if str(r.get(k)) in allowed]
        return rows
    return fetch


def test_metrics_aggregate_latest_run_and_hide_people():
    db = FakeDB(); seed(db)
    for k in unsigned_keys(db):
        sign_assumption(db, k, "raghav@freshroots.in")
    rid = run_pipeline(db, RUN_DATE)["run_id"]
    recs = db.fetch_all("recommendations", {"run_id": rid}).set_index("sku")
    record_decision(db, rid, {"sku": "SKU001", **recs.loc["SKU001"].to_dict()}, "approve", "raghav@freshroots.in",
                    note="secret note")
    record_decision(db, rid, {"sku": "SKU006", **recs.loc["SKU006"].to_dict()}, "override", "raghav@freshroots.in",
                    final_action="REORDER_NOW", final_qty=40, reason_code="OTHER", note="private")
    m = metrics.build_metrics(fake_fetch(db))
    assert m["latest_run"]["id"] == rid and m["summary"]["skus"] == 15
    assert m["summary"]["reorder_skus"] == 5 and m["summary"]["reorder_units"] == 1820
    assert m["summary"]["decisions"] == {"approve": 1, "override": 1, "reject": 0, "undecided": 13}
    assert m["assumptions"] == {"signed": 6, "total": 6}
    r = {x["sku"]: x for x in m["recommendations"]}
    assert r["SKU006"]["final_qty"] == 40 and r["SKU004"]["window_min"] == 3.0
    # SKU001: (900 + 0 + 750) / 268.6 = 6.1 weeks after the approved order
    assert r["SKU001"]["std_qty"] == 500
    assert r["SKU001"]["projected_cover"] == 6.1 and r["SKU002"]["projected_cover"] is None
    assert m["trend"] == [{"run_id": rid, "run_date": RUN_DATE, "reorder_units": 1820, "reorder_skus": 5, "pause_skus": 2}]
    blob = json.dumps(m, default=str)
    for leak in ("raghav@", "secret note", "private", "decided_by", "pushed_by"):
        assert leak not in blob


def test_metrics_with_no_runs_is_empty_not_error():
    db = FakeDB(); seed(db)
    m = metrics.build_metrics(fake_fetch(db))
    assert m["latest_run"] is None and m["recommendations"] == [] and m["runs"] == []
