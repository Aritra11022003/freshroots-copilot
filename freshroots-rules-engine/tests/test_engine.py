"""One test per documented rule and per exception guard.

Each case is a single SKU with hand-computable numbers, so Raghav can check
the expected answer on paper.
"""
import pandas as pd
import pytest

from freshroots_rules import (Config, run_rules, last_complete_weeks, APPROVE_QUEUE,
                              EXCEPTION_QUEUE, REORDER_NOW, REORDER_HIGH_DEMAND, PAUSE,
                              NO_ACTION, NEEDS_REVIEW)

JULY = "2026-07-16"     # not a festival month
OCTOBER = "2026-10-22"  # festival month


def case(weekly, on_hand, *, run=JULY, weeks=20, perishability="normal", std=100,
         on_order=0, split=None, cfg=None, stock=True):
    """weekly: units per week (number) or list, newest last. split: {dist: share}."""
    grid = last_complete_weeks(run, weeks)
    series = weekly if isinstance(weekly, list) else [weekly] * weeks
    series = [None] * (weeks - len(series)) + series
    split = split or {"D1": 1.0}
    rows = []
    for wk, u in zip(grid, series):
        if u is None:
            continue
        for d, sh in split.items():
            units = u(d) if callable(u) else u * sh
            rows.append((wk, "S1", d, units))
    sales = pd.DataFrame(rows, columns=["week_start", "sku", "distributor_id", "units"])
    skus = pd.DataFrame([{"sku": "S1", "sku_name": "Test SKU", "perishability": perishability,
                          "standard_order_qty": std}])
    st = pd.DataFrame([{"sku": "S1", "on_hand": on_hand, "on_order": on_order}]) if stock \
        else pd.DataFrame(columns=["sku", "on_hand", "on_order"])
    return run_rules(sales, st, skus, run, cfg).iloc[0]


def test_normal_cover_no_action():
    r = case(100, 1000)                       # 10 weeks of cover, inside 6-14
    assert (r.action, r.route, r.weeks_of_cover) == (NO_ACTION, APPROVE_QUEUE, 10.0)


def test_rule2_stockout_risk():
    r = case(100, 500)                        # 5 weeks < 6
    assert (r.action, r.recommended_qty, r.route) == (REORDER_NOW, 100, APPROVE_QUEUE)


def test_rule3_overstock_risk():
    r = case(100, 1500)                       # 15 weeks > 14
    assert (r.action, r.recommended_qty) == (PAUSE, 0)


def test_rule1_high_demand():
    r = case([100] * 16 + [150] * 4, 1000)    # 4w avg 150 vs 12w avg 116.7 = +28.6%
    assert (r.action, r.recommended_qty, r.route) == (REORDER_HIGH_DEMAND, 150, APPROVE_QUEUE)


def test_just_under_high_demand_threshold():
    r = case([100] * 16 + [125] * 4, 1000)    # 125 vs 108.3 = +15.4%
    assert r.action == NO_ACTION


def test_stockout_and_high_demand_orders_1_5x():
    r = case([100] * 16 + [150] * 4, 500)     # 4.3 weeks cover and demand up
    assert (r.action, r.recommended_qty) == (REORDER_NOW, 150)


def test_rule4_festival_uplift_applied_before_thresholds():
    assert case(100, 700, run=JULY).action == NO_ACTION        # 7 weeks
    r = case(100, 700, run=OCTOBER)                             # 700 / 125 = 5.6 weeks
    assert (r.action, r.expected_weekly_demand) == (REORDER_NOW, 125.0)


def test_rule5_perishable_windows():
    assert case(100, 500, perishability="high").action == PAUSE            # 5 > 4
    r = case(100, 250, perishability="high")                               # 2.5 < 3
    assert (r.action, r.route) == (REORDER_NOW, APPROVE_QUEUE)


def test_perishable_order_overshoot_goes_to_raghav():
    r = case(100, 250, perishability="high", std=300)   # would reach 5.5 weeks > 4
    assert r.route == EXCEPTION_QUEUE and "above the 4-week max" in r.exceptions


def test_new_launch_goes_to_raghav():
    r = case(100, 500, weeks=6)
    assert r.route == EXCEPTION_QUEUE and "new launch" in r.exceptions
    # averages use only post-launch weeks, so no fake demand spike
    assert (r.avg_weekly_12w, r.weeks_of_cover, r.action) == (100.0, 5.0, REORDER_NOW)


def test_conflict_high_demand_but_overstocked():
    r = case([100] * 16 + [150] * 4, 2000)    # 17.1 weeks cover, demand +28.6%
    assert (r.action, r.route) == (PAUSE, EXCEPTION_QUEUE) and "Conflict" in r.exceptions


def test_on_order_counts_toward_cover():
    assert case(100, 300, on_order=400).action == NO_ACTION       # 7 weeks incl. on order
    cfg = Config(include_on_order_in_cover=False)
    assert case(100, 300, on_order=400, cfg=cfg).action == REORDER_NOW


def test_large_distributor_drop_goes_to_raghav():
    weekly = [100] * 19 + [lambda d: 10 if d == "D1" else 50]
    r = case(weekly, 1000, split={"D1": 0.5, "D2": 0.5})
    assert r.route == EXCEPTION_QUEUE and "Distributor D1" in r.exceptions


def test_missing_stock_record_needs_review():
    r = case(100, 0, stock=False)
    assert (r.action, r.route) == (REORDER_NOW, EXCEPTION_QUEUE)
    assert "No stock record" in r.exceptions


def test_dormant_sku_needs_review():
    r = case([100] * 8 + [0] * 12, 500)
    assert (r.action, r.route) == (NEEDS_REVIEW, EXCEPTION_QUEUE)


def test_config_rejects_unknown_keys(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"high_demand_ratio": 1.2, "typo_key": 1}')
    with pytest.raises(ValueError, match="typo_key"):
        Config.from_json(p)
