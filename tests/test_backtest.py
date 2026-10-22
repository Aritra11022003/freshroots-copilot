"""[C10] Backtest replays, metrics, and the 80% gate (never faked)."""
import pandas as pd

from copilot.backtest import run_backtest, run_and_save
from copilot.db import FakeDB
from copilot.seed import seed


def test_seed_backtest_replays_weeks_and_reports_gate_not_evaluable(seed_tables, tmp_path, monkeypatch):
    import copilot.backtest as bt
    monkeypatch.setattr(bt, "PAST_DECISIONS", tmp_path / "missing.csv")
    m = run_backtest(seed_tables)
    assert m["history_weeks"] == 16
    assert m["decision_weeks"] == ["2026-W39", "2026-W40", "2026-W41", "2026-W42", "2026-W43"]
    assert sum(m["action_mix"].values()) == 15 * 5
    assert m["low_confidence"] is True
    assert m["gate"]["status"] == "NOT_EVALUABLE" and "match_rate" not in m["gate"]
    assert "needs Raghav's past decisions" in m["gate"]["message"]
    assert m["stockouts_flagged_in_advance"]["status"] == "NOT_EVALUABLE"


def test_latest_replay_equals_live_run(seed_tables):
    from copilot.engine import run_rules
    m = run_backtest(seed_tables)
    live, _ = run_rules(seed_tables["weekly_sales"], seed_tables["inventory"], seed_tables["skus"],
                        seed_tables["calendar"], "2026-10-22")
    assert m["timeline"]["2026-W43"] == dict(zip(live.sku, live.action))


def test_gate_pass_and_fail_with_past_decisions(seed_tables):
    m = run_backtest(seed_tables)
    t = m["timeline"]["2026-W43"]
    rows = [{"sku": s, "week": "2026-W43", "action": a} for s, a in t.items()]
    good = run_backtest(seed_tables, past_decisions=pd.DataFrame(rows))
    assert good["gate"]["status"] == "PASS" and good["gate"]["match_rate"] == 1.0
    bad_rows = [dict(r, action="NO_ACTION" if r["action"] != "NO_ACTION" else "REORDER_NOW") for r in rows]
    bad = run_backtest(seed_tables, past_decisions=pd.DataFrame(bad_rows))
    assert bad["gate"]["status"] == "FAIL" and bad["gate"]["match_rate"] == 0.0


def test_flip_rate_counts_action_changes(seed_tables):
    m = run_backtest(seed_tables)
    for s, rate in m["flip_rate_by_sku"].items():
        acts = [m["timeline"][w][s] for w in m["decision_weeks"]]
        assert rate == round(sum(a != b for a, b in zip(acts, acts[1:])) / 4, 2)


def test_backtest_saved_to_db():
    db = FakeDB()
    seed(db)
    run_and_save(db)
    rows = db.fetch_all("backtest_results")
    assert len(rows) == 1 and rows.iloc[0]["gate_status"] in {"NOT_EVALUABLE", "PASS", "FAIL"}
